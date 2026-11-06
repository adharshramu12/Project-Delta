"""Browser choice on a real Windows PC: the stand-in tools drive Delta's private browser, its engine can be
picked, and "Sign in to my sites" opens a visible tab on the page asked for. Run with DELTA_E2E=1."""

from __future__ import annotations

import os
from typing import Any

import pytest

from delta.config import Settings
from delta.tools.browser import BrowserManager, OpenArgs, browser_tools, find_browser
from delta.tools.browser_choice import detect
from delta.tools.browser_switch import BrowserHub

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DELTA_E2E") != "1", reason="real-browser e2e: set DELTA_E2E=1"),
]


def hub_for(mgr: BrowserManager, tmp_path: Any, engine: str = "auto") -> BrowserHub:
    settings = Settings(data_dir=tmp_path)
    settings.browser.mode = "delta-profile"
    settings.browser.private_engine = engine  # type: ignore[assignment]
    hub = BrowserHub(settings, None)
    hub.add_private(browser_tools(mgr), mgr)
    return hub


def test_detection_sees_the_installed_browser() -> None:
    inst = detect()
    assert inst.any and inst.automation
    assert inst.engine_target("auto") == (find_browser() or ("",))[0]


async def test_stand_in_tools_drive_the_private_browser(mgr: BrowserManager, site: str, tmp_path: Any) -> None:
    hub = hub_for(mgr, tmp_path)
    tools = {t.name: t for t in hub.tools()}
    token = hub.begin_task("e2e", "open the search page")
    try:
        out = await tools["browser_open"].run(OpenArgs(url=f"{site}/search.html"))
    finally:
        hub.end_task("e2e", token)
    assert out.ok, out.output
    assert "Docs search" in out.output and mgr.running


async def test_signin_opens_the_page_asked_for(mgr: BrowserManager, site: str, tmp_path: Any) -> None:
    hub = hub_for(mgr, tmp_path)
    out = await hub.open_signin(f"{site}/login.html")
    assert out.ok, out.output
    assert "never type passwords" in out.output and mgr.hold_until > 0
    page = await mgr.page()
    assert any(p.url.endswith("/login.html") for p in page.context.pages)


async def test_edge_can_run_the_private_browser(mgr: BrowserManager, site: str, tmp_path: Any) -> None:
    if not detect().edge:
        pytest.skip("Microsoft Edge is not installed on this runner")
    hub = hub_for(mgr, tmp_path, engine="edge")
    tools = {t.name: t for t in hub.tools()}
    token = hub.begin_task("e2e-edge", "open the search page")
    try:
        out = await tools["browser_open"].run(OpenArgs(url=f"{site}/search.html"))
    finally:
        hub.end_task("e2e-edge", token)
    assert out.ok, out.output
    assert mgr.browser_name == "edge" and mgr.profile.name == "edge-profile"
