"""Saved-login captchas read by Delta itself (on by default), in a real Chrome/Edge against the fake ERP
(delta.evals.portal_fixture modes erp_noisy and erp_canvas: noisy Pillow captchas with lines and dots, a new random
answer per picture):

- a login saved with no captcha choice (the default) reads a noisy captcha with no approval and signs in; the box's
  real maxlength reaches the solver; an unreadable first picture is refreshed;
- a digits-only captcha drawn on a bare <canvas> next to a Yii-style verifyCode box is found, and OCR's O/l/S/Z
  mix-ups are fixed by the box's pattern;
- three unreadable pictures: the filled form waits and the picture comes back for the user; the reply signs in;
- the own-browser path (the CDP-attached stand-in) does the same;
- REAL Windows OCR, when this runner has an OCR language: the read rate on 20 noisy captchas is printed (the -rs / -s
  output of the e2e job), and a whole sign-in either succeeds by OCR alone or falls back to the picture, never to an
  AI call.

Made-up credentials only. Run with DELTA_E2E=1 pytest -m e2e."""

from __future__ import annotations

import io
import os
import time
from pathlib import Path
from typing import Any

import pytest

from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.evals.own_browser_standin import PlaywrightOwnDriver
from delta.evals.portal_fixture import USER, PortalSite, misread, noisy_captcha_png, sealers
from delta.logins.service import LoginService
from delta.logins.store import LoginStore
from delta.safety.audit import AuditLog
from delta.tools.browser import BrowserManager, settle
from delta.tools.captcha import CaptchaSolver, PlaywrightCaptchaPage, local_ocr

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DELTA_E2E") != "1", reason="real-browser e2e: set DELTA_E2E=1"),
]

PASSWORD = "E2e-Noisy-Canary-Pass-5527"


def site(mode: str) -> PortalSite:
    s = PortalSite(PASSWORD)
    s.state.mode = mode
    return s


def make(tmp_path: Path, erp: PortalSite, reads: list[str] | None = None, ocr: Any = None,
         seen: list[bytes] | None = None) -> LoginService:
    store = LoginStore(tmp_path / "logins", **sealers())
    store.add("Fixture ERP", erp.url, USER, PASSWORD, login_url=erp.url + "/", permission="always")  # no choice
    store.allow_own_browser("fixture_erp", "chrome")
    assert store.get("Fixture ERP").simple_captcha  # on by default
    queue = list(reads or [])

    async def stand_in(jpeg: bytes) -> str:  # reads the answer of the picture on screen, as OCR would
        assert jpeg[:2] == b"\xff\xd8"
        if seen is not None:
            seen.append(jpeg)
        text = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else "")
        return text.replace("{captcha}", erp.state.captcha_answer).replace("{captcha_ocr}",
                                                                          misread(erp.state.captcha_answer))

    async def vision(b64: str) -> str:
        raise AssertionError("no AI call for a captcha here")

    async def nobody(host: str, saved: str) -> bool:
        raise AssertionError("a sign-in never uses the on-PC hand-off for a simple captcha")

    solver = CaptchaSolver(allowed_sites=lambda: [], vision=vision, ask_human=nobody,
                           audit=AuditLog(tmp_path / "captcha.jsonl"), ocr=ocr or stand_in,
                           vision_ready=lambda: False)
    solver.origin_allowed = store.captcha_allowed
    svc = LoginService(store, AuditLog(tmp_path / "audit.jsonl"), None, captcha_solver=solver, settle=settle)

    async def handoff(kind: str, host: str, text: str) -> bool:
        raise AssertionError(f"no hand-off expected ({kind})")

    svc.handoff = handoff  # type: ignore[method-assign]
    return svc


def chat() -> Any:
    return CURRENT_REQUEST.set(TaskRequest("sign in", channel="api", chat_id="e2e-auto"))


def no_secret_anywhere(tmp_path: Path) -> None:
    for f in tmp_path.rglob("*"):
        if f.is_file() and f.suffix in (".jsonl", ".log", ".txt", ".json", ".db", ".bin"):
            assert PASSWORD.encode() not in f.read_bytes(), f"password found in {f}"


async def test_default_on_reads_a_noisy_captcha_with_no_approval(mgr: BrowserManager, tmp_path):
    seen: list[bytes] = []
    with site("erp_noisy") as erp:
        svc = make(tmp_path, erp, ["", "{captcha} -"], seen=seen)
        page = await mgr.page()
        token = chat()
        try:
            hints = None
            await page.goto(erp.url + "/")
            await page.wait_for_function("getComputedStyle(document.documentElement).visibility === 'visible'")
            hints = await PlaywrightCaptchaPage(page).field_hints("")
            result = await svc.sign_in("Fixture ERP", page)
        finally:
            CURRENT_REQUEST.reset(token)
        assert hints is not None and hints.get("maxlength") == "5"
        assert result.ok, result.message
        assert erp.state.logins == 1 and erp.state.captcha_wrong == 0 and len(erp.state.posts) == 1
        assert erp.state.captcha_views >= 2 and len(seen) == 2  # the unreadable picture was refreshed
        from PIL import Image

        shot = Image.open(io.BytesIO(seen[-1]))
        assert 100 <= shot.width <= 600 and 30 <= shot.height <= 200  # the captcha element only, any scaling
    no_secret_anywhere(tmp_path)


