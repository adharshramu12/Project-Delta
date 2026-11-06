"""The dashboard in a real Chrome/Edge on a clean Windows PC: Delta's API server on a private port, the page opened
the way the Delta shortcut opens it (token in the URL fragment), and every view rendered without a script error or
a Content-Security-Policy violation. Nothing here reaches the internet."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DELTA_E2E") != "1", reason="real-browser e2e: set DELTA_E2E=1"),
]

VIEWS = ["tasks", "apps", "schedules", "documents", "memory", "settings", "chat"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def dashboard(tmp_path: Path) -> Iterator[tuple[str, str]]:
    """Delta's real API (create_app on a fresh data folder) served by uvicorn on 127.0.0.1."""
    import uvicorn

    from delta.api.server import create_app, load_token
    from delta.bootstrap import build
    from delta.config import Settings

    c = build(Settings(data_dir=tmp_path / "data"))
    token = load_token(tmp_path / "data")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(c, token), host="127.0.0.1", port=port, log_level="warning",
                                           lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    try:
        yield f"http://127.0.0.1:{port}", token
    finally:
        server.should_exit = True
        thread.join(timeout=10)


async def check_dashboard(page: Any, base: str, token: str) -> None:
    """Shared with local runs: opens the page and walks every view."""
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"script error: {e}"))
    page.on("console", lambda m: problems.append(f"console: {m.text}")
            if m.type == "error" and "Content Security Policy" in m.text else None)
    await page.goto(f"{base}/#token={token}")
    await page.wait_for_selector(".nav-item[data-go='chat'][aria-current='page']", timeout=15000)
    assert "token" not in page.url  # the token leaves the address bar at once
    # A fresh install opens setup. Close it: the chat then shows how to get started.
    with contextlib.suppress(Exception):
        await page.wait_for_selector("#setup[open]", timeout=5000)
        await page.click("#wclose")
    await page.wait_for_selector("#getstarted:not([hidden])", timeout=10000)
    await page.wait_for_function("window.Delta.conn === 'open'", timeout=15000)  # the live connection is up
    for view in VIEWS:
        await page.click(f".nav-item[data-go='{view}']")
        section = page.locator(f".views > .view[data-view='{view}']")
        await section.wait_for(state="visible", timeout=5000)
        await page.wait_for_function(
            "v => !document.querySelector(`.view[data-view='${v}'] [aria-busy='true']`)", arg=view, timeout=10000)
        assert await section.locator(".error-state").count() == 0, f"{view} shows an error"
        title = await page.text_content("#pagetitle")
        assert title and title.strip(), view
    # Settings loaded the real values; the theme toggle switches and comes back.
    await page.click(".nav-item[data-go='settings']")
    await page.wait_for_selector("input[name=s_autonomy]:checked", state="attached", timeout=10000)  # loads async
    assert await page.locator("input[name=s_autonomy]:checked").count() == 1
    before = await page.get_attribute("html", "data-mode")
    for _ in range(3):
        await page.click("#themebtn")
    assert await page.get_attribute("html", "data-mode") == before
    assert not problems, problems


async def test_dashboard_renders_every_view(mgr, dashboard):
    base, token = dashboard
    page = await mgr.page()
    await asyncio.wait_for(check_dashboard(page, base, token), timeout=120)


async def attach_and_send(page: Any, base: str, token: str, folder: Path) -> None:
    """Attach a picture and a PDF with the file picker, see them read on this PC, send them with a message, and see
    them in the sent message. No script error and no Content-Security-Policy violation on the way."""
    from delta.evals import attachment_fixtures as fixtures

    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"script error: {e}"))
    page.on("console", lambda m: problems.append(f"console: {m.text}")
            if m.type == "error" and "Content Security Policy" in m.text else None)
    picture, pdf = folder / "chart.png", folder / "report.pdf"
    picture.write_bytes(fixtures.png(640, 400, "Sales 2026"))
    pdf.write_bytes(fixtures.text_pdf([["Water report"], ["Budget 4 lakh"]]))
    await page.goto(f"{base}/#token={token}")
    await page.wait_for_selector(".nav-item[data-go='chat'][aria-current='page']", timeout=15000)
    with contextlib.suppress(Exception):
        await page.wait_for_selector("#setup[open]", timeout=5000)
        await page.click("#wclose")
    await page.set_input_files("#attachinput", [str(picture), str(pdf)])
    await page.wait_for_function(
        "() => document.querySelectorAll('#attachlist .achip').length === 2"
        " && !document.querySelector('#attachlist .achip.loading')", timeout=20000)
    chips = await page.locator("#attachlist .achip").all_inner_texts()
    assert any("chart.png" in t and "Picture, 640x400" in t for t in chips), chips
    assert any("report.pdf" in t and "2 pages" in t for t in chips), chips
    assert await page.locator("#attachlist .achip.bad").count() == 0
    await page.wait_for_selector("#attachlist .achip img", timeout=10000)  # the picture's preview (a blob: URL)
    await page.fill("#text", "What do these show?")
    await page.click("#sendbtn")
    await page.wait_for_selector(".msg.me .msg-files .achip", timeout=10000)
    assert await page.locator(".msg.me .msg-files .achip").count() == 2
    assert await page.locator("#attachlist").is_hidden()
    assert not problems, problems


async def test_attaching_files_in_the_dashboard(mgr, dashboard, tmp_path):
    base, token = dashboard
    page = await mgr.page()
    await asyncio.wait_for(attach_and_send(page, base, token, tmp_path), timeout=120)
