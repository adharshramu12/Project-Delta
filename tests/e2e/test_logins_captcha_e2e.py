"""A sign-in page built like a real college ERP (delta.evals.portal_fixture mode erp_captcha: the page stays hidden
until a late stylesheet loads, floating labels, LoginForm[...] names, a captcha box whose placeholder says "Enter
verification Code", a hidden "Enter MFA Code" box, "The verification code is incorrect."), in a real Chrome/Edge:

- the login's captcha switch on: read on this PC (an OCR stand-in), a rejected captcha re-read, signed in, never an
  OTP hand-off and never the password re-sent after the site said the PASSWORD was wrong;
- the switch off: the form is filled and kept, the captcha picture comes back for the user, whose reply signs in; a
  wrong reply brings the new picture;
- the same in the user's own browser mode (the CDP-attached stand-in for their everyday Chrome/Edge).

Made-up credentials only. Run with DELTA_E2E=1 pytest -m e2e."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.evals import portal_fixture
from delta.evals.own_browser_standin import PlaywrightOwnDriver
from delta.evals.portal_fixture import CAPTCHA, USER, PortalSite, sealers
from delta.logins.service import LoginService
from delta.logins.store import LoginStore
from delta.safety.audit import AuditLog
from delta.tools.browser import BrowserManager, settle
from delta.tools.captcha import CaptchaSolver

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DELTA_E2E") != "1", reason="real-browser e2e: set DELTA_E2E=1"),
]

PASSWORD = "E2e-Erp-Canary-Pass-8842"


@pytest.fixture
def erp():
    with PortalSite(PASSWORD) as site:
        site.state.mode = "erp_captcha"
        yield site


def make(tmp_path: Path, site: PortalSite, switch: bool, reads: list[str] | None = None) -> LoginService:
    store = LoginStore(tmp_path / "logins", **sealers())
    store.add("Fixture ERP", site.url, USER, PASSWORD, login_url=site.url + "/", permission="always",
              simple_captcha=switch)
    store.allow_own_browser("fixture_erp", "chrome")
    queue = list(reads or [])

    async def ocr(jpeg: bytes) -> str:  # stands in for Windows OCR; the picture itself must be real
        assert jpeg[:2] == b"\xff\xd8"
        return queue.pop(0) if queue else ""

    async def nobody(host: str, saved: str) -> bool:
        raise AssertionError("a sign-in never uses the on-PC hand-off for a simple captcha")

    solver = CaptchaSolver(allowed_sites=lambda: [], vision=None, ask_human=nobody,
                           audit=AuditLog(tmp_path / "captcha.jsonl"), ocr=ocr)
    solver.origin_allowed = store.captcha_allowed
    svc = LoginService(store, AuditLog(tmp_path / "audit.jsonl"), None, captcha_solver=solver, settle=settle)
    handed: list[str] = []

    async def handoff(kind: str, host: str, text: str) -> bool:
        handed.append(kind)
        return False

    svc.handoff = handoff  # type: ignore[method-assign]
    svc.handed = handed  # type: ignore[attr-defined]
    return svc


def no_secret_anywhere(tmp_path: Path) -> None:
    for f in tmp_path.rglob("*"):
        if f.is_file() and f.suffix in (".jsonl", ".log", ".txt", ".json", ".db", ".bin"):
            data = f.read_bytes()
            assert PASSWORD.encode() not in data, f"password found in {f}"
            assert f"\"{CAPTCHA}\"".encode() not in data, f"captcha answer found in {f}"


def chat(text: str = "sign in") -> Any:
    return CURRENT_REQUEST.set(TaskRequest(text, channel="api", chat_id="e2e-chat"))


async def test_switch_on_reads_the_captcha_and_rereads_a_rejected_one(mgr: BrowserManager, erp: PortalSite,
                                                                      tmp_path):
    svc = make(tmp_path, erp, switch=True, reads=["WRONG7", CAPTCHA])
    token = chat()
    try:
        result = await svc.sign_in("Fixture ERP", await mgr.page())
    finally:
        CURRENT_REQUEST.reset(token)
    assert result.ok, result.message
    assert erp.state.logins == 1 and erp.state.captcha_wrong == 1 and erp.state.failed == 0
    assert erp.state.posts == [{"username": USER, "password": "<set>", "captcha": "WRONG7"},
                               {"username": USER, "password": "<set>", "captcha": CAPTCHA}]
    assert svc.handed == []  # type: ignore[attr-defined]  # never an "OTP" hand-off for the captcha box
    no_secret_anywhere(tmp_path)


async def test_switch_off_sends_the_picture_and_the_reply_signs_in(mgr: BrowserManager, erp: PortalSite, tmp_path):
    svc = make(tmp_path, erp, switch=False, reads=[CAPTCHA])
    page = await mgr.page()
    token = chat()
    try:
        asked = await svc.sign_in("Fixture ERP", page)
        assert asked.outcome == "needs_user" and asked.captcha_image and asked.captcha_image[:2] == b"\xff\xd8"
        assert "Reply with just the characters" in asked.message and erp.state.posts == []
        assert await page.input_value("#loginFormPasswordID") != ""  # the filled form waits for the answer
        wrong = await svc.answer_captcha("e2e-chat", "NOPE1")
        assert wrong.outcome == "needs_user" and wrong.captcha_image and erp.state.captcha_wrong == 1
        done = await svc.answer_captcha("e2e-chat", CAPTCHA)
    finally:
        CURRENT_REQUEST.reset(token)
    assert done.ok, done.message
    assert erp.state.logins == 1 and erp.state.failed == 0 and page.url.endswith("/dashboard")
    assert svc.handed == []  # type: ignore[attr-defined]
    no_secret_anywhere(tmp_path)


async def test_a_page_hidden_until_its_stylesheet_loads_is_not_taken_for_signed_in(mgr: BrowserManager,
                                                                                    erp: PortalSite, tmp_path,
                                                                                    monkeypatch):
    monkeypatch.setattr(portal_fixture, "LATE_CSS_S", 3.0)
    svc = make(tmp_path, erp, switch=True, reads=[CAPTCHA])
    result = await svc.sign_in("Fixture ERP", await mgr.page())
    assert result.outcome != "already_signed_in", result.message
    assert result.ok, result.message


async def test_wrong_password_on_a_captcha_form_is_never_retried(mgr: BrowserManager, erp: PortalSite, tmp_path):
    svc = make(tmp_path, erp, switch=True, reads=[CAPTCHA, CAPTCHA, CAPTCHA])
    erp.state.password = "Something-Else-1"
    result = await svc.sign_in("Fixture ERP", await mgr.page())
    assert result.outcome == "wrong_password" and erp.state.failed == 1 and len(erp.state.posts) == 1


async def test_own_browser_mode_reads_the_captcha_too(mgr: BrowserManager, erp: PortalSite, tmp_path):
    svc = make(tmp_path, erp, switch=True, reads=["WRONG7", CAPTCHA])
    token = chat()
    try:
        result = await svc.sign_in_own("Fixture ERP", PlaywrightOwnDriver(mgr, settle))
    finally:
        CURRENT_REQUEST.reset(token)
    assert result.ok, result.message
    assert erp.state.logins == 1 and erp.state.captcha_wrong == 1 and len(erp.state.posts) == 2
    no_secret_anywhere(tmp_path)


async def test_own_browser_mode_switch_off_parks_and_the_reply_signs_in(mgr: BrowserManager, erp: PortalSite,
                                                                        tmp_path):
    svc = make(tmp_path, erp, switch=False)
    token = chat()
    try:
        asked = await svc.sign_in_own("Fixture ERP", PlaywrightOwnDriver(mgr, settle))
        assert asked.outcome == "needs_user" and asked.captcha_image and erp.state.posts == []
        done = await svc.answer_captcha("e2e-chat", CAPTCHA)
    finally:
        CURRENT_REQUEST.reset(token)
    assert done.ok, done.message
    assert erp.state.logins == 1 and len(erp.state.posts) == 1
