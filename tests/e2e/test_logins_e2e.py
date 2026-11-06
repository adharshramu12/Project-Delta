"""Saved logins and watchers in a real Chrome/Edge against the fake college portal (delta.evals.portal_fixture).

Covers what only a real browser can show: the password is typed only on the exact saved origin (lookalike host,
http downgrade and a sign-in form inside a frame from another origin are refused before anything is typed), the
one-time code and captcha hand-offs, wrong passwords are not retried, and a watcher notices only new items, signs
in again after the session expires, says "couldn't read" after a redesign and reports a site that is down.
Run with DELTA_E2E=1 pytest -m e2e."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from delta.evals.portal_fixture import CAPTCHA, OTP, USER, PortalSite, sealers
from delta.logins.service import LoginService
from delta.logins.store import LoginStore
from delta.safety.audit import AuditLog
from delta.tools.browser import BrowserManager, settle
from delta.tools.captcha import CaptchaSolver
from delta.watchers.recipe import Recipe, Section
from delta.watchers.service import WatchService
from delta.watchers.store import WatchStore

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DELTA_E2E") != "1", reason="real-browser e2e: set DELTA_E2E=1"),
]

PASSWORD = "E2e-Canary-Pass-7731"




@pytest.fixture
def portal():
    with PortalSite(PASSWORD) as site:
        yield site


def make_service(tmp_path: Path, site: PortalSite, handoff: Any = None, solver: Any = None,
                 origin: str = "", login_url: str = "") -> LoginService:
    store = LoginStore(tmp_path / "logins", **sealers())
    store.add("Fixture ERP", origin or site.url, USER, PASSWORD, login_url=login_url or site.url + "/login",
              permission="always")
    svc = LoginService(store, AuditLog(tmp_path / "audit.jsonl"), None, captcha_solver=solver, settle=settle)
    calls: list[tuple[str, str]] = []

    async def default(kind: str, host: str, text: str) -> bool:
        calls.append((kind, host))
        return False

    svc.handoff = handoff or default  # type: ignore[method-assign]
    svc.handoffs = calls  # type: ignore[attr-defined]
    return svc


def no_secret_anywhere(tmp_path: Path) -> None:
    for f in tmp_path.rglob("*"):
        if f.is_file() and f.suffix in (".jsonl", ".log", ".txt", ".json", ".db"):
            assert PASSWORD.encode() not in f.read_bytes(), f"password found in {f}"


async def test_signs_in_on_the_exact_origin_and_masks_the_box(mgr: BrowserManager, portal: PortalSite, tmp_path):
    svc = make_service(tmp_path, portal)
    page = await mgr.page()
    result = await svc.sign_in("Fixture ERP", page)
    assert result.ok, result.message
    assert page.url.endswith("/dashboard") and portal.state.logins == 1
    assert portal.state.posts == [{"username": USER, "password": "<set>"}]  # nothing else was sent
    no_secret_anywhere(tmp_path)
    # signed in already: a second call types nothing
    again = await svc.sign_in("Fixture ERP", page)
    assert again.outcome == "already_signed_in" and len(portal.state.posts) == 1


async def test_password_box_is_masked_for_pictures(mgr: BrowserManager, portal: PortalSite):
    from delta.logins.signin import mask_secret_fields

    page = await mgr.page()
    await page.goto(portal.url + "/login")
    await page.fill("#password", "visible?")
    await page.evaluate("document.getElementById('password').type = 'text'")  # a "show password" button
    await mask_secret_fields(page)
    css = await page.evaluate("getComputedStyle(document.getElementById('password')).webkitTextSecurity")
    assert css == "disc"


async def test_wrong_password_is_reported_and_not_retried(mgr: BrowserManager, portal: PortalSite, tmp_path):
    portal.state.password = "something-else"
    svc = make_service(tmp_path, portal)
    result = await svc.sign_in("Fixture ERP", await mgr.page())
    assert result.outcome == "wrong_password" and portal.state.failed == 1


async def test_lookalike_origin_is_refused_before_typing(mgr: BrowserManager, portal: PortalSite, tmp_path):
    svc = make_service(tmp_path, portal)
    page = await mgr.page()
    await page.goto(portal.other + "/evil/login")  # same form, another origin (localhost vs 127.0.0.1)
    result = await svc.sign_in("Fixture ERP", page)
    assert result.outcome == "refused" and "phishing" in result.message
    assert portal.state.posts == [] and await page.input_value("#password") == ""


async def test_http_downgrade_is_refused(mgr: BrowserManager, portal: PortalSite, tmp_path):
    https = portal.url.replace("http://", "https://")
    svc = make_service(tmp_path, portal, origin=https, login_url=https + "/login")
    page = await mgr.page()
    await page.goto(portal.url + "/login")  # the same host over plain http
    result = await svc.sign_in("Fixture ERP", page)
    assert result.outcome == "refused" and "downgrade" in result.message and portal.state.posts == []


async def test_form_in_a_cross_origin_frame_is_refused(mgr: BrowserManager, portal: PortalSite, tmp_path):
    svc = make_service(tmp_path, portal, login_url=portal.url + "/frame")
    page = await mgr.page()
    await page.goto(portal.url + "/frame")
    await page.wait_for_timeout(500)
    result = await svc.sign_in("Fixture ERP", page)
    assert result.outcome == "refused" and "frame from another site" in result.message
    assert portal.state.posts == []


async def test_one_time_code_is_handed_to_the_user(mgr: BrowserManager, portal: PortalSite, tmp_path):
    portal.state.mode = "otp"
    page = await mgr.page()
    asked: list[str] = []

    async def user_types_code(kind: str, host: str, text: str) -> bool:
        asked.append(kind)
        await page.fill("#otp", OTP)  # the person, at the PC
        await page.click("button:has-text('Verify')")
        return True

    svc = make_service(tmp_path, portal, handoff=user_types_code)
    result = await svc.sign_in("Fixture ERP", page)
    assert result.ok, result.message
    assert asked == ["otp"] and page.url.endswith("/dashboard")


async def test_one_time_code_not_done_in_time_stops(mgr: BrowserManager, portal: PortalSite, tmp_path):
    portal.state.mode = "otp"
    svc = make_service(tmp_path, portal)
    result = await svc.sign_in("Fixture ERP", await mgr.page())
    assert result.outcome == "needs_user" and svc.handoffs == [("otp", "127.0.0.1")]  # type: ignore[attr-defined]


def solver(tmp_path: Path, allowed: list[str], human: Any = None, reads: list[str] | None = None) -> CaptchaSolver:
    replies = list(reads or [])

    async def vision(b64: str) -> str:
        return replies.pop(0) if replies else ""

    async def nobody(host: str, saved: str) -> bool:
        return False

    return CaptchaSolver(allowed_sites=lambda: allowed, vision=vision, ask_human=human or nobody,
                         audit=AuditLog(tmp_path / "captcha.jsonl"))


async def test_simple_captcha_on_an_allowed_site(mgr: BrowserManager, portal: PortalSite, tmp_path):
    portal.state.mode = "captcha"
    svc = make_service(tmp_path, portal, solver=solver(tmp_path, ["127.0.0.1"], reads=[CAPTCHA]))
    result = await svc.sign_in("Fixture ERP", await mgr.page())
    assert result.ok, result.message
    assert (tmp_path / "captcha.jsonl").read_text().count('"mode": "auto"') == 1


async def test_simple_captcha_elsewhere_goes_to_the_user(mgr: BrowserManager, portal: PortalSite, tmp_path):
    portal.state.mode = "captcha"
    page = await mgr.page()
    asked: list[str] = []

    async def human(host: str, saved: str) -> bool:
        asked.append(host)
        await page.fill("#captcha", CAPTCHA)  # the person types it on the PC
        return True

    svc = make_service(tmp_path, portal, solver=solver(tmp_path, [], human, reads=[CAPTCHA]))
    result = await svc.sign_in("Fixture ERP", page)
    assert result.ok, result.message
    assert asked == ["127.0.0.1"]  # never read automatically: the site is not allowed


async def test_recaptcha_is_handed_over_never_solved(mgr: BrowserManager, portal: PortalSite, tmp_path):
    portal.state.mode = "recaptcha"
    svc = make_service(tmp_path, portal)
    result = await svc.sign_in("Fixture ERP", await mgr.page())
    assert result.outcome == "needs_user" and "reCAPTCHA" in result.message
    assert svc.handoffs == [("challenge", "127.0.0.1")] and portal.state.posts == []  # type: ignore[attr-defined]


async def test_watcher_end_to_end(mgr: BrowserManager, portal: PortalSite, tmp_path):
    svc = make_service(tmp_path, portal)

    class Hub:
        pass

    hub = Hub()
    hub.mgr = mgr  # type: ignore[attr-defined]
    store = WatchStore(tmp_path / "watchers.db", **sealers())
    watch = WatchService(store, svc, hub, settle=settle)
    recipe = Recipe(sections=[Section(name="Upcoming exams", url="/exams", rows="table#exams tbody tr",
                                      key=["course", "date"]),
                              Section(name="Timetable", url="/timetable"),
                              Section(name="Notifications", url="/notifications", rows="ul.notices li")])
    w = watch.create("ERP", "Fixture ERP", ["exams", "timetable", "notifications"], recipe=recipe)

    first = await watch.check(w, trigger="schedule")
    assert first.status == "done" and "Now watching" in first.text and "Upcoming exams (2 items)" in first.text

    portal.state.exams.append(("CS399 Compiler Design", "2026-11-28", "10:00", "Hall C"))
    second = await watch.check(store.get(w.id), trigger="schedule")
    assert "Upcoming exams (1 new)" in second.text and "CS399 Compiler Design" in second.text
    assert "CS301" not in second.text  # only what is new
    assert store.pending(w.id, "offer")  # an exam: offered as a reminder, not made into one

    third = await watch.check(store.get(w.id), trigger="schedule")
    assert third.text == "" and third.summary == "no changes"

    portal.expire()  # the site signs everyone out
    logins = portal.state.logins
    fourth = await watch.check(store.get(w.id), trigger="schedule")
    assert fourth.status == "done" and portal.state.logins == logins + 1 and fourth.text == ""

    portal.state.layout = "v2"  # a redesign: the old recipe no longer finds the exams table
    fifth = await watch.check(store.get(w.id), trigger="schedule")
    assert "Couldn't read: Upcoming exams" in fifth.text and "CS399" not in fifth.text

    portal.state.down = True
    sixth = await watch.check(store.get(w.id), trigger="schedule")
    assert sixth.status == "failed" and "down" in sixth.summary
    assert not mgr.running  # the watcher closed the browser it opened
    no_secret_anywhere(tmp_path)
