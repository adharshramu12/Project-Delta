"""Opt-in captcha help: allowlist rules, refusals, attempt limit, human handoff, audit hygiene."""

import asyncio
import json
from pathlib import Path

import pytest

from delta.config import Boundaries, Settings, read_user_settings
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine, Verdict
from delta.tools.captcha import (
    MAX_AUTO_ATTEMPTS,
    CaptchaSolver,
    approval_handoff,
    challenge_kind,
    clean_answer,
    live_allowlist,
    manage_allowlist,
    site_allowed,
)
from delta.tools.filesystem import WriteArgs, WriteFile

SECRET = "X7kQ9"


class FakePage:
    def __init__(self, url: str = "https://erp.example.edu/login", markers: list[str] | None = None,
                 field_type: str = "text") -> None:
        self.url = url
        self.markers = markers or []
        self.field_type = field_type
        self.value = ""
        self.fills = 0

    async def challenge_markers(self) -> list[str]:
        return self.markers

    async def screenshot(self, image: str) -> bytes | None:
        return b"\xff\xd8fake-jpeg"

    async def has_field(self, field: str) -> bool:
        return True

    async def is_secret_field(self, field: str) -> bool:
        return self.field_type == "password"

    async def fill(self, field: str, value: str) -> None:
        self.value = value
        self.fills += 1

    async def field_value(self, field: str) -> str:
        return self.value


class Human:
    """Stands in for the approval channels; `types` is what the user enters on the PC."""

    def __init__(self, page: FakePage, approve: bool = True, types: str = SECRET) -> None:
        self.page, self.approve, self.types, self.asked = page, approve, types, 0

    async def __call__(self, host: str, saved: str) -> bool:
        self.asked += 1
        if self.approve:
            self.page.value = self.types
        return self.approve


def solver(tmp_path: Path, page: FakePage, allowed: list[str], reply: str = SECRET, approve: bool = True,
           clock: list[float] | None = None) -> tuple[CaptchaSolver, Human, list[int]]:
    calls: list[int] = []

    async def vision(b64: str) -> str:
        calls.append(1)
        return reply

    human = Human(page, approve)
    now = clock or [0.0]
    s = CaptchaSolver(allowed_sites=lambda: allowed, vision=vision, ask_human=human,
                      audit=AuditLog(tmp_path / "audit.jsonl"), clock=lambda: now[0])
    return s, human, calls


