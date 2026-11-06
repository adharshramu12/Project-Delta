"""Saved site logins: exact origins (anti-phishing), the sealed store, permission per login, Remote Guard, the audit
trail and the CLI. The browser part (typing into real pages) is covered by tests/e2e/test_logins_e2e.py."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.logins.cli import run_cli
from delta.logins.origin import OriginError, lookalike, mismatch, parse_origin, same_origin_login_url
from delta.logins.service import LoginService
from delta.logins.signin import SignInResult
from delta.logins.store import LoginError, LoginStore, VaultMissingError
from delta.logins.tools import ListLogins, ListLoginsArgs, SignIn, SignInArgs
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.remote_guard import REMOTE_PIN_TOOLS, RemoteGuard, approval_refusal, tool_refusal

SECRET = "Canary-Pw-9!zQ"


def seal(text: str) -> str:
    return "sealed:" + text[::-1]


def unseal(text: str) -> str:
    return text[len("sealed:"):][::-1]


def store(tmp_path: Path) -> LoginStore:
    return LoginStore(tmp_path / "logins", seal, unseal)


# --- origins ------------------------------------------------------------------------------------------------

def test_origin_is_scheme_host_and_port():
    o = parse_origin("https://ERP.College.edu/login?next=/home")
    assert str(o) == "https://erp.college.edu" and o.port == 443 and o.secure
    assert str(parse_origin("erp.college.edu")) == "https://erp.college.edu"
    assert str(parse_origin("http://127.0.0.1:8080/x")) == "http://127.0.0.1:8080"
    for bad in ("", "ftp://x.edu", "javascript:alert(1)", "https://user:pw@erp.college.edu", "https://*.edu"):
        with pytest.raises(OriginError):
            parse_origin(bad)


@pytest.mark.parametrize("url", [
    "https://erp.college.edu.evil.com/login",  # the saved host as a prefix of another
    "https://erp-college.edu/login",  # a lookalike name
    "https://xn--rp-college-z4a.edu/login",  # punycode
    "https://lms.college.edu/login",  # another subdomain of the same college
    "https://college.edu/login",  # the parent domain
    "http://erp.college.edu/login",  # downgrade
    "https://erp.college.edu:8443/login",  # another port
    "about:blank", "file:///C:/fake/login.html", "data:text/html,<form>",
])
def test_password_is_only_for_the_exact_origin(url):
    assert mismatch(parse_origin("https://erp.college.edu"), url)


def test_matching_pages_and_plain_reasons():
    saved = parse_origin("https://erp.college.edu")
    assert mismatch(saved, "https://erp.college.edu/") == ""
    assert mismatch(saved, "https://ERP.college.edu:443/student/home") == ""
    assert "downgrade" in mismatch(saved, "http://erp.college.edu/login")
    assert "phishing" in mismatch(saved, "https://erp.college.edu.evil.com/")
    assert lookalike("erp.college.edu", "erp-college.edu") and not lookalike("erp.college.edu", "erp.college.edu")
    assert same_origin_login_url(saved, "https://erp.college.edu/login.aspx") == ""
    assert same_origin_login_url(saved, "https://sso.other.com/login")


# --- the sealed store ----------------------------------------------------------------------------------------

def test_store_seals_and_never_writes_the_password_in_plain_text(tmp_path):
    s = store(tmp_path)
    login = s.add("College ERP", "https://erp.college.edu/", "student01", SECRET,
                  login_url="https://erp.college.edu/login")
    raw = (tmp_path / "logins" / "college_erp.bin").read_text("ascii")
    assert SECRET not in raw and "student01" not in raw
    again = s.get("college erp")
    assert again is not None and again.password == SECRET and again.username == "student01"
    assert s.get("erp.college.edu") == again and s.get("https://erp.college.edu/x") == again
    assert SECRET not in repr(login) and SECRET not in str(login)
    pub = again.public()
    assert "password" not in pub and pub["has_password"] and pub["username"] == "student01"
    assert "username" not in again.public(with_username=False)
    assert s.count() == 1


def test_store_refuses_without_dpapi(tmp_path, monkeypatch):
    from delta.system import vault

    monkeypatch.setattr(vault, "available", lambda: False)
    s = LoginStore(tmp_path / "logins")  # the real sealer: no DPAPI here
    with pytest.raises(VaultMissingError):
        s.add("ERP", "https://erp.college.edu", "u", SECRET)
    assert not (tmp_path / "logins").exists() or not list((tmp_path / "logins").glob("*"))


def test_store_rules(tmp_path):
    s = store(tmp_path)
    with pytest.raises(LoginError, match="plain http"):
        s.add("Old ERP", "http://erp.college.edu", "u", SECRET)
    s.add("Old ERP", "http://erp.college.edu", "u", SECRET, allow_http=True)
    assert s.get("Old ERP").public()["insecure"]
    s.add("Local", "http://127.0.0.1:8080", "u", SECRET)  # this PC itself: no warning needed
    with pytest.raises(LoginError, match="not on"):
        s.add("ERP", "https://erp.college.edu", "u", SECRET, login_url="https://evil.example/login")
    s.add("ERP", "https://erp.college.edu", "u", SECRET)
    with pytest.raises(LoginError, match="already exists"):
        s.add("ERP", "https://erp.college.edu", "u", "other")
    s.add("ERP", "https://erp.college.edu", "u2", "other", replace=True)
    assert s.get("ERP").username == "u2"
    assert s.set_permission("ERP", "always").permission == "always"
    with pytest.raises(LoginError):
        s.set_permission("ERP", "sometimes")
    assert s.remove("ERP") is not None and s.get("ERP") is None


# --- permission, Remote Guard and audit -----------------------------------------------------------------------

class Approver:
    def __init__(self, broker: ApprovalBroker, answers: list[tuple[bool, bool]], by: str = "dashboard") -> None:
        self.broker, self.answers, self.by, self.asked = broker, list(answers), by, []
        broker.add_presenter("test", self.present)

    async def present(self, req: Any) -> None:
        self.asked.append(req)
        approved, for_task = self.answers.pop(0) if self.answers else (False, False)
        asyncio.get_running_loop().call_soon(self.broker.answer, req.id, approved, self.by, for_task)


class FakeEngine:
    calls = 0

    def __init__(self, *a: Any, **k: Any) -> None:
        pass

    async def sign_in(self, page: Any, login: Any) -> SignInResult:
        FakeEngine.calls += 1
        return SignInResult("signed_in", f"signed in to {login.site.host}", "https://erp.college.edu/home")


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr("delta.logins.service.SignInEngine", FakeEngine)
    FakeEngine.calls = 0
    s = store(tmp_path)
    s.add("College ERP", "https://erp.college.edu", "student01", SECRET)
    broker = ApprovalBroker(timeout_s=5)
    audit = AuditLog(tmp_path / "audit.jsonl")
    return LoginService(s, audit, broker), broker, tmp_path


async def test_ask_every_time_by_default(service):
    svc, broker, _ = service
    who = Approver(broker, [(True, False), (True, False)])
    assert (await svc.sign_in("College ERP", object(), task_id="t1")).ok
    assert (await svc.sign_in("College ERP", object(), task_id="t1")).ok
    assert len(who.asked) == 2 and who.asked[0].risk == "external" and who.asked[0].for_task
    assert "College ERP" in who.asked[0].summary and SECRET not in who.asked[0].summary


async def test_allow_for_this_task_covers_only_that_task(service):
    svc, broker, _ = service
    who = Approver(broker, [(True, True), (False, False)])
    assert (await svc.sign_in("College ERP", object(), task_id="t1")).ok
    assert (await svc.sign_in("College ERP", object(), task_id="t1")).ok  # no second question
    assert len(who.asked) == 1
    denied = await svc.sign_in("College ERP", object(), task_id="t2")  # a new task asks again
    assert not denied.ok and denied.outcome == "needs_user" and FakeEngine.calls == 2


async def test_always_allow_does_not_ask(service):
    svc, broker, _ = service
    svc.store.set_permission("College ERP", "always")
    who = Approver(broker, [])
    assert (await svc.sign_in("College ERP", object(), task_id="t1")).ok and not who.asked


async def test_every_use_is_audited_without_the_secret(service):
    svc, broker, tmp = service
    Approver(broker, [(True, False), (False, False)])
    await svc.sign_in("College ERP", object(), task_id="t1")
    await svc.sign_in("College ERP", object(), task_id="t2")
    text = (tmp / "audit.jsonl").read_text("utf-8")
    uses = [json.loads(x) for x in text.splitlines() if '"login.use"' in x]
    assert [u["data"]["outcome"] for u in uses] == ["signed_in", "not_allowed"]
    assert uses[0]["data"]["site"] == "https://erp.college.edu" and uses[0]["data"]["task"] == "t1"
    assert SECRET not in text and "student01" not in text


def test_remote_use_needs_the_pin(tmp_path):
    assert "sign_in" in REMOTE_PIN_TOOLS and "browser_upload" in REMOTE_PIN_TOOLS
    secrets: dict[str, str] = {}
    audit = AuditLog(tmp_path / "audit.jsonl")
    guard = RemoteGuard(tmp_path, audit, get_secret=secrets.get, set_secret=secrets.__setitem__)
    assert "PIN" in (tool_refusal(guard, "telegram", "5", "sign_in") or "")  # no PIN set: refused
    guard.set_pin("4826", by="dashboard")
    assert tool_refusal(guard, "telegram", "5", "sign_in") is not None  # set, not unlocked yet
    assert tool_refusal(guard, "dashboard", "", "sign_in") is None  # the PC itself
    # approving a sign-in prompt from the phone needs the PIN too (it is an external action)
    refused = approval_refusal(guard, "telegram:5", True, "sign_in", "external")
    assert refused is not None and refused[0] is False
    assert guard.try_pin("5", "4826")[0]
    assert tool_refusal(guard, "telegram", "5", "sign_in") is None
    assert approval_refusal(guard, "telegram:5", True, "sign_in", "external") is None


async def test_locked_remote_control_never_signs_in(service, tmp_path):
    svc, _broker, _ = service
    secrets: dict[str, str] = {}
    guard = RemoteGuard(tmp_path, AuditLog(tmp_path / "g.jsonl"), get_secret=secrets.get,
                        set_secret=secrets.__setitem__)
    guard.lock("test", by="dashboard")
    svc.guard = guard
    svc.store.set_permission("College ERP", "always")
    token = CURRENT_REQUEST.set(TaskRequest("check erp", channel="telegram", user="5"))
    try:
        result = await svc.sign_in("College ERP", object(), task_id="t1")
    finally:
        CURRENT_REQUEST.reset(token)
    assert not result.ok and FakeEngine.calls == 0


async def test_tools_never_show_secrets_and_hide_without_logins(service):
    svc, _, _ = service

    class Hub:
        mgr = None
        settings = None

    sign_in, listing = SignIn(svc, Hub()), ListLogins(svc)
    assert not sign_in.available()  # no private browser on this PC
    out = (await listing.run(ListLoginsArgs())).output
    assert "College ERP" in out and SECRET not in out and "student01" not in out
    assert sign_in.domains(SignInArgs(site="College ERP")) == ["https://erp.college.edu"]
    assert SECRET not in json.dumps(sign_in.spec())
    svc.store.remove("College ERP")
    assert not listing.available()


# --- CLI -------------------------------------------------------------------------------------------------------

def _args(**kw: Any) -> argparse.Namespace:
    base = {"action": "list", "name": None, "site": "", "login_url": "", "username": None, "allow_http": False,
            "replace": False, "always": False, "ask": False}
    return argparse.Namespace(**{**base, **kw})


def test_cli_add_list_allow_remove(tmp_path):
    s = store(tmp_path)
    out: list[str] = []
    asked_secret: list[str] = []

    def secret(prompt: str) -> str:
        asked_secret.append(prompt)
        return SECRET

    rc = run_cli(None, _args(action="add", name="College ERP", site="https://erp.college.edu", username="student01"),
                 secret=secret, out=out.append, store=s)  # type: ignore[arg-type]
    assert rc == 0 and asked_secret and "DPAPI" in out[-1]
    run_cli(None, _args(action="list"), out=out.append, store=s)  # type: ignore[arg-type]
    assert "erp.college.edu as student01" in out[-1] and SECRET not in "\n".join(out)
    run_cli(None, _args(action="allow", name="College ERP", always=True), out=out.append, store=s)  # type: ignore[arg-type]
    assert s.get("College ERP").permission == "always"
    assert run_cli(None, _args(action="remove", name="College ERP"), out=out.append, store=s) == 0  # type: ignore[arg-type]
    assert s.count() == 0


def test_cli_refuses_plain_http_without_the_flag(tmp_path):
    out: list[str] = []
    rc = run_cli(None, _args(action="add", name="Old", site="http://erp.college.edu", username="u"),  # type: ignore[arg-type]
                 secret=lambda p: SECRET, out=out.append, store=store(tmp_path))
    assert rc == 1 and "plain http" in out[-1]
