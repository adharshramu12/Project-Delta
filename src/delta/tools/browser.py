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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
from pydantic import BaseModel, Field, model_validator

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

# Accessible-name approximation shared by the page listing and the live re-check before acting.
_NAME_JS = """(el) => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const by = el.getAttribute('aria-labelledby');
  if (by) {
    const parts = by.split(/\\s+/).map((id) => document.getElementById(id));
    const t = clean(parts.map((n) => (n ? n.innerText : '')).join(' '));
    if (t) return t;
  }
  const aria = clean(el.getAttribute('aria-label'));
  if (aria) return aria;
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select') {
    if (tag === 'input' && ['submit', 'button', 'reset'].includes(type)) return clean(el.value);
    const lab = el.labels && el.labels.length ? clean(el.labels[0].innerText) : '';
    return lab || clean(el.placeholder) || clean(el.title) || clean(el.name);
  }
  const img = el.querySelector && el.querySelector('img[alt]');
  return clean(el.innerText) || clean(el.title) || (img ? clean(img.alt) : '');
}"""

# Numbers every visible interactive element with a data-delta-ref attribute. Numbers are kept for an
# element across reads of the same document (only new elements get new numbers), so a ref the agent
# saw stays valid until the page navigates. Returns one record per numbered element.
REFS_JS = """() => {
  const name = """ + _NAME_JS + """;
  const SEL = 'a[href],button,input:not([type=hidden]),textarea,select,summary,[contenteditable=true],'
    + '[role=button],[role=link],[role=textbox],[role=searchbox],[role=combobox],[role=checkbox],'
    + '[role=radio],[role=switch],[role=tab],[role=menuitem],[role=option]';
  const ROLE = {a: 'link', button: 'button', textarea: 'textbox', select: 'combobox', summary: 'button'};
  const INPUT = {submit: 'button', button: 'button', reset: 'button', image: 'button', checkbox: 'checkbox',
                 radio: 'radio', search: 'searchbox', range: 'slider', file: 'file'};
  if (typeof window.__deltaRefNext !== 'number') window.__deltaRefNext = 1;
  const out = [];
  const vh = window.innerHeight, vw = window.innerWidth;
  for (const el of document.querySelectorAll(SEL)) {
    if (out.length >= %(limit)d) break;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || el.disabled) continue;
    let ref = Number(el.getAttribute('data-delta-ref'));
    if (!ref) { ref = window.__deltaRefNext++; el.setAttribute('data-delta-ref', String(ref)); }
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const role = el.getAttribute('role') || ROLE[tag] || (tag === 'input' ? (INPUT[type] || 'textbox') : tag);
    const attr = (a) => (el.getAttribute(a) || '').slice(0, 80);
    out.push({ref, role, tag, type, name: name(el).slice(0, 80), attrs: [attr('name'), attr('aria-label'),
      attr('placeholder'), attr('autocomplete')].join(' ').trim(),
      inView: r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw});
  }
  return out;
}"""
REF_LIMIT = 200  # elements numbered per read; the listing itself is cut by the read budget
SNAPSHOT_CHARS = 1200  # page summary returned with every action: enough to pick the next step
SNAPSHOT_ELEMENTS = 30
SETTLE_IDLE_MS = 1500  # the most an action waits for the network to go quiet
ELEMENTS_JS = REFS_JS % {"limit": REF_LIMIT}  # kept name: older callers evaluate ELEMENTS_JS


@dataclass(frozen=True)
class ElementRef:
    """One numbered element as the agent was shown it. The name is what risk checks use."""

    ref: int
    role: str
    name: str
    tag: str = ""
    type: str = ""
    attrs: str = ""  # name/aria-label/placeholder/autocomplete, for search-box and password checks
    in_view: bool = True

    def line(self) -> str:
        kind = f" ({self.type})" if self.type and self.role == "textbox" and self.type != "text" else ""
        return f'[{self.ref}] {self.role} "{self.name}"{kind}'

    def field_text(self) -> str:
        """What a fill's search-box check reads: the same signals _is_search_box looks at."""
        return " ".join(x for x in (self.name, self.type, self.role, self.attrs) if x)


