"""Delta's own browser picks a free remote-control port (no fixed 9223), and a second manager on the same data
folder (Delta restarted) finds and reuses the browser Delta started instead of opening another copy (L16)."""

from __future__ import annotations

import asyncio
import contextlib
import os

import pytest

from delta.tools import browser
from delta.tools.browser import BrowserManager, find_browser, owned_browser_port

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DELTA_E2E") != "1", reason="real-browser e2e: set DELTA_E2E=1"),
]


async def _close(m: BrowserManager) -> None:
    await m.close()
    if m._reaper is not None:
        m._reaper.cancel()
        with contextlib.suppress(BaseException):
            await m._reaper


async def test_free_port_per_launch_and_reuse_after_restart(tmp_path, monkeypatch, site):
    if find_browser() is None:
        pytest.fail("DELTA_E2E=1 but no Chrome or Edge found")
    monkeypatch.setattr(browser, "PORT", 0)  # the shipped default: pick a free port
    data = tmp_path / "data"
    first = BrowserManager(data)
    second = BrowserManager(data)
    try:
        page = await first.page()
        await page.goto(f"{site}/index.html")
        assert first.port and first.port != browser.LEGACY_PORT
        assert await asyncio.to_thread(owned_browser_port, data) == first.port
        assert await asyncio.to_thread(owned_browser_port, tmp_path / "someone-else") is None
        # "Delta restarted": a new manager, same data folder. It must attach to the browser already running.
        again = await second.page()
        assert second.port == first.port and second._proc is None  # nothing new was started
        assert again is not None
    finally:
        with contextlib.suppress(Exception):
            if second._browser is not None:
                await second._browser.close()  # disconnect only: the first manager owns the process
            if second._pw is not None:
                await second._pw.stop()
        second._browser = second._pw = None
        await _close(first)
        await asyncio.sleep(0.5)