async def test_canvas_digits_captcha_next_to_a_verifycode_box(mgr: BrowserManager, tmp_path):
    with site("erp_canvas") as erp:
        svc = make(tmp_path, erp, ["{captcha_ocr}"])
        token = chat()
        try:
            result = await svc.sign_in("Fixture ERP", await mgr.page())
        finally:
            CURRENT_REQUEST.reset(token)
        assert result.ok, result.message
        assert erp.state.posts == [{"username": USER, "password": "<set>", "captcha": erp.state.captcha_answer}]
    no_secret_anywhere(tmp_path)


async def test_three_unreadable_pictures_then_the_user_and_the_reply_signs_in(mgr: BrowserManager, tmp_path):
    with site("erp_noisy") as erp:
        svc = make(tmp_path, erp, [""])
        page = await mgr.page()
        token = chat()
        try:
            asked = await svc.sign_in("Fixture ERP", page)
            assert asked.outcome == "needs_user" and asked.captcha_image and asked.captcha_image[:2] == b"\xff\xd8"
            assert "Reply with just the characters" in asked.message and erp.state.posts == []
            assert erp.state.captcha_views == 3
            assert svc.waiting_view("e2e-auto") is not None  # shown again if the dashboard reloads
            done = await svc.answer_captcha("e2e-auto", erp.state.captcha_answer)
        finally:
            CURRENT_REQUEST.reset(token)
        assert done.ok, done.message
        assert erp.state.logins == 1 and len(erp.state.posts) == 1
    no_secret_anywhere(tmp_path)


async def test_region_picture_when_no_picture_element_exists(mgr: BrowserManager):
    with site("erp_noisy") as erp:
        page = await mgr.page()
        await page.goto(erp.url + "/")
        await page.wait_for_function("getComputedStyle(document.documentElement).visibility === 'visible'")
        await page.evaluate("document.querySelector('#loginFormCaptcha-image').remove()")
        cpage = PlaywrightCaptchaPage(page)
        assert await cpage.screenshot("") is None
        band = await cpage.region_shot()
        assert band is not None and band[:2] == b"\xff\xd8"


async def test_own_browser_mode_reads_the_noisy_captcha(mgr: BrowserManager, tmp_path):
    with site("erp_noisy") as erp:
        svc = make(tmp_path, erp, ["{captcha}"])
        token = chat()
        try:
            result = await svc.sign_in_own("Fixture ERP", PlaywrightOwnDriver(mgr, settle))
        finally:
            CURRENT_REQUEST.reset(token)
        assert result.ok, result.message
        assert erp.state.logins == 1 and len(erp.state.posts) == 1
    no_secret_anywhere(tmp_path)


# --- real Windows OCR (skipped where the runner has none) ---------------------------------------------------------

async def _windows_ocr() -> Any:
    from delta.perception.ocr import WindowsOcr

    engine = WindowsOcr("auto")
    if not engine.platform_ok() or not await engine.probe():
        pytest.skip("no Windows OCR language on this runner")
    return local_ocr("auto")


async def test_real_windows_ocr_read_rate_on_noisy_captchas(capsys):
    from delta.tools.captcha_ocr import Hints, pick

    reader = await _windows_ocr()
    assert reader is not None
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    import random

    rng = random.Random(11)
    right, started = 0, time.monotonic()
    for i in range(20):
        answer = "".join(rng.choice(alphabet) for _ in range(5))
        got = pick(await reader(noisy_captcha_png(answer, i)), Hints(0, 5, ""))
        right += int((got or "").upper() == answer)
    took = time.monotonic() - started
    with capsys.disabled():
        print(f"\n[captcha-ocr] Windows OCR read {right}/20 noisy captchas exactly ({took / 20:.1f} s each)")
    assert took / 20 < 30  # one OCR start per picture, never one per pass


async def test_real_windows_ocr_sign_in_never_calls_an_ai(mgr: BrowserManager, tmp_path, capsys):
    reader = await _windows_ocr()
    with site("erp_noisy") as erp:
        svc = make(tmp_path, erp, ocr=reader)
        token = chat()
        try:
            result = await svc.sign_in("Fixture ERP", await mgr.page())
        finally:
            CURRENT_REQUEST.reset(token)
        with capsys.disabled():
            print(f"\n[captcha-ocr] real sign-in: {result.outcome}, pictures {erp.state.captcha_views}, "
                  f"submits {len(erp.state.posts)}")
        assert result.ok or (result.outcome == "needs_user" and result.captcha_image)
        assert svc.solver.vision_calls == 0 and erp.state.failed == 0
    no_secret_anywhere(tmp_path)