def parse_refs(raw: Any) -> list[ElementRef]:
    """Page records from REFS_JS -> ElementRefs, skipping anything malformed."""
    out: list[ElementRef] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            ref = int(item.get("ref", 0))
        except (TypeError, ValueError):
            continue
        if ref <= 0:
            continue
        out.append(ElementRef(ref=ref, role=str(item.get("role") or ""), name=str(item.get("name") or ""),
                              tag=str(item.get("tag") or ""), type=str(item.get("type") or ""),
                              attrs=str(item.get("attrs") or ""), in_view=bool(item.get("inView", True))))
    return out


_RISK_RANK = {Risk.READ: 0, Risk.ACT: 1, Risk.WRITE: 2, Risk.EXTERNAL: 3, Risk.DESTRUCTIVE: 4, Risk.SYSTEM: 5}


def riskier(a: Risk, b: Risk) -> bool:
    return _RISK_RANK[a] > _RISK_RANK[b]


class StaleRef(LookupError):
    pass


def stale_message(ref: int) -> str:
    return f"element [{ref}] is no longer on the page (it changed or navigated); call browser_read to get fresh refs"


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
        self.refs: dict[int, ElementRef] = {}  # numbered elements from the latest listing of the page
        self.refs_url = ""

    def remember_refs(self, elements: list[ElementRef], url: str) -> None:
        """The latest listing replaces the old one: only refs the agent was just shown are valid."""
        self.refs = {e.ref: e for e in elements}
        self.refs_url = url

    def ref(self, ref: int) -> ElementRef:
        if ref not in self.refs:
            raise StaleRef(stale_message(ref))
        return self.refs[ref]

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
            self._proc = subprocess.Popen([exe, *chrome_args(self.profile, PORT)], close_fds=True)  # noqa: ASYNC220
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


async def settle(page: Any, idle_ms: int = SETTLE_IDLE_MS) -> None:
    """Wait for the page to be ready after an action, bounded: the document parsed, then up to
    idle_ms for the network to go quiet, then two animation frames so scripts have rendered.
    Every step gives up quietly: a busy page (live feeds, ads) must not stall the agent."""
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=idle_ms)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(page.evaluate(
            "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))"), 1.0)


async def list_refs(page: Any, mgr: BrowserManager) -> list[ElementRef]:
    """Number the page's interactive elements and make them the agent's current refs."""
    elements = parse_refs(await page.evaluate(ELEMENTS_JS))
    mgr.remember_refs(elements, page.url)
    return elements


def snapshot_text(title: str, url: str, elements: list[ElementRef], budget: int = SNAPSHOT_CHARS,
                  max_elements: int = SNAPSHOT_ELEMENTS) -> str:
    """Compact page summary for action results: title, URL and the first numbered elements,
    on-screen ones first. Bounded so every action costs at most ~budget characters."""
    from delta.tools.user_browser import select_lines

    ordered = [e for e in elements if e.in_view] + [e for e in elements if not e.in_view]
    lines = [e.line() for e in ordered[:max_elements]]
    more = len(ordered) - len(lines)
    body = select_lines(lines, None, budget) if lines else "(no clickable elements found)"
    tail = f"\n…[{more} more elements; browser_read lists them]" if more > 0 else ""
    return f"Page: {title} — {url}\n{body}{tail}"


async def _observed(page: Any, mgr: BrowserManager, message: str) -> ToolResult:
    """Act-and-observe: the action's result carries a short snapshot of the page it left behind,
    with fresh refs, so the next step usually needs no separate browser_read."""
    ev = await _evidence(page, mgr)
    try:
        elements = await list_refs(page, mgr)
    except Exception:  # snapshot is a bonus; the action itself succeeded
        mgr.remember_refs([], page.url)
        return ToolResult(True, message, ev)
    return ToolResult(True, f"{message}\n\n{snapshot_text(ev['title'], ev['url'], elements)}", ev)


async def _ref_target(page: Any, mgr: BrowserManager, ref: int) -> tuple[Any, ElementRef, str]:
    """The live element for a ref, the record the agent saw, and the element's name right now."""
    seen = mgr.ref(ref)
    loc = page.locator(f'[data-delta-ref="{ref}"]')
    if await loc.count() == 0:
        raise StaleRef(stale_message(ref))
    loc = loc.first
    live = str(await loc.evaluate(_NAME_JS) or "")
    return loc, seen, live


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
    description = ("Open a URL in Delta's Chrome (signed in to the user's sites). Returns the page title, URL "
                   "and numbered elements ([12] button \"Sign in\") to use as ref in browser_click/browser_fill.")
    risk = Risk.ACT
    Args = OpenArgs

    def domains(self, args: OpenArgs) -> list[str]:
        return [args.url]

    async def run(self, args: OpenArgs) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            url = args.url if "://" in args.url else f"https://{args.url}"
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await settle(page)
            return await _observed(page, self.mgr, f"opened {await page.title()} ({page.url})")

        return await self._guarded(go)


