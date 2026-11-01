"""Browser control through a dedicated, signed-in Chrome profile (Playwright over CDP).

Chrome refuses remote debugging on your *default* profile, so Delta has its own profile in
<data_dir>/chrome-profile. Sign in to your sites there once (scripts/setup_chrome_profile.ps1).
Chrome is launched only when a task needs it and closed after 5 idle minutes (~300 MB freed).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil
from pydantic import BaseModel, Field

from delta.system.logging import log
from delta.tools.base import Risk, Tool, ToolResult
from delta.tools.desktop import click_risk

CHROME_PATHS = [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"]
PORT = 9223
IDLE_CLOSE_S = 300

ELEMENTS_JS = """() => {
  const out = [];
  for (const el of document.querySelectorAll('a,button,input,textarea,select,[role=button],[role=link]')) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || out.length >= 80) continue;
    const label = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.name || '').trim();
    out.push(`[${el.tagName.toLowerCase()}${el.type ? ':' + el.type : ''}] ${label.slice(0, 70)}`);
  }
  return out;
}"""


def find_chrome() -> str | None:
    return next((p for p in (os.path.expandvars(x) for x in CHROME_PATHS) if Path(p).exists()), None)


def chrome_args(profile: Path, port: int = PORT) -> list[str]:
    return [f"--remote-debugging-port={port}", f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", "--disable-features=Translate"]


class BrowserManager:
    def __init__(self, data_dir: Path) -> None:
        self.profile = data_dir / "chrome-profile"
        self._proc: subprocess.Popen[bytes] | None = None
        self._pw: Any = None
        self._browser: Any = None
        self._lock = asyncio.Lock()
        self._last_used = 0.0
        self._reaper: asyncio.Task[None] | None = None
        self.current_url = ""

    async def page(self) -> Any:
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                await self._launch()
            self._last_used = time.monotonic()
            ctx = self._browser.contexts[0]
            return ctx.pages[-1] if ctx.pages else await ctx.new_page()

    async def _launch(self) -> None:
        from playwright.async_api import async_playwright

        endpoint = f"http://127.0.0.1:{PORT}"
        if not await self._alive(endpoint):
            chrome = find_chrome()
            if not chrome:
                raise RuntimeError("Google Chrome not found. Install Chrome or set it up in scripts/.")
            self.profile.mkdir(parents=True, exist_ok=True)
            # Popen only starts the process (non-blocking); Chrome must outlive any asyncio transport
            self._proc = subprocess.Popen([chrome, *chrome_args(self.profile)], close_fds=True)  # noqa: ASYNC220
            for _ in range(40):
                if await self._alive(endpoint):
                    break
                await asyncio.sleep(0.25)
            else:
                raise RuntimeError("Chrome did not open its debugging port")
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(endpoint)
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_when_idle())
        log.info("browser.started")

    @staticmethod
    async def _alive(endpoint: str) -> bool:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=1) as c:
                return (await c.get(f"{endpoint}/json/version")).status_code == 200
        except httpx.HTTPError:
            return False

    async def _reap_when_idle(self) -> None:
        while self._browser is not None:
            await asyncio.sleep(30)
            if time.monotonic() - self._last_used > IDLE_CLOSE_S:
                log.info("browser.idle_close")
                await self.close()
                return

    async def close(self) -> None:
        async with self._lock:
            with contextlib.suppress(Exception):
                if self._browser:
                    await self._browser.close()
            with contextlib.suppress(Exception):
                if self._pw:
                    await self._pw.stop()
            self._browser = self._pw = None
            if self._proc and self._proc.poll() is None:  # only kill the Chrome *we* started
                with contextlib.suppress(psutil.Error):
                    parent = psutil.Process(self._proc.pid)
                    for p in [*parent.children(recursive=True), parent]:
                        p.terminate()
            self._proc = None


async def _evidence(page: Any, mgr: BrowserManager) -> dict[str, Any]:
    mgr.current_url = page.url
    return {"url": page.url, "title": await page.title()}


class _BrowserTool[A: BaseModel](Tool[A]):
    def __init__(self, mgr: BrowserManager) -> None:
        self.mgr = mgr

    def domains(self, args: A) -> list[str]:
        return [self.mgr.current_url] if self.mgr.current_url.startswith("http") else []

    async def _guarded(self, coro_fn: Any) -> ToolResult:
        try:
            return await coro_fn()
        except Exception as e:  # playwright timeouts etc. are normal outcomes for the agent
            return ToolResult(False, f"{type(e).__name__}: {str(e).splitlines()[0][:300]}")


class OpenArgs(BaseModel):
    url: str


class BrowserOpen(_BrowserTool[OpenArgs]):
    name = "browser_open"
    description = "Open a URL in Delta's Chrome (signed in to the user's sites)."
    risk = Risk.ACT
    Args = OpenArgs

    def domains(self, args: OpenArgs) -> list[str]:
        return [args.url]

    async def run(self, args: OpenArgs) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            url = args.url if "://" in args.url else f"https://{args.url}"
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            ev = await _evidence(page, self.mgr)
            return ToolResult(True, f"opened {ev['title']} ({ev['url']})", ev)

        return await self._guarded(go)


class NoArgs(BaseModel):
    pass


class BrowserRead(_BrowserTool[NoArgs]):
    name = "browser_read"
    description = "Read the current browser page: title, URL, visible text and clickable elements."
    risk = Risk.READ
    Args = NoArgs

    async def run(self, args: NoArgs) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            text = (await page.inner_text("body"))[:12000]
            elements = await page.evaluate(ELEMENTS_JS)
            ev = await _evidence(page, self.mgr)
            body = f"{ev['title']} — {ev['url']}\n\n{text}\n\nINTERACTIVE:\n" + "\n".join(elements)
            return ToolResult(True, body, ev)

        return await self._guarded(go)


class ClickArgs(BaseModel):
    text: str = Field(description="Visible text or accessible name of the link/button")


class BrowserClick(_BrowserTool[ClickArgs]):
    name = "browser_click"
    description = "Click a link or button on the current page by its visible text."
    risk = Risk.ACT
    Args = ClickArgs

    def risk_for(self, args: ClickArgs) -> Risk:
        return click_risk(args.text)

    def summary(self, args: ClickArgs) -> str:
        return f"click '{args.text}' on {self.mgr.current_url or 'the page'}"

    async def run(self, args: ClickArgs) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            target = page.get_by_role("button", name=args.text).or_(page.get_by_role("link", name=args.text)) \
                .or_(page.get_by_text(args.text)).first
            await target.click(timeout=8000)
            await page.wait_for_load_state("domcontentloaded")
            ev = await _evidence(page, self.mgr)
            return ToolResult(True, f"clicked '{args.text}', now at {ev['url']}", ev)

        return await self._guarded(go)


class FillArgs(BaseModel):
    field: str = Field(description="Label or placeholder of the input")
    value: str


class BrowserFill(_BrowserTool[FillArgs]):
    name = "browser_fill"
    description = "Type into a form field found by its label or placeholder. Never for passwords or card numbers."
    risk = Risk.ACT
    Args = FillArgs

    def summary(self, args: FillArgs) -> str:
        return f"fill '{args.field}' on {self.mgr.current_url or 'the page'}"

    async def run(self, args: FillArgs) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            box = page.get_by_label(args.field).or_(page.get_by_placeholder(args.field)).first
            kind = ((await box.get_attribute("type")) or "").lower()
            auto = ((await box.get_attribute("autocomplete")) or "").lower()
            if kind == "password" or "cc-" in auto or "password" in auto:
                return ToolResult(False, "REFUSED: password/payment field. Ask the user to fill it in.")
            await box.fill(args.value, timeout=8000)
            return ToolResult(True, f"filled '{args.field}'", await _evidence(page, self.mgr))

        return await self._guarded(go)


class PressArgs(BaseModel):
    key: str = Field(description="e.g. Enter, Tab, Escape, PageDown")


class BrowserPress(_BrowserTool[PressArgs]):
    name = "browser_press"
    description = "Press a key in the browser page."
    risk = Risk.ACT
    Args = PressArgs

    def risk_for(self, args: PressArgs) -> Risk:
        return Risk.EXTERNAL if args.key.lower() in {"enter", "return"} else Risk.ACT

    async def run(self, args: PressArgs) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            await page.keyboard.press(args.key)
            return ToolResult(True, f"pressed {args.key}", await _evidence(page, self.mgr))

        return await self._guarded(go)


def browser_tools(mgr: BrowserManager) -> list[Tool]:
    return [BrowserOpen(mgr), BrowserRead(mgr), BrowserClick(mgr), BrowserFill(mgr), BrowserPress(mgr)]
