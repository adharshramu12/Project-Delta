"""Browser control through Delta's own profile in the installed browser (Playwright over CDP).

Works out of the box: Google Chrome if installed, otherwise Microsoft Edge, which every
Windows 10/11 PC has. Nothing is downloaded. Browsers refuse remote control of your *default*
profile, so Delta keeps its own profile in <data_dir>/<browser>-profile (sign in to sites there
once if you want Delta to use them). The browser starts only when a task needs it and closes
after 15 idle minutes (~300 MB freed).

Browsing (open, search, read, click links, scroll, type into search boxes) runs without asking.
Clicking things like Send/Buy/Delete, submitting forms and anything with passwords or payments
still asks: those cannot be undone.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil
from pydantic import BaseModel, Field

from delta.system.logging import log
from delta.tools.base import Risk, Tool, ToolResult
from delta.tools.desktop import click_risk

BROWSERS = [  # first one found is used
    ("chrome", [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"]),
    ("edge", [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
              r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"]),
]
PORT = 9223
IDLE_CLOSE_S = 900
SEARCH_URL = "https://duckduckgo.com/?q={q}"  # no account, no consent wall
# Field labels that mean "search box": submitting one only runs a search, so it needs no approval.
SEARCHY = re.compile(r"\b(search|query|find|look ?up|q)\b", re.I)

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


def find_browser() -> tuple[str, str] | None:
    """(name, exe) of the first installed browser: Chrome, else Edge."""
    for name, paths in BROWSERS:
        for p in (os.path.expandvars(x) for x in paths):
            if Path(p).exists():
                return name, p
    return None


def find_chrome() -> str | None:  # kept for scripts and older callers
    found = find_browser()
    return found[1] if found else None


def chrome_args(profile: Path, port: int = PORT) -> list[str]:
    return [f"--remote-debugging-port={port}", f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", "--disable-features=Translate"]


class BrowserManager:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.profile = data_dir / "chrome-profile"  # set per browser at launch
        self.browser_name = ""
        self.last_fill_was_search = False  # lets "press Enter" after typing a search run freely
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
            found = find_browser()
            if not found:
                raise RuntimeError("No supported browser found. Install Google Chrome or Microsoft Edge.")
            self.browser_name, exe = found
            self.profile = self.data_dir / f"{self.browser_name}-profile"
            self.profile.mkdir(parents=True, exist_ok=True)
            # Popen only starts the process (non-blocking); the browser must outlive any asyncio transport
            self._proc = subprocess.Popen([exe, *chrome_args(self.profile)], close_fds=True)  # noqa: ASYNC220
            for _ in range(60):
                if await self._alive(endpoint):
                    break
                await asyncio.sleep(0.25)
            else:
                raise RuntimeError(f"{self.browser_name} did not open its remote-control port")
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(endpoint)
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_when_idle())
        log.info("browser.started", browser=self.browser_name or "already running")

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
            matches = (page.get_by_role("button", name=args.text).or_(page.get_by_role("link", name=args.text))
                       .or_(page.get_by_text(args.text)))
            target = None
            for i in range(min(await matches.count(), 10)):
                with contextlib.suppress(Exception):
                    if await matches.nth(i).is_visible():
                        target = matches.nth(i)
                        break
            if target is None:
                return ToolResult(False, f"nothing visible called '{args.text}'; use browser_read to see the page")
            await target.click(timeout=8000)
            await page.wait_for_load_state("domcontentloaded")
            ev = await _evidence(page, self.mgr)
            return ToolResult(True, f"clicked '{args.text}', now at {ev['url']}", ev)

        return await self._guarded(go)


class FillArgs(BaseModel):
    field: str = Field(description="Label or placeholder of the input")
    value: str
    submit: bool = Field(default=False, description="Press Enter afterwards (free for search boxes)")


async def _first_visible(locator: Any, limit: int = 10) -> Any | None:
    """Pages often have hidden duplicates (mobile menus, collapsed headers): use a visible one."""
    for i in range(min(await locator.count(), limit)):
        item = locator.nth(i)
        with contextlib.suppress(Exception):
            if await item.is_visible() and await item.is_editable():
                return item
    return None


async def _is_search_box(box: Any) -> bool:
    attrs = [((await box.get_attribute(a)) or "") for a in ("type", "role", "name", "aria-label", "placeholder")]
    return attrs[0].lower() == "search" or attrs[1].lower() == "searchbox" or bool(SEARCHY.search(" ".join(attrs)))


class BrowserFill(_BrowserTool[FillArgs]):
    name = "browser_fill"
    description = ("Type into a field found by its label or placeholder; submit=true presses Enter. "
                   "Never for passwords or card numbers.")
    risk = Risk.ACT
    Args = FillArgs

    def risk_for(self, args: FillArgs) -> Risk:
        # Submitting a search box only runs a search; submitting any other form may send something.
        if args.submit and not SEARCHY.search(args.field):
            return Risk.EXTERNAL
        return Risk.ACT

    def summary(self, args: FillArgs) -> str:
        what = "fill and submit" if args.submit else "fill"
        return f"{what} '{args.field}' on {self.mgr.current_url or 'the page'}"

    async def run(self, args: FillArgs) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            candidates = (page.get_by_label(args.field).or_(page.get_by_placeholder(args.field))
                          .or_(page.get_by_role("searchbox", name=args.field))
                          .or_(page.get_by_role("textbox", name=args.field)))
            box = await _first_visible(candidates)
            if box is None and SEARCHY.search(args.field):  # "search" asked for: any visible search box
                box = await _first_visible(page.get_by_role("searchbox").or_(page.locator("input[type=search]")))
            if box is None:
                return ToolResult(False, f"no visible field called '{args.field}'; use browser_read to see the page")
            kind = ((await box.get_attribute("type")) or "").lower()
            auto = ((await box.get_attribute("autocomplete")) or "").lower()
            if kind == "password" or "cc-" in auto or "password" in auto:
                return ToolResult(False, "REFUSED: password/payment field. Ask the user to fill it in.")
            await box.fill(args.value, timeout=8000)
            self.mgr.last_fill_was_search = await _is_search_box(box)
            if args.submit:
                await box.press("Enter")
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
            done = "filled and submitted" if args.submit else "filled"
            return ToolResult(True, f"{done} '{args.field}'", await _evidence(page, self.mgr))

        return await self._guarded(go)


class SearchArgs(BaseModel):
    query: str


class BrowserSearch(_BrowserTool[SearchArgs]):
    name = "browser_search"
    description = ("Search the web in the browser and show the results page (then browser_read / "
                   "browser_click to open a result). Use web_search instead when you only need text.")
    risk = Risk.ACT
    Args = SearchArgs

    def domains(self, args: SearchArgs) -> list[str]:
        return [SEARCH_URL]

    async def run(self, args: SearchArgs) -> ToolResult:
        from urllib.parse import quote_plus

        async def go() -> ToolResult:
            page = await self.mgr.page()
            await page.goto(SEARCH_URL.format(q=quote_plus(args.query)), wait_until="domcontentloaded", timeout=30000)
            with contextlib.suppress(Exception):
                await page.wait_for_selector("a[data-testid=result-title-a], article h2 a", timeout=8000)
            ev = await _evidence(page, self.mgr)
            return ToolResult(True, f"searched '{args.query}' ({ev['url']}); use browser_read to see results", ev)

        return await self._guarded(go)


class ScrollArgs(BaseModel):
    direction: str = Field(default="down", pattern="^(up|down)$")
    pages: float = Field(default=1, gt=0, le=10)


class BrowserScroll(_BrowserTool[ScrollArgs]):
    name = "browser_scroll"
    description = "Scroll the current page up or down by screen heights."
    risk = Risk.ACT
    Args = ScrollArgs

    async def run(self, args: ScrollArgs) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            height = await page.evaluate("window.innerHeight")
            await page.mouse.wheel(0, height * args.pages * (1 if args.direction == "down" else -1))
            await asyncio.sleep(0.3)
            return ToolResult(True, f"scrolled {args.direction}", await _evidence(page, self.mgr))

        return await self._guarded(go)


class BrowserBack(_BrowserTool[NoArgs]):
    name = "browser_back"
    description = "Go back to the previous page."
    risk = Risk.ACT
    Args = NoArgs

    async def run(self, args: NoArgs) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            await page.go_back(wait_until="domcontentloaded", timeout=15000)
            return ToolResult(True, "went back", await _evidence(page, self.mgr))

        return await self._guarded(go)


class PressArgs(BaseModel):
    key: str = Field(description="e.g. Enter, Tab, Escape, PageDown")


class BrowserPress(_BrowserTool[PressArgs]):
    name = "browser_press"
    description = "Press a key in the browser page."
    risk = Risk.ACT
    Args = PressArgs

    def risk_for(self, args: PressArgs) -> Risk:
        # Enter right after typing into a search box just searches; elsewhere it can submit a form.
        if args.key.lower() in {"enter", "return"} and not self.mgr.last_fill_was_search:
            return Risk.EXTERNAL
        return Risk.ACT

    async def run(self, args: PressArgs) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            await page.keyboard.press(args.key)
            if args.key.lower() in {"enter", "return"}:
                self.mgr.last_fill_was_search = False  # one search per typed query
            return ToolResult(True, f"pressed {args.key}", await _evidence(page, self.mgr))

        return await self._guarded(go)


def browser_tools(mgr: BrowserManager) -> list[Tool]:
    return [BrowserOpen(mgr), BrowserSearch(mgr), BrowserRead(mgr), BrowserClick(mgr), BrowserFill(mgr),
            BrowserPress(mgr), BrowserScroll(mgr), BrowserBack(mgr)]