class NoArgs(BaseModel):
    pass


class ReadArgs(BaseModel):
    focus: str | None = Field(default=None, description="Only return parts of the page about this, e.g. 'born'")


class BrowserRead(_BrowserTool[ReadArgs]):
    name = "browser_read"
    description = ("Read the current browser page: title, URL, visible text and numbered clickable elements "
                   "([12] button \"Sign in\"); pass the number as ref to browser_click / browser_fill. "
                   "Pass focus (e.g. 'born', 'price') to get only the relevant parts of long pages.")
    risk = Risk.READ
    Args = ReadArgs

    async def run(self, args: ReadArgs) -> ToolResult:
        from delta.tools.user_browser import MAX_READ_CHARS, select_lines

        async def go() -> ToolResult:
            page = await self.mgr.page()
            text = await page.inner_text("body")
            elements = await list_refs(page, self.mgr)
            ev = await _evidence(page, self.mgr)
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()] + [e.line() for e in elements]
            body = f"{ev['title']} — {ev['url']}\n\n" + select_lines(lines, args.focus, MAX_READ_CHARS)
            return ToolResult(True, body, ev)

        return await self._guarded(go)


class ClickArgs(BaseModel):
    text: str = Field(description="Visible text or accessible name of the link/button")


class RefClickArgs(ClickArgs):
    """Delta browser's click: by ref number from the latest page listing, or by visible text."""

    text: str = Field(default="", description="Visible text or accessible name of the link/button")
    ref: int | None = Field(default=None, description="Element number from the latest page listing, e.g. 12 for [12]")

    @model_validator(mode="after")
    def _one_target(self) -> RefClickArgs:
        if self.ref is None and not self.text.strip():
            raise ValueError("give ref (the [number] from the page listing) or text")
        return self


def _click_label(mgr: BrowserManager, args: ClickArgs) -> str:
    """The name risk checks read: the element's real name for a ref, else the text asked for.
    An unknown ref has no name; its run refuses before touching the page."""
    ref = getattr(args, "ref", None)
    if ref is None:
        return args.text
    seen = mgr.refs.get(ref)
    return seen.name if seen else ""


class BrowserClick(_BrowserTool[RefClickArgs]):
    name = "browser_click"
    description = ("Click a link or button on the current page: by ref (the [number] from browser_read or the "
                   "last action's page listing; fastest and exact) or by its visible text.")
    risk = Risk.ACT
    Args = RefClickArgs

    def risk_for(self, args: ClickArgs) -> Risk:
        return click_risk(_click_label(self.mgr, args))

    def summary(self, args: ClickArgs) -> str:
        ref = getattr(args, "ref", None)
        what = f"[{ref}] '{_click_label(self.mgr, args)}'" if ref is not None else f"'{args.text}'"
        return f"click {what} on {self.mgr.current_url or 'the page'}"

    async def run(self, args: ClickArgs) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            ref = getattr(args, "ref", None)
            if ref is not None:
                try:
                    target, seen, live = await _ref_target(page, self.mgr, ref)
                except StaleRef as e:
                    return ToolResult(False, str(e))
                # The approval was decided on the name the agent saw; never click something riskier.
                if riskier(click_risk(live), click_risk(seen.name)):
                    return ToolResult(False, f"element [{ref}] now reads '{live}', not '{seen.name}'; "
                                             "call browser_read to get fresh refs")
                label = live or seen.name
            else:
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
                label = args.text
            await target.click(timeout=8000)
            await settle(page)
            return await _observed(page, self.mgr, f"clicked '{label}', now at {page.url}")

        return await self._guarded(go)


class FillArgs(BaseModel):
    field: str = Field(description="Label or placeholder of the input")
    value: str
    submit: bool = Field(default=False, description="Press Enter afterwards (free for search boxes)")


class RefFillArgs(FillArgs):
    """Delta browser's fill: by ref number from the latest page listing, or by label/placeholder."""

    field: str = Field(default="", description="Label or placeholder of the input")
    ref: int | None = Field(default=None, description="Field number from the latest page listing, e.g. 7 for [7]")

    @model_validator(mode="after")
    def _one_target(self) -> RefFillArgs:
        if self.ref is None and not self.field.strip():
            raise ValueError("give ref (the [number] from the page listing) or field")
        return self


