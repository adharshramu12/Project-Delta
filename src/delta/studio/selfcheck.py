"""Delta tries the project's page itself: it opens the running preview in a hidden browser, lets it run, presses the
keys a game or app starts with, and reports every JavaScript error, failed file and console error it saw.

This is how a Build request finds and fixes its own bugs: check_preview runs it, the model reads the errors, fixes
the code and checks again. It uses the browser that is already on the PC (Edge or Chrome, the same one Delta's own
browser uses), started headless with a throw-away profile and no access to anything but the preview's address on
this PC: requests to any other host are blocked, so a page can never reach the internet or the dashboard from here.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from delta.system.logging import log

RUN_S = 3.0          # how long the page runs before and after the keys are pressed
KEYS = ("Space", "Enter", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown")  # what starts and moves most games
MAX_ITEMS = 12


@dataclass
class PageRun:
    ok: bool                      # the page could be opened at all
    title: str = ""
    errors: list[str] = field(default_factory=list)    # uncaught exceptions and console errors
    failed: list[str] = field(default_factory=list)    # files of the project the page asked for that did not load
    outside: list[str] = field(default_factory=list)   # addresses on the internet the page uses (not loaded here)
    text: str = ""                # the visible text after it ran
    canvas: bool = False          # the page draws on a canvas (a game, a chart)
    note: str = ""                # why it could not run, when ok is False

    def report(self) -> str:
        if not self.ok:
            return f"(Delta could not run the page in a browser: {self.note})"
        lines = [f"Ran the page in a browser for {RUN_S * 2:.0f}s and pressed {', '.join(KEYS)}."]
        lines.append("JavaScript errors: " + ("none" if not self.errors else ""))
        lines += [f"  - {e}" for e in self.errors[:MAX_ITEMS]]
        if self.failed:
            lines.append("Files of the project that did not load:")
            lines += [f"  - {f}" for f in self.failed[:MAX_ITEMS]]
        if self.outside:
            lines.append("The page also uses these internet addresses (not loaded in this offline check; if it needs "
                         "them, it works only online):")
            lines += [f"  - {f}" for f in self.outside[:MAX_ITEMS]]
        lines.append(f"Draws on a canvas: {'yes' if self.canvas else 'no'}")
        lines.append(f"Visible text after running: {self.text[:600] or '(none)'}")
        if self.errors or self.failed:
            lines.append("Fix the cause of each error above in the code, then check_preview again.")
        return "\n".join(lines)


def _local(url: str, origin: str) -> bool:
    parts = urlsplit(url)
    return url.startswith(("data:", "blob:", "about:")) or f"{parts.scheme}://{parts.netloc}" == origin


async def run_page(url: str) -> PageRun:
    """Open `url` (the preview, on this PC) headless, run it, press the usual keys, and collect what went wrong."""
    try:
        from playwright.async_api import async_playwright

        from delta.tools.browser import find_browser
    except ImportError:
        return PageRun(False, note="the browser automation is not installed")
    found = find_browser()
    if not found:
        return PageRun(False, note="no Edge or Chrome on this PC")
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    run = PageRun(True)
    with tempfile.TemporaryDirectory(prefix="delta-selfcheck-") as profile:
        try:
            async with async_playwright() as pw:
                ctx = await pw.chromium.launch_persistent_context(
                    profile, executable_path=found[1], headless=True, viewport={"width": 1100, "height": 760},
                    args=["--no-first-run", "--no-default-browser-check", "--disable-extensions",
                          "--disable-background-networking", "--disable-sync"])
                try:
                    async def guard(route: object) -> None:  # only the preview itself: nothing else on the web
                        req = route.request  # type: ignore[attr-defined]
                        if _local(req.url, origin):
                            await route.continue_()  # type: ignore[attr-defined]
                        else:
                            run.outside.append(req.url[:120])
                            await route.abort()  # type: ignore[attr-defined]

                    await ctx.route("**/*", guard)
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    page.on("pageerror", lambda e: run.errors.append(f"{type(e).__name__}: {str(e)[:300]}"))
                    # a file that failed to load is reported once, by name (not again as a console message)
                    page.on("console", lambda m: run.errors.append(f"console.error: {m.text[:300]}")
                            if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
                    page.on("response", lambda r: run.failed.append(
                        f"{urlsplit(r.url).path or '/'} (HTTP {r.status})")
                        if r.status >= 400 and _local(r.url, origin) else None)
                    await page.goto(url, wait_until="load", timeout=15_000)
                    await asyncio.sleep(RUN_S)
                    with contextlib.suppress(Exception):
                        await page.mouse.click(550, 380)
                    for key in KEYS:
                        with contextlib.suppress(Exception):
                            await page.keyboard.press(key)
                            await asyncio.sleep(0.15)
                    await asyncio.sleep(RUN_S)
                    run.title = (await page.title())[:120]
                    run.canvas = bool(await page.evaluate("document.querySelector('canvas') !== null"))
                    run.text = " ".join((await page.evaluate("document.body ? document.body.innerText : ''")).split())
                finally:
                    await ctx.close()
        except Exception as e:  # the check must never break a Build request
            log.info("studio.selfcheck_failed", error=str(e)[:300])
            return PageRun(False, note=f"{type(e).__name__}: {str(e)[:200]}")
    run.errors = list(dict.fromkeys(run.errors))
    run.failed = [f for f in dict.fromkeys(run.failed) if "favicon.ico" not in f]
    run.outside = list(dict.fromkeys(run.outside))
    return run