def audit_text(tmp_path: Path) -> str:
    return (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


@pytest.mark.parametrize(("host", "allowed", "ok"), [
    ("erp.example.edu", ["erp.example.edu"], True),
    ("ERP.Example.edu.", ["https://erp.example.edu:443/login"], True),
    ("erp.example.edu.evil.com", ["erp.example.edu"], False),
    ("evil-erp.example.edu", ["erp.example.edu"], False),
    ("example.edu", ["erp.example.edu"], False),
    ("sub.erp.example.edu", ["erp.example.edu"], False),  # exact means exact
    ("erp.example.edu", ["*.example.edu"], True),
    ("a.b.example.edu", ["*.example.edu"], True),
    ("example.edu", ["*.example.edu"], False),
    ("notexample.edu", ["*.example.edu"], False),
    ("example.edu.evil.com", ["*.example.edu"], False),
    ("anything.com", ["*", "*.", "com", ""], False),  # wildcards for everything are ignored
    ("", ["erp.example.edu"], False),
])
def test_allowlist_matching(host, allowed, ok):
    assert site_allowed(host, allowed) is ok


def test_default_config_is_off(tmp_path):
    assert Settings().captcha.allowed_sites == []
    assert live_allowlist(tmp_path, [])() == []


@pytest.mark.parametrize(("markers", "kind"), [
    (["https://www.google.com/recaptcha/api2/anchor?k=1"], "reCAPTCHA"),
    (["https://www.recaptcha.net/recaptcha/api.js"], "reCAPTCHA"),
    (["g-recaptcha"], "reCAPTCHA"),
    (["https://newassets.hcaptcha.com/captcha/v1/x"], "hCaptcha"),
    (["https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile"], "Cloudflare Turnstile"),
    (["cf-turnstile"], "Cloudflare Turnstile"),
    (["https://erp.example.edu/captcha.jpg"], None),
])
def test_challenge_detection(markers, kind):
    assert challenge_kind(markers) == kind


async def test_recaptcha_hcaptcha_turnstile_are_refused_even_when_allowed(tmp_path):
    for marker in ("https://www.google.com/recaptcha/api2/bframe", "https://hcaptcha.com/1/api.js",
                   "https://challenges.cloudflare.com/turnstile/v0/api.js"):
        page = FakePage(markers=[marker])
        s, human, calls = solver(tmp_path, page, ["erp.example.edu"])
        res = await s.solve(page)
        assert not res.ok and res.output.startswith("NEEDS HUMAN")
        assert calls == [] and human.asked == 0 and page.fills == 0
    assert audit_text(tmp_path).count('"mode": "refused"') == 3


async def test_allowed_site_is_read_and_typed_without_leaking_the_answer(tmp_path):
    page = FakePage()
    s, human, _ = solver(tmp_path, page, ["erp.example.edu"])
    res = await s.solve(page)
    assert res.ok and page.value == SECRET and human.asked == 0
    assert SECRET not in res.output and SECRET not in json.dumps(res.evidence)
    rec = AuditLog(tmp_path / "audit.jsonl").tail(1)[0]
    assert rec["event"] == "captcha"
    assert rec["data"] == {"site": "erp.example.edu", "mode": "auto", "ok": True, "attempt": 1}
    assert SECRET not in audit_text(tmp_path)


async def test_two_auto_attempts_then_human_handoff(tmp_path):
    page = FakePage()
    s, human, calls = solver(tmp_path, page, ["erp.example.edu"])
    for _ in range(MAX_AUTO_ATTEMPTS):  # each wrong answer shows up as a new captcha and a new call
        assert (await s.solve(page)).ok
    assert len(calls) == MAX_AUTO_ATTEMPTS and human.asked == 0
    res = await s.solve(page)
    assert res.ok and res.evidence["mode"] == "human" and human.asked == 1
    assert len(calls) == MAX_AUTO_ATTEMPTS  # no third automatic read
    assert SECRET not in audit_text(tmp_path)


async def test_unreadable_replies_use_up_attempts_in_one_call(tmp_path):
    page = FakePage()
    s, human, calls = solver(tmp_path, page, ["erp.example.edu"], reply="Sorry, I can't read that.")
    res = await s.solve(page)
    assert res.ok and res.evidence["mode"] == "human"
    assert len(calls) == MAX_AUTO_ATTEMPTS and human.asked == 1


async def test_attempts_reset_after_the_window(tmp_path):
    page, now = FakePage(), [0.0]
    s, _, _ = solver(tmp_path, page, ["erp.example.edu"], clock=now)
    for _ in range(MAX_AUTO_ATTEMPTS):
        await s.solve(page)
    now[0] = 16 * 60
    assert (await s.solve(page)).evidence["mode"] == "auto"


async def test_site_not_allowed_goes_straight_to_the_human(tmp_path):
    page = FakePage(url="https://erp.example.edu.evil.com/login")
    s, human, calls = solver(tmp_path, page, ["erp.example.edu"])
    res = await s.solve(page)
    assert res.ok and calls == [] and human.asked == 1
    assert '"reason": "site not in captcha.allowed_sites"' in audit_text(tmp_path)


async def test_denied_or_untyped_handoff_fails_cleanly(tmp_path):
    page = FakePage()
    s, _, _ = solver(tmp_path, page, [], approve=False)
    res = await s.solve(page)
    assert not res.ok and "NEEDS HUMAN" in res.output and page.value == ""

    page2 = FakePage()
    s2, human2, _ = solver(tmp_path, page2, [])
    human2.types = ""  # approved without typing anything
    assert not (await s2.solve(page2)).ok


async def test_password_fields_and_non_web_pages_are_refused(tmp_path):
    page = FakePage(field_type="password")
    s, _, calls = solver(tmp_path, page, ["erp.example.edu"])
    res = await s.solve(page)
    assert not res.ok and "REFUSED" in res.output and calls == [] and page.fills == 0
    blank = FakePage(url="about:blank")
    assert not (await solver(tmp_path, blank, ["erp.example.edu"])[0].solve(blank)).ok


def test_clean_answer():
    assert clean_answer(" X7kQ9 \n") == "X7kQ9"
    assert clean_answer("`ab12c`") == "ab12c"
    assert clean_answer("The captcha says X7kQ9") is None
    assert clean_answer("") is None


async def test_approval_handoff_uses_the_existing_broker():
    broker = ApprovalBroker(timeout_s=2)
    seen = []

    async def presenter(req):
        seen.append(req)
        broker.resolve(req.id, True, by="test")

    broker.add_presenter("test", presenter)
    assert await approval_handoff(broker)("erp.example.edu", "03-captcha.jpg") is True
    assert seen[0].tool == "solve_captcha" and seen[0].summary.startswith("Captcha on erp.example.edu: ")
    assert "03-captcha.jpg" in seen[0].summary


async def test_approval_handoff_timeout_means_no():
    broker = ApprovalBroker(timeout_s=0.05)
    assert await asyncio.wait_for(approval_handoff(broker)("erp.example.edu", ""), 2) is False


def test_cli_allow_and_remove(tmp_path):
    settings = Settings(data_dir=tmp_path)
    lines: list[str] = []
    assert manage_allowlist(settings, "allow", "https://ERP.example.edu/login", lines.append) == 0
    assert read_user_settings(tmp_path)["captcha"]["allowed_sites"] == ["erp.example.edu"]
    assert live_allowlist(tmp_path, [])() == ["erp.example.edu"]
    assert manage_allowlist(settings, "allow", "*", lines.append) == 2
    assert manage_allowlist(settings, "remove", "erp.example.edu", lines.append) == 0
    assert live_allowlist(tmp_path, ["erp.example.edu"])() == []  # removal wins over the shipped list


def test_agent_cannot_edit_its_own_allowlist(tmp_path):
    """The allowlist lives in Delta's data dir (protected) or its config dir (read-only)."""
    data, config = tmp_path / "data", tmp_path / "config"
    pe = PolicyEngine(Boundaries(autonomy="autopilot"), protected=[data], read_only=[config])
    for target in (data / "settings.yaml", config / "delta.yaml"):
        args = WriteArgs(path=str(target), content="captcha:\n  allowed_sites: ['*.com']\n")
        assert pe.evaluate(WriteFile(), args).verdict == Verdict.DENY