def _fill_label(mgr: BrowserManager, args: FillArgs) -> str:
    """What the search-box check reads: for a ref, the field's real name and attributes."""
    ref = getattr(args, "ref", None)
    if ref is None:
        return args.field
    seen = mgr.refs.get(ref)
    return seen.field_text() if seen else ""


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


class BrowserFill(_BrowserTool[RefFillArgs]):
    name = "browser_fill"
    description = ("Type into a field: by ref (the [number] from the page listing) or by its label or "
                   "placeholder; submit=true presses Enter. Never for passwords or card numbers.")
    risk = Risk.ACT
    Args = RefFillArgs

    def risk_for(self, args: FillArgs) -> Risk:
        # Submitting a search box only runs a search; submitting any other form may send something.
        if args.submit and not SEARCHY.search(_fill_label(self.mgr, args)):
            return Risk.EXTERNAL
        return Risk.ACT

    def summary(self, args: FillArgs) -> str:
        what = "fill and submit" if args.submit else "fill"
        ref = getattr(args, "ref", None)
        seen = self.mgr.refs.get(ref) if ref is not None else None
        field = f"[{ref}] '{seen.name if seen else ''}'" if ref is not None else f"'{args.field}'"
        return f"{what} {field} on {self.mgr.current_url or 'the page'}"

    async def run(self, args: FillArgs) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            ref = getattr(args, "ref", None)
            if ref is not None:
                try:
                    box, seen, live = await _ref_target(page, self.mgr, ref)
                except StaleRef as e:
                    return ToolResult(False, str(e))
                if not (await box.is_visible() and await box.is_editable()):
                    return ToolResult(False, f"element [{ref}] ('{live or seen.name}') is not a field you can type in")
                label = live or seen.name
            else:
                candidates = (page.get_by_label(args.field).or_(page.get_by_placeholder(args.field))
                              .or_(page.get_by_role("searchbox", name=args.field))
                              .or_(page.get_by_role("textbox", name=args.field)))
                box = await _first_visible(candidates)
                if box is None and SEARCHY.search(args.field):  # "search" asked for: any visible search box
                    box = await _first_visible(page.get_by_role("searchbox").or_(page.locator("input[type=search]")))
                if box is None:
                    return ToolResult(False,
                                      f"no visible field called '{args.field}'; use browser_read to see the page")
                label = args.field
            kind = ((await box.get_attribute("type")) or "").lower()
            auto = ((await box.get_attribute("autocomplete")) or "").lower()
            if kind == "password" or "cc-" in auto or "password" in auto:
                return ToolResult(False, "REFUSED: password/payment field. Ask the user to fill it in.")
            is_search = await _is_search_box(box)
            if ref is not None and args.submit and not is_search and self.risk_for(args) == Risk.ACT:
                # Approved as a search box; the live field is not one, so Enter could send a form.
                return ToolResult(False, f"element [{ref}] is no longer a search box; call browser_read again")
            await box.fill(args.value, timeout=8000)
            self.mgr.last_fill_was_search = is_search
            if args.submit:
                await box.press("Enter")
                await settle(page)
            done = "filled and submitted" if args.submit else "filled"
            return await _observed(page, self.mgr, f"{done} '{label}'")

        return await self._guarded(go)


class SearchArgs(BaseModel):
    query: str


class BrowserSearch(_BrowserTool[SearchArgs]):
    name = "browser_search"
    description = ("Search the web in the browser and show the results page with numbered links (then "
                   "browser_click ref=N to open a result). Use web_search instead when you only need text.")
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
            await settle(page)
            return await _observed(page, self.mgr, f"searched '{args.query}' ({page.url})")

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
            await settle(page, idle_ms=800)
            return await _observed(page, self.mgr, f"scrolled {args.direction}")

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
            await settle(page)
            return await _observed(page, self.mgr, "went back")

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
            await settle(page)
            return await _observed(page, self.mgr, f"pressed {args.key}")

        return await self._guarded(go)


def browser_tools(mgr: BrowserManager) -> list[Tool]:
    return [BrowserOpen(mgr), BrowserSearch(mgr), BrowserRead(mgr), BrowserClick(mgr), BrowserFill(mgr),
            BrowserPress(mgr), BrowserScroll(mgr), BrowserBack(mgr)]
