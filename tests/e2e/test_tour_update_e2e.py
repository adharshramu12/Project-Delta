"""The first-run tour and the update banner in a real Chrome/Edge on a clean Windows PC.

Delta's real API on a private port, with GitHub replaced by a stand-in on 127.0.0.1 (DELTA_UPDATE_API /
DELTA_UPDATE_RAW) that offers a newer version, and the update launcher replaced by a recorder (nothing is installed;
`delta update`'s real run is in the fresh-user CI journey). Checks: the tour appears after setup, moves with the
arrow keys, spotlights the real chat box, skips with Esc, never comes back by itself, and replays from Settings and
from /tour; the banner shows the new version and its notes as text, Skip this version sticks, Update now asks first
and then starts the update of exactly the checked commit and files, What's new shows once after an update, and an
update that never closes Delta says so after 3 minutes. No script error and no Content-Security-Policy violation
anywhere. DELTA_SHOTS=<folder> saves screenshots."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DELTA_E2E") != "1", reason="real-browser e2e: set DELTA_E2E=1"),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def serve_delta(data: Path, *, tour_done: bool, newer: str | None) -> Iterator[SimpleNamespace]:
    """Delta's API (create_app, fresh data folder with setup completed) and, if `newer`, a stand-in GitHub whose
    latest release is that version. Restores every setting it changes."""
    import uvicorn
    from fake_github import FakeGitHub

    from delta.api.server import create_app, load_token
    from delta.bootstrap import build
    from delta.config import Settings, write_user_settings
    from delta.updates import install

    saved_env = {k: os.environ.get(k) for k in ("DELTA_NO_UPDATE_CHECK", "DELTA_UPDATE_API", "DELTA_UPDATE_RAW")}
    saved = {k: getattr(install, k) for k in ("launch", "supported", "is_dev_checkout")}
    launched: list[Any] = []
    gh = FakeGitHub()
    ghserver = None
    try:
        if newer:
            gh.release(f"v{newer}", body=f"## Delta {newer}\n- A guided tour for new users\n- Update banner <b>with</b>"
                                         " notes\n- Faster start\n- Fourth line")
            ghserver, base = gh.serve()
            os.environ.update(DELTA_UPDATE_API=base, DELTA_UPDATE_RAW=base)
            os.environ.pop("DELTA_NO_UPDATE_CHECK", None)
        install.launch = launched.append  # type: ignore[assignment]
        install.supported = lambda: True  # type: ignore[assignment]
        install.is_dev_checkout = lambda root=install.ROOT: False  # type: ignore[assignment]
        write_user_settings(data, {"setup": {"completed": True}, "tour": {"done": tour_done}})
        settings = Settings(data_dir=data)
        settings.tour.done = tour_done
        c = build(settings)
        if newer:
            c.updates.checker.check(force=True)  # what the background check does after start-up
        token = load_token(data)
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
            yield SimpleNamespace(base=f"http://127.0.0.1:{port}", token=token, c=c, gh=gh, launched=launched)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
    finally:
        if ghserver is not None:
            ghserver.shutdown()
            ghserver.server_close()
        for k, v in saved.items():
            setattr(install, k, v)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def watch(page: Any) -> list[str]:
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"script error: {e}"))
    page.on("console", lambda m: problems.append(f"console: {m.text}")
            if m.type == "error" and "Content Security Policy" in m.text else None)
    return problems


async def shot(page: Any, name: str) -> None:
    folder = os.environ.get("DELTA_SHOTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(Path(folder) / name))


async def open_dashboard(page: Any, d: SimpleNamespace) -> None:
    await page.goto(f"{d.base}/#token={d.token}")
    await page.wait_for_selector(".nav-item[data-go='chat'][aria-current='page']", timeout=15000)


async def tour_flow(page: Any, d: SimpleNamespace) -> None:
    problems = watch(page)
    await page.set_viewport_size({"width": 1280, "height": 800})
    await open_dashboard(page, d)
    card = page.locator(".tour-card")
    await card.wait_for(state="visible", timeout=15000)
    assert await page.locator("#tour_title").text_content() == "Welcome to Delta"
    assert "open source AI agent" in (await page.locator("#tour_body").text_content() or "")
    assert await card.get_attribute("role") == "dialog" and await card.get_attribute("aria-modal") == "true"
    assert await page.evaluate("document.activeElement && document.activeElement.id") == "tour_title"
    await shot(page, "tour-welcome.png")
    await page.keyboard.press("ArrowRight")
    await page.wait_for_function("document.getElementById('tour_title').textContent === 'Ask in plain words'")
    await page.wait_for_timeout(400)  # the spotlight's short move (none with reduced motion)
    spot = await page.locator(".tour-spot").bounding_box()
    box = await page.locator("#composer").bounding_box()
    assert spot and box and spot["x"] <= box["x"] and spot["y"] <= box["y"]
    assert spot["x"] + spot["width"] >= box["x"] + box["width"]
    assert spot["y"] + spot["height"] >= box["y"] + box["height"]
    cardbox = await card.bounding_box()
    assert cardbox and cardbox["y"] + cardbox["height"] <= box["y"]  # above the chat box, not over it
    await shot(page, "tour-step.png")
    # Tab stays in the card
    for _ in range(5):
        await page.keyboard.press("Tab")
        assert await page.evaluate("!!document.activeElement.closest('.tour-card')")
    await page.keyboard.press("ArrowLeft")
    await page.wait_for_function("document.getElementById('tour_title').textContent === 'Welcome to Delta'")
    await page.keyboard.press("Escape")
    await card.wait_for(state="detached", timeout=5000)
    await page.wait_for_function("window.Delta.view === 'chat'")
    for _ in range(50):
        if d.c.settings.tour.done:
            break
        await asyncio.sleep(0.1)
    assert d.c.settings.tour.done, "skipping saves the tour as done on the PC"
    # not again by itself
    await page.reload()
    await page.wait_for_selector(".nav-item[data-go='chat'][aria-current='page']", timeout=15000)
    await page.wait_for_timeout(1500)
    assert await page.locator(".tour-card").count() == 0
    # replay from Settings, through every step to Done
    await page.click(".nav-item[data-go='settings']")
    await page.click("#tour_start")
    await card.wait_for(state="visible", timeout=5000)
    titles = []
    for _ in range(12):
        await page.wait_for_timeout(250)
        if not await card.count():
            break
        titles.append(await page.locator("#tour_title").text_content())
        if titles[-1] == "Saved logins":
            await page.emulate_media(color_scheme="dark")
            await page.evaluate("window.DeltaTheme.set('dark')")
            await page.wait_for_timeout(300)
            await shot(page, "tour-settings-dark.png")
            await page.evaluate("window.DeltaTheme.set('system')")
        await page.locator(".tour-card .btn.primary").click()
    assert await card.count() == 0
    assert titles[0] == "Welcome to Delta" and titles[-1] == "Phone and start-up" and len(titles) == 9, titles
    await page.wait_for_function("window.Delta.view === 'settings'")  # back where it was started
    # replay from the chat box
    await page.click(".nav-item[data-go='chat']")
    await page.fill("#text", "/tour")
    await page.keyboard.press("Enter")
    await card.wait_for(state="visible", timeout=5000)
    assert await page.locator(".msg.me").count() == 0  # /tour is not sent to Delta as a message
    await page.keyboard.press("Escape")
    await card.wait_for(state="detached", timeout=5000)
    # a narrow window: steps whose element is off screen are skipped, never stuck
    await page.set_viewport_size({"width": 600, "height": 760})
    await page.fill("#text", "/tour")
    await page.keyboard.press("Enter")
    await card.wait_for(state="visible", timeout=5000)
    seen = []
    for _ in range(20):
        await page.wait_for_timeout(700)
        if not await card.count():
            break
        seen.append(await page.locator("#tour_title").text_content())
        await page.keyboard.press("ArrowRight")
    assert await card.count() == 0 and "Watch your tasks" not in seen and "Ask in plain words" in seen, seen
    await page.set_viewport_size({"width": 1280, "height": 800})
    assert not problems, problems


async def update_flow(page: Any, d: SimpleNamespace, newer: str) -> None:
    problems = watch(page)
    await page.set_viewport_size({"width": 1280, "height": 800})
    await open_dashboard(page, d)
    ban = page.locator("#updbanner")
    await ban.wait_for(state="visible", timeout=15000)
    text = await ban.inner_text()
    assert f"Delta {newer} is out" in text and "A guided tour for new users" in text, text
    assert "<b>" not in text and "Fourth line" not in text  # cleaned text, three highlights at most
    assert await ban.locator("b").count() == 1  # only the title is bold: nothing from GitHub became markup
    await shot(page, "update-banner.png")
    await ban.get_by_role("button", name="What's new").click()
    await page.wait_for_selector("#sheet[open]")
    assert "Fourth line" in (await page.locator("#sheet_body").inner_text())
    await page.click("#sheet_close")
    # Update now asks first; Cancel changes nothing
    await ban.get_by_role("button", name="Update now").click()
    await page.wait_for_selector("#confirm[open]")
    assert newer in (await page.locator("#cf_title").text_content() or "")
    await page.click("#cf_no")
    await page.wait_for_timeout(300)
    assert not d.launched
    await ban.get_by_role("button", name="Update now").click()
    await page.wait_for_selector("#confirm[open]")
    await page.click("#cf_yes")
    await page.wait_for_selector(f"text=Updating to Delta {newer}", timeout=15000)
    await shot(page, "update-started.png")
    (target,) = d.launched
    assert target.sha == d.gh.refs[f"v{newer}"] and target.version == newer
    files = d.gh.files[target.sha]
    import hashlib

    assert target.pyproject_sha256 == hashlib.sha256(files["pyproject.toml"].encode()).hexdigest()
    assert target.lock_sha256 == hashlib.sha256(files["uv.lock"].encode()).hexdigest()
    assert not problems, problems


async def skip_and_whats_new_flow(page: Any, d: SimpleNamespace, newer: str) -> None:
    import delta.updates.checker as checker_mod

    problems = watch(page)
    await open_dashboard(page, d)
    ban = page.locator("#updbanner")
    await ban.wait_for(state="visible", timeout=15000)
    await ban.get_by_role("button", name="Skip this version").click()
    await ban.wait_for(state="hidden", timeout=5000)
    await page.reload()
    await page.wait_for_selector(".nav-item[data-go='chat'][aria-current='page']", timeout=15000)
    await page.wait_for_timeout(1500)
    assert await ban.is_hidden()
    # Settings > Updates and help says what is going on
    await page.click(".nav-item[data-go='settings']")
    await page.wait_for_function(f"document.getElementById('u_state').textContent.includes('{newer}')", timeout=10000)
    # after an update: What's new once
    d.c.updates.checker.update_state(whats_new={"version": checker_mod.__version__, "notes": "- A guided tour",
                                                "highlights": ["A guided tour"], "url": ""})
    await page.reload()
    await page.wait_for_selector("#sheet[open]", timeout=15000)
    assert f"What's new in Delta {checker_mod.__version__}" in (await page.locator("#sheet_title").text_content() or "")
    assert "null" not in (await page.locator("#sheet_body").inner_text())
    await shot(page, "whats-new.png")
    await page.click("#sheet_close")
    await page.wait_for_timeout(500)
    await page.reload()
    await page.wait_for_selector(".nav-item[aria-current='page']", timeout=15000)
    await page.wait_for_timeout(1500)
    assert not await page.locator("#sheet[open]").count()
    assert not problems, problems


async def update_stopped_flow(page: Any, d: SimpleNamespace, newer: str) -> None:
    """The launcher is a recorder, so Delta never closes: like an installer that stopped at its download check.
    After 3 minutes (the page's clock moved on) the Updating screen says so and offers the way back."""
    problems = watch(page)
    await page.set_viewport_size({"width": 1280, "height": 800})
    await page.clock.install()  # real time keeps flowing; fast_forward below skips the 3 minutes
    await open_dashboard(page, d)
    ban = page.locator("#updbanner")
    await ban.wait_for(state="visible", timeout=15000)
    await ban.get_by_role("button", name="Update now").click()
    await page.wait_for_selector("#confirm[open]")
    await page.click("#cf_yes")
    await page.wait_for_selector(f"text=Updating to Delta {newer}", timeout=15000)
    assert len(d.launched) == 1
    assert await page.locator("#upd_back").count() == 0  # not before the 3 minutes
    await page.clock.fast_forward("03:05")
    back = page.locator("#upd_back")
    await back.wait_for(state="visible", timeout=20000)
    text = await page.locator("main[role='status']").inner_text()
    assert "The update stopped before changing anything" in text and "delta-install.log" in text, text
    await shot(page, "update-stopped.png")
    await back.click()
    await page.wait_for_selector(".nav-item[data-go='chat'][aria-current='page']", timeout=15000)
    assert not problems, problems


async def test_first_run_tour(mgr, tmp_path):
    with serve_delta(tmp_path / "data", tour_done=False, newer=None) as d:
        page = await mgr.page()
        await asyncio.wait_for(tour_flow(page, d), timeout=180)


async def test_update_banner_and_update_now(mgr, tmp_path):
    with serve_delta(tmp_path / "data", tour_done=True, newer="9.9.9") as d:
        page = await mgr.page()
        await asyncio.wait_for(update_flow(page, d, "9.9.9"), timeout=120)


async def test_skip_this_version_and_whats_new(mgr, tmp_path):
    with serve_delta(tmp_path / "data", tour_done=True, newer="9.9.9") as d:
        page = await mgr.page()
        await asyncio.wait_for(skip_and_whats_new_flow(page, d, "9.9.9"), timeout=120)


async def test_update_that_never_closes_delta_says_it_stopped(mgr, tmp_path):
    with serve_delta(tmp_path / "data", tour_done=True, newer="9.9.9") as d:
        page = await mgr.page()
        await asyncio.wait_for(update_stopped_flow(page, d, "9.9.9"), timeout=120)
