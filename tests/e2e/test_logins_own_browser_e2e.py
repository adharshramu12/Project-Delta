"""Saved logins in the user's OWN browser mode, in a real Chrome/Edge, against the fake college portal
(delta.evals.portal_fixture). Run with DELTA_E2E=1 pytest -m e2e.

Two layers:
- The own-browser sign-in engine (delta.logins.own_browser) with the CDP-attached browser standing in for the user's
  everyday browser (delta.evals.own_browser_standin): one-time consent, exact-origin and frame refusals, a tab switch
  mid-fill, the password nowhere but the form. Runs wherever Chrome or Edge is installed.
- The real own-browser driver (delta.logins.own_uia, Windows UI Automation on a window of the installed browser's
  default profile). Runs only when that browser's page can be read through UI Automation on this machine; otherwise
  it is skipped with the reason (a CI runner may have no interactive desktop or a first-run screen in the way).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from delta.evals.own_browser_standin import PlaywrightOwnDriver
from delta.evals.portal_fixture import USER, PortalSite, sealers
from delta.logins.service import OWN_CONSENT_TOOL, LoginService
from delta.logins.store import LoginStore
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.tools.browser import BrowserManager, settle

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DELTA_E2E") != "1", reason="real-browser e2e: set DELTA_E2E=1"),
]

PASSWORD = "E2e-Own-Canary-Pass-5520"


@pytest.fixture
def portal():
    with PortalSite(PASSWORD) as site:
        yield site


def make_service(tmp_path: Path, site: PortalSite, login_url: str = "",
                 answer: bool = True) -> tuple[LoginService, list[Any]]:
    store = LoginStore(tmp_path / "logins", **sealers())
    store.add("Fixture ERP", site.url, USER, PASSWORD, login_url=login_url or site.url + "/login",
              permission="always")
    broker = ApprovalBroker(timeout_s=10)
    asked: list[Any] = []

    async def present(req: Any) -> None:
        asked.append(req)
        asyncio.get_running_loop().call_soon(broker.answer, req.id, answer, "dashboard", False)

    broker.add_presenter("e2e", present)
    return LoginService(store, AuditLog(tmp_path / "audit.jsonl"), broker, settle=settle), asked


def no_secret_anywhere(tmp_path: Path) -> None:
    for f in tmp_path.rglob("*"):
        if f.is_file() and f.suffix in (".jsonl", ".log", ".txt", ".json", ".db", ".bin"):
            assert PASSWORD.encode() not in f.read_bytes(), f"password found in {f}"


async def test_own_browser_fill_asks_once_then_signs_in(mgr: BrowserManager, portal: PortalSite, tmp_path):
    svc, asked = make_service(tmp_path, portal)
    page = await mgr.page()
    result = await svc.sign_in_own("Fixture ERP", PlaywrightOwnDriver(mgr, settle))
    assert result.ok, result.message
    assert page.url.endswith("/dashboard") and portal.state.posts == [{"username": USER, "password": "<set>"}]
    assert [a.tool for a in asked] == [OWN_CONSENT_TOOL] and "Google Chrome" in asked[0].summary
    again = await svc.sign_in_own("Fixture ERP", PlaywrightOwnDriver(mgr, settle))
    assert again.outcome == "already_signed_in" and len(asked) == 1 and len(portal.state.posts) == 1
    no_secret_anywhere(tmp_path)


async def test_own_browser_refuses_a_lookalike(mgr: BrowserManager, portal: PortalSite, tmp_path):
    svc, _ = make_service(tmp_path, portal)
    page = await mgr.page()
    await page.goto(portal.other + "/evil/login")
    result = await svc.sign_in_own("Fixture ERP", PlaywrightOwnDriver(mgr, settle))
    assert result.outcome == "refused" and "phishing" in result.message
    assert portal.state.posts == [] and await page.input_value("#password") == ""


async def test_own_browser_refuses_a_cross_origin_frame(mgr: BrowserManager, portal: PortalSite, tmp_path):
    svc, _ = make_service(tmp_path, portal, login_url=portal.url + "/frame")
    page = await mgr.page()
    await page.goto(portal.url + "/frame")
    await page.wait_for_timeout(500)
    result = await svc.sign_in_own("Fixture ERP", PlaywrightOwnDriver(mgr, settle))
    assert result.outcome == "refused" and "frame from another site" in result.message
    assert portal.state.posts == []


async def test_own_browser_tab_switch_mid_fill_aborts(mgr: BrowserManager, portal: PortalSite, tmp_path):
    svc, _ = make_service(tmp_path, portal)
    page = await mgr.page()
    await page.goto(portal.url + "/login")
    driver = PlaywrightOwnDriver(mgr, settle)
    real_set = driver.set_value

    async def user_switches_tab(f: Any, value: str, secret: bool) -> None:
        await real_set(f, value, secret)
        if f.kind == "user" and value:
            mgr.active = await page.context.new_page()  # the user opens another tab mid-fill

    driver.set_value = user_switches_tab  # type: ignore[method-assign]
    result = await svc.sign_in_own("Fixture ERP", driver)
    assert result.outcome == "aborted" and "switched to another tab" in result.message
    assert portal.state.posts == []
    assert await page.input_value("#username") == "" and await page.input_value("#password") == ""
    no_secret_anywhere(tmp_path)


# --- the real driver: Windows UI Automation on the installed browser's own window -----------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="UI Automation is Windows only")
async def test_real_own_browser_through_ui_automation(portal: PortalSite, tmp_path):
    try:
        from delta.logins.own_uia import UIAOwnDriver
        from delta.tools.desktop import DesktopController
        from delta.tools.user_browser import UserBrowser
    except ImportError as e:  # pragma: no cover - the desktop extras are part of the Windows install
        pytest.skip(f"desktop control is not installed: {e}")
    ctl = DesktopController()
    ub = UserBrowser(ctl)
    if not ub.exe:
        pytest.skip("no Chrome or Edge installed for the user's own browser")
    driver = UIAOwnDriver(ub)
    try:
        try:
            await asyncio.wait_for(driver.ensure_window(portal.url + "/login"), 40)
            url = await asyncio.wait_for(driver.top_url(), 20)
        except Exception as e:
            pytest.skip(f"the browser's page is not readable through UI Automation here ({type(e).__name__})")
        if not url.startswith(portal.url):
            pytest.skip(f"UI Automation did not show the portal page (it read {url[:60]!r}): a first-run screen or "
                        "no interactive desktop on this machine")
        svc, asked = make_service(tmp_path, portal)
        result = await asyncio.wait_for(svc.sign_in_own("Fixture ERP", driver), 120)
        assert result.ok, result.message
        assert portal.state.posts == [{"username": USER, "password": "<set>"}] and len(asked) == 1
        no_secret_anywhere(tmp_path)
    finally:
        with contextlib.suppress(Exception):
            import win32con
            import win32gui

            if ub.handle:
                win32gui.PostMessage(ub.handle, win32con.WM_CLOSE, 0, 0)
        ctl.close()
