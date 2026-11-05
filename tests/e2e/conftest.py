"""Real-browser end-to-end fixtures: a local static site and a BrowserManager on a private port.

Hermetic: the site is served from tests/e2e/site on 127.0.0.1 and nothing touches the internet.
These tests only run with DELTA_E2E=1 (CI job e2e-windows); a plain `pytest` skips them."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from delta.tools import browser
from delta.tools.browser import BrowserManager, find_browser

SITE = Path(__file__).parent / "site"
SLOW_API_S = 0.8  # /slow/... answers after this delay, like a real API call behind a SPA


class _SiteHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/slow/"):
            time.sleep(SLOW_API_S)
            self.path = self.path[len("/slow"):]
        super().do_GET()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def site() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_SiteHandler, directory=str(SITE)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
async def mgr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[BrowserManager]:
    """Delta's own BrowserManager launching the installed Chrome/Edge (find_browser), on a private
    port so it never attaches to a Delta browser already running on this PC."""
    if find_browser() is None:
        pytest.fail("DELTA_E2E=1 but no Chrome or Edge found (find_browser); these tests need a real browser")
    monkeypatch.setattr(browser, "PORT", _free_port())
    m = BrowserManager(tmp_path / "data")
    try:
        yield m
    finally:
        await m.close()
        if m._reaper is not None:
            m._reaper.cancel()
            with contextlib.suppress(BaseException):
                await m._reaper
        await asyncio.sleep(0.5)  # let the browser process release its profile folder
