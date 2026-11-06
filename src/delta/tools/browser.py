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
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import psutil
from pydantic import BaseModel, Field, model_validator

from delta.system.logging import log
from delta.tools import snapshot
from delta.tools.base import Risk, Tool, ToolResult
from delta.tools.desktop import click_risk

BROWSERS = [  # first one found is used
    ("chrome", [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"]),
    ("edge", [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
              r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"]),
]
# Remote-control (CDP) port of Delta's own browser. 0 = a free port is picked at each launch, so no fixed, well-known
# port is left open on the PC (L16). Set a number to pin it (tests and evals do). Before 0.2.0 it was always 9223.
PORT = 0
LEGACY_PORT = 9223
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
  if (typeof window.__deltaRefNext !== 'number') window.__deltaRefNext = %(base)d;
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
SETTLE_IDLE_MS = 1500  # the most an action waits for the network and the DOM to go quiet
QUIET_S = 0.2  # no request in flight for this long counts as quiet
DOM_QUIET_MS = 150  # no DOM change for this long counts as rendered
ELEMENTS_JS = REFS_JS % {"limit": REF_LIMIT, "base": 1}  # kept name: older callers evaluate ELEMENTS_JS
# Elements inside frames (iframes) are numbered too, each frame from its own block of numbers, so a ref is unique
# across the page and stays valid while the frame lives. At most MAX_FRAMES frames and FRAME_LIMIT elements each.
FRAME_BLOCK = 1000
MAX_FRAMES = 6
FRAME_LIMIT = 80


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
    frame: str = ""  # '' = the page itself; else the host of the frame (iframe) it is in

    def line(self) -> str:
        kind = f" ({self.type})" if self.type and self.role == "textbox" and self.type != "text" else ""
        where = f" (in frame {self.frame})" if self.frame else ""
        return f'[{self.ref}] {self.role} "{self.name}"{kind}{where}'

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
                              attrs=str(item.get("attrs") or ""), in_view=bool(item.get("inView", True)),
                              frame=str(item.get("frame") or "")))
    return out


_RISK_RANK = {Risk.READ: 0, Risk.ACT: 1, Risk.WRITE: 2, Risk.EXTERNAL: 3, Risk.DESTRUCTIVE: 4, Risk.SYSTEM: 5}


def riskier(a: Risk, b: Risk) -> bool:
    return _RISK_RANK[a] > _RISK_RANK[b]


class StaleRef(LookupError):
    pass


def stale_message(ref: int) -> str:
    return f"element [{ref}] is no longer on the page (it changed or navigated); call browser_read to get fresh refs"


def find_browser(prefer: str = "auto") -> tuple[str, str] | None:
    """(name, exe) of the browser for Delta's own profile. auto: the first installed (Chrome, else Edge).
    chrome / edge: that one when installed, else the other one, so a task still works."""
    found: list[tuple[str, str]] = []
    for name, paths in BROWSERS:
        for p in (os.path.expandvars(x) for x in paths):
            if Path(p).exists():
                found.append((name, p))
                break
    if prefer in ("chrome", "edge"):
        found.sort(key=lambda f: f[0] != prefer)
    return found[0] if found else None


def find_chrome() -> str | None:  # kept for scripts and older callers
    found = find_browser()
    return found[1] if found else None


def chrome_args(profile: Path, port: int | None = None) -> list[str]:
    return [f"--remote-debugging-port={port or PORT or LEGACY_PORT}", f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", "--disable-features=Translate"]


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _arg(cmd: list[str], name: str) -> str | None:
    return next((a.split("=", 1)[1] for a in cmd if a.startswith(f"--{name}=")), None)


def _listens_on(proc: Any, port: int) -> bool:
    """True when the process listens on the port; also True when Windows will not say (never a reason to fail)."""
    try:
        conns = proc.net_connections(kind="tcp") if hasattr(proc, "net_connections") else proc.connections("tcp")
    except (psutil.Error, OSError, NotImplementedError):
        return True
    return any(c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN for c in conns)


def owned_browser_port(data_dir: Path) -> int | None:
    """The CDP port of a browser that Delta itself started (its profile is inside Delta's data folder), when one
    is still running: after a Delta restart it is reused instead of starting a second copy on the same profile.
    Whatever else answers on a port is never trusted just because it answers (the old fixed-port behaviour)."""
    root = os.path.normcase(os.path.abspath(data_dir))
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmd = proc.info.get("cmdline") or []
            port_s, profile = _arg(cmd, "remote-debugging-port"), _arg(cmd, "user-data-dir")
            if not port_s or not profile or not port_s.isdigit() or int(port_s) == 0:
                continue
            inside = os.path.normcase(os.path.abspath(profile.strip('"')))
            if (inside == root or inside.startswith(root + os.sep)) and _listens_on(proc, int(port_s)):
                return int(port_s)
        except (psutil.Error, OSError, ValueError, TypeError):
            continue
    return None


class BrowserManager:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.profile = data_dir / "chrome-profile"  # set per browser at launch
        self.browser_name = ""
        # Which installed browser runs Delta's profile: auto (Chrome, else Edge), chrome or edge.
        # Changed only between tasks (delta.tools.browser_switch), never while a task is using it.
        self.engine = "auto"
        self.port = 0  # the CDP port of the browser in use (0 = not started)
        self.hold_until = 0.0  # monotonic time: not closed for idleness before this (the user is signing in)
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
        # Act-and-observe sends only what changed on the same page (snapshot.py); off = full every time.
        self.diff_snapshots = True
        self.full_every = snapshot.FULL_EVERY
        self.snap = snapshot.SnapshotState()  # what the model was last shown of this page
        # Tabs, frames and dialogs (delta.tools.browser_actions): the tab Delta works in, the number block each
        # frame's elements get, what the last dialog said and what to do with the next one.
        self.active: Any = None
        self.frame_bases: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()
        self.events: list[str] = []  # things that happened by themselves (a new tab, a dialog), told once
        self.next_dialog = "dismiss"  # accept | dismiss: the next confirm/prompt dialog (alerts are just closed)
        self.dialog_text = ""

    def frame_base(self, frame: Any) -> int:
        """The first ref number for a frame's elements: a new block of FRAME_BLOCK per frame, kept while it lives."""
        if frame not in self.frame_bases:
            self.frame_bases[frame] = FRAME_BLOCK * (len(self.frame_bases) + 1) + 1
        return self.frame_bases[frame]

    def note(self, text: str) -> None:
        if text and text not in self.events:
            self.events = [*self.events[-4:], text]

    def take_notes(self) -> str:
        out, self.events = self.events, []
        return " ".join(out)

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
            watch_context(ctx, self)
            active = self.active
            if active is not None and active in ctx.pages and not _closed(active):
                page = active
            else:
                page = ctx.pages[-1] if ctx.pages else await ctx.new_page()
                self.active = page
            track_requests(page)
            watch_page(page, self)
            return page

    async def _launch(self) -> None:
        from playwright.async_api import async_playwright

        owned: int | None = None if PORT else await asyncio.to_thread(owned_browser_port, self.data_dir)
        port = PORT or owned or 0
        endpoint = f"http://127.0.0.1:{port}"
        if not (port and await self._alive(endpoint)):
            port = PORT or free_port()
            endpoint = f"http://127.0.0.1:{port}"
            found = find_browser(self.engine)
            if not found:
                raise RuntimeError("No supported browser found. Install Google Chrome or Microsoft Edge.")
            self.browser_name, exe = found
            self.profile = self.data_dir / f"{self.browser_name}-profile"
            self.profile.mkdir(parents=True, exist_ok=True)
            # Popen only starts the process (non-blocking); the browser must outlive any asyncio transport
            self._proc = subprocess.Popen([exe, *chrome_args(self.profile, port)], close_fds=True)  # noqa: ASYNC220
            for _ in range(60):
                if await self._alive(endpoint):
                    break
                await asyncio.sleep(0.25)
            else:
                raise RuntimeError(f"{self.browser_name} did not open its remote-control port")
        self.port = port
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(endpoint)
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_when_idle())
        log.info("browser.started", browser=self.browser_name or "already running")

    @property
    def running(self) -> bool:
        return self._browser is not None

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
            if time.monotonic() - self._last_used > IDLE_CLOSE_S and time.monotonic() > self.hold_until:
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


# Requests in flight per page, so settle() also waits for the API calls an action starts
# (a click that fetches data, a SPA route change). Playwright's "networkidle" load state cannot
# do that: once a page has reached it, waiting for it again returns at once.
_INFLIGHT: weakref.WeakKeyDictionary[Any, set[Any]] = weakref.WeakKeyDictionary()


def track_requests(page: Any) -> None:
    """Count the page's requests from now on (once per page)."""
    if page in _INFLIGHT:
        return
    pending: set[Any] = set()
    _INFLIGHT[page] = pending

    # Plain functions, not bound builtins: Playwright tags each handler with an attribute.
    def started(req: Any) -> None:
        pending.add(req)

    def ended(req: Any) -> None:
        pending.discard(req)

    page.on("request", started)
    page.on("requestfinished", ended)
    page.on("requestfailed", ended)


def _closed(page: Any) -> bool:
    with contextlib.suppress(Exception):
        return bool(page.is_closed())
    return False


_WATCHED: weakref.WeakSet[Any] = weakref.WeakSet()


def watch_context(ctx: Any, mgr: BrowserManager) -> None:
    """New tabs and popups: the tab a click opened becomes the one Delta works in (as before), and the next action
    result says so once. Installed once per browser context."""
    if ctx in _WATCHED:
        return
    _WATCHED.add(ctx)

    def opened(page: Any) -> None:
        mgr.active = page
        mgr.note("A new tab opened and Delta now works in it (browser_tabs lists and switches tabs).")

    with contextlib.suppress(Exception):
        ctx.on("page", opened)


def watch_page(page: Any, mgr: BrowserManager) -> None:
    """Dialogs (alert, confirm, prompt, leave-page): an alert is closed; a confirm or prompt is dismissed unless the
    agent asked for the next one to be accepted (browser_dialog). What it said is told once with the next result.
    Before, Playwright dismissed every dialog silently."""
    if page in _WATCHED:
        return
    _WATCHED.add(page)

    async def handle(dialog: Any) -> None:
        kind = str(getattr(dialog, "type", "") or "")
        text = str(getattr(dialog, "message", "") or "")[:200]
        accept = kind == "alert" or mgr.next_dialog == "accept"
        mgr.next_dialog = "dismiss"  # one dialog per browser_dialog(accept=true)
        mgr.dialog_text = text
        with contextlib.suppress(Exception):
            await (dialog.accept() if accept else dialog.dismiss())
        what = "closed" if kind == "alert" else ("accepted" if accept else "dismissed (Cancel)")
        mgr.note(f"A {kind or 'page'} dialog said: \"{text}\". It was {what}."
                 + ("" if accept else " To accept the next one, call browser_dialog(accept=true) and repeat."))

    def on_dialog(dialog: Any) -> None:
        asyncio.ensure_future(handle(dialog))  # noqa: RUF006 (lives as long as the dialog)

    with contextlib.suppress(Exception):
        page.on("dialog", on_dialog)


async def _network_quiet(pending: set[Any], deadline: float) -> None:
    """Until no request has been in flight for QUIET_S, or the deadline passes."""
    quiet_since: float | None = None
    while (now := time.monotonic()) < deadline:
        if pending:
            quiet_since = None
        elif quiet_since is None:
            quiet_since = now
        elif now - quiet_since >= QUIET_S:
            return
        await asyncio.sleep(0.05)


# Resolves once the DOM has had no mutation for %(quiet)d ms, or after %(cap)d ms at most. Plain
# numbers are written into the script (no evaluate argument) so it runs on any page object.
DOM_QUIET_JS = """() => new Promise((resolve) => {
  let timer = null, cap = null;
  const done = () => { obs.disconnect(); clearTimeout(timer); clearTimeout(cap); resolve(true); };
  const obs = new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(done, %(quiet)d); });
  obs.observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
  timer = setTimeout(done, %(quiet)d);
  cap = setTimeout(done, %(cap)d);
})"""


async def _dom_quiet(page: Any, deadline: float) -> None:
    """Until the page stops changing its DOM for DOM_QUIET_MS, or the deadline passes. A page that
    navigates away meanwhile ends the wait (its script context is gone): the network wait covers it."""
    left_ms = int((deadline - time.monotonic()) * 1000)
    if left_ms <= 0:
        return
    js = DOM_QUIET_JS % {"quiet": DOM_QUIET_MS, "cap": left_ms}
    with contextlib.suppress(Exception):
        await asyncio.wait_for(page.evaluate(js), left_ms / 1000 + 0.5)


async def _dom_ready(page: Any, timeout_ms: int) -> None:
    """Until the document is parsed. Asks the document first: a page restored from the back-forward
    cache fires no load events, so Playwright's load-state wait would sit out its whole timeout."""
    with contextlib.suppress(Exception):
        if await asyncio.wait_for(page.evaluate("document.readyState"), 2.0) in ("interactive", "complete"):
            return
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)


async def settle(page: Any, idle_ms: int = SETTLE_IDLE_MS) -> None:
    """Wait for the page to be ready after an action, driven by events and bounded: the document
    parsed, then (together, both capped at idle_ms) the network quiet for QUIET_S, including requests
    the action itself started, and the DOM free of mutations for DOM_QUIET_MS; then two animation
    frames so scripts have rendered. No fixed sleep: a page that is already still costs ~QUIET_S.
    (Playwright's "networkidle" is not used: it always waits 500 ms of silence after a load, and
    returns at once for requests started after the page reached it.) Every step gives up quietly:
    a busy page (live feeds, ads) must not stall the agent."""
    await _dom_ready(page, 10000)
    deadline = time.monotonic() + idle_ms / 1000
    pending = _INFLIGHT.get(page)
    waits = [_dom_quiet(page, deadline)]
    if pending is not None:
        waits.append(_network_quiet(pending, deadline))
    await asyncio.gather(*waits)
    await _dom_ready(page, 3000)  # the action may have started a navigation meanwhile
    with contextlib.suppress(Exception):
        await asyncio.wait_for(page.evaluate(
            "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))"), 1.0)


def _frame_host(url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(url).hostname or "") if url.startswith("http") else ""


def child_frames(page: Any) -> list[Any]:
    """The page's frames other than the page itself (iframes), in document order. [] for fakes without frames."""
    frames = getattr(page, "frames", None)
    main = getattr(page, "main_frame", None)
    if not isinstance(frames, list) or main is None:
        return []
    return [f for f in frames if f is not main]


async def list_refs(page: Any, mgr: BrowserManager) -> list[ElementRef]:
    """Number the page's interactive elements (and those inside its frames) and make them the agent's current
    refs. Frame elements carry the frame's host; one from another site says so."""
    elements = parse_refs(await page.evaluate(ELEMENTS_JS))
    main_host = _frame_host(str(getattr(page, "url", "")))
    for frame in child_frames(page)[:MAX_FRAMES]:
        with contextlib.suppress(Exception):
            if frame.is_detached():
                continue
            js = REFS_JS % {"limit": FRAME_LIMIT, "base": mgr.frame_base(frame)}
            host = _frame_host(str(frame.url)) or "this page"
            label = host if host in (main_host, "this page") else f"{host}, another site"
            elements += [ElementRef(e.ref, e.role, e.name, e.tag, e.type, e.attrs, e.in_view, label)
                         for e in parse_refs(await frame.evaluate(js))]
    mgr.remember_refs(elements, page.url)
    return elements


async def locate_ref(page: Any, ref: int) -> Any | None:
    """The live element numbered `ref`: on the page itself first, then inside its frames."""
    sel = f'[data-delta-ref="{ref}"]'
    loc = page.locator(sel)
    if await loc.count():
        return loc.first
    for frame in child_frames(page)[:MAX_FRAMES]:
        with contextlib.suppress(Exception):
            floc = frame.locator(sel)
            if await floc.count():
                return floc.first
    return None


def _ordered(elements: list[ElementRef]) -> list[ElementRef]:
    return [e for e in elements if e.in_view] + [e for e in elements if not e.in_view]


def _shown(elements: list[ElementRef], budget: int = SNAPSHOT_CHARS,
           max_elements: int = SNAPSHOT_ELEMENTS) -> list[ElementRef]:
    """The elements a snapshot lists: on-screen first, at most max_elements and ~budget characters
    (the same cut select_lines makes)."""
    out: list[ElementRef] = []
    size = 0
    for e in _ordered(elements)[:max_elements]:
        out.append(e)
        size += len(e.line())
        if size > budget:
            break
    return out


def snapshot_text(title: str, url: str, elements: list[ElementRef], budget: int = SNAPSHOT_CHARS,
                  max_elements: int = SNAPSHOT_ELEMENTS) -> str:
    """Compact page summary for action results: title, URL and the first numbered elements,
    on-screen ones first. Bounded so every action costs at most ~budget characters."""
    from delta.tools.user_browser import select_lines

    ordered = _ordered(elements)
    lines = [e.line() for e in ordered[:max_elements]]
    more = len(ordered) - len(lines)
    body = select_lines(lines, None, budget) if lines else "(no clickable elements found)"
    tail = f"\n…[{more} more elements; browser_read lists them]" if more > 0 else ""
    return f"{snapshot.FULL_PREFIX}{title} — {url}\n{body}{tail}"


def page_listing(mgr: BrowserManager, title: str, url: str, elements: list[ElementRef],
                 force_full: bool = False) -> str:
    """The listing to send after an action: only the changes when the model already holds this
    page (same address, not too many diffs in a row, and the diff is smaller), else in full."""
    full = snapshot_text(title, url, elements)
    if force_full:
        mgr.snap.reset()
    shown = _shown(elements)
    more = max(0, len(elements) - len(shown))
    return snapshot.choose(mgr.snap, title, url, shown, {e.ref for e in elements}, full, more,
                           enabled=mgr.diff_snapshots, every=mgr.full_every)


# What the self-check compares before and after an action: address, title, how much is on the page, what has focus,
# scroll position and the number of tabs. Cheap (one evaluate) and never fails the action.
STATE_JS = """() => {
  const a = document.activeElement;
  return {url: location.href, title: document.title, nodes: document.getElementsByTagName('*').length,
          text: document.body ? document.body.innerText.length : 0,
          focus: a ? (a.tagName + ':' + (a.getAttribute('data-delta-ref') || '')) : '',
          y: Math.round(window.scrollY), height: document.documentElement.scrollHeight};
}"""


async def page_state(page: Any) -> dict[str, Any]:
    """A small fingerprint of the page for the self-check ({} when it cannot be read)."""
    state: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        raw = await asyncio.wait_for(page.evaluate(STATE_JS), 3.0)
        if isinstance(raw, dict):
            state = dict(raw)
    with contextlib.suppress(Exception):
        state["tabs"] = len(page.context.pages)
    return state


def what_changed(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Plain words for what an action changed; [] when nothing visible changed (or nothing could be read)."""
    if not before or not after:
        return ["unknown"]
    out = []
    if before.get("url") != after.get("url"):
        out.append("address")
    if before.get("title") != after.get("title"):
        out.append("title")
    if before.get("tabs") != after.get("tabs"):
        out.append("tabs")
    if before.get("nodes") != after.get("nodes") or before.get("text") != after.get("text"):
        out.append("content")
    if before.get("y") != after.get("y") or before.get("height") != after.get("height"):
        out.append("scroll")
    if before.get("focus") != after.get("focus"):
        out.append("focus")
    return out


async def current_page(page: Any, mgr: BrowserManager) -> Any:
    """The tab to observe after an action: a tab the action opened, else the same one."""
    active = mgr.active
    if active is not None and active is not page and not _closed(active):
        await _dom_ready(active, 5000)
        track_requests(active)
        watch_page(active, mgr)
        return active
    return page


async def _observed(page: Any, mgr: BrowserManager, message: str, force_full: bool = False,
                    before: dict[str, Any] | None = None) -> ToolResult:
    """Act-and-observe: the action's result carries a short snapshot of the page it left behind,
    with fresh refs, so the next step usually needs no separate browser_read. On the same page it
    is only what changed (see snapshot.py); after a navigation it is the full listing.
    With `before` (a page_state taken before acting) the result also says whether the page changed, so the agent
    can tell a click that did nothing from one that worked (evidence "changed")."""
    page = await current_page(page, mgr)
    ev = await _evidence(page, mgr)
    if before is not None:
        changed = what_changed(before, await page_state(page))
        ev["changed"] = changed
        if not changed:
            message += ("\nSelf-check: the page did not visibly change. If something should have happened, the "
                        "action may not have worked: read the page, or try another way.")
        if "address" in changed:
            force_full = True
    if notes := mgr.take_notes():
        message += f"\n{notes}"
    try:
        elements = await list_refs(page, mgr)
    except Exception:  # snapshot is a bonus; the action itself succeeded
        mgr.remember_refs([], page.url)
        mgr.snap.reset()  # the model holds no current listing: the next one is sent in full
        return ToolResult(True, message, ev)
    listing = page_listing(mgr, ev["title"], ev["url"], elements, force_full)
    ev["snapshot"] = "diff" if listing.startswith(snapshot.DIFF_PREFIX) else "full"
    return ToolResult(True, f"{message}\n\n{listing}", ev)


async def _ref_target(page: Any, mgr: BrowserManager, ref: int) -> tuple[Any, ElementRef, str]:
    """The live element for a ref, the record the agent saw, and the element's name right now."""
    seen = mgr.ref(ref)
    loc = await locate_ref(page, ref)
    if loc is None:
        raise StaleRef(stale_message(ref))
    live = str(await loc.evaluate(_NAME_JS) or "")
    return loc, seen, live


CLICK_TRIES = 3  # a click is tried this often (scrolled into view, the page given time to settle) before failing


class ActionFailed(RuntimeError):
    """A browser action that could not be done, with a plain reason for the agent and the user."""


def explain(e: BaseException, what: str = "the element") -> str:
    """A Playwright error in plain words: why the action failed and what to try next."""
    msg = str(e).splitlines()[0] if str(e) else type(e).__name__
    low = str(e).lower()
    if "intercepts pointer events" in low:
        cover = re.search(r"<(\w+)[^>]*>", str(e).split("intercepts pointer events")[0][-300:])
        by = f" (a <{cover.group(1)}> element)" if cover else ""
        return (f"{what} is covered by something else{by}, such as a popup, cookie banner or overlay. Close it first "
                "(browser_read shows it), then try again.")
    if "not attached" in low or "detached" in low or "execution context was destroyed" in low:
        return f"{what} disappeared while acting (the page changed). Call browser_read for fresh refs."
    if "not visible" in low or "outside of the viewport" in low:
        return f"{what} is not visible. Scroll to it (browser_scroll ref=...) or open the menu that holds it."
    if "disabled" in low or "not enabled" in low:
        return f"{what} is disabled right now. Something else may need to be filled or chosen first."
    if "not editable" in low or "readonly" in low:
        return f"{what} cannot be typed into."
    if "timeout" in low:
        return (f"{what} did not become ready in time (it may be hidden, disabled, covered or still loading). "
                "Read the page and try another way.")
    if "net::" in low or "err_" in low:
        return f"the page could not be loaded ({msg[:120]}). The site may be down or the address wrong."
    return msg[:300]


async def robust_click(page: Any, target: Any, tries: int = CLICK_TRIES, what: str = "the element") -> None:
    """Click with retries: scroll into view, click, and on a temporary failure let the page settle and try again.
    Never forces a click through a covering element (that could press something the agent did not choose)."""
    last: BaseException | None = None
    for i in range(max(1, tries)):
        try:
            with contextlib.suppress(Exception):
                await target.scroll_into_view_if_needed(timeout=2000)
            await target.click(timeout=8000 if i == 0 else 4000)
            return
        except Exception as e:
            last = e
            low = str(e).lower()
            if "not attached" in low or "detached" in low:
                break  # the element is gone: retrying the same handle cannot work
            await settle(page, idle_ms=600)
    raise ActionFailed(explain(last or RuntimeError("click failed"), what))


QUIET_TOOLS = frozenset({"browser_read", "browser_wait"})  # look only: no "Delta is in control" signal


class _BrowserTool[A: BaseModel](Tool[A]):
    def __init__(self, mgr: BrowserManager) -> None:
        self.mgr = mgr

    def domains(self, args: A) -> list[str]:
        return [self.mgr.current_url] if self.mgr.current_url.startswith("http") else []

    async def _guarded(self, coro_fn: Any) -> ToolResult:
        from delta.system.agent_cursor import driving

        # Acting in Delta's browser window shows the "Delta is in control" ring; reading does not.
        signal = contextlib.nullcontext() if self.name in QUIET_TOOLS else driving()
        try:
            with signal:
                return await coro_fn()
        except ActionFailed as e:
            return ToolResult(False, f"{self.name} failed: {e}")
        except Exception as e:  # playwright timeouts etc. are normal outcomes for the agent
            first = str(e).splitlines()[0][:300] if str(e) else ""
            hint = explain(e, "the page")
            return ToolResult(False, f"{type(e).__name__}: {first}" + (f" ({hint})" if hint not in first else ""))


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
            return await _observed(page, self.mgr, f"opened {await page.title()} ({page.url})", force_full=True)

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
            before = await page_state(page)
            await robust_click(page, target, what=f"'{label}'")
            await settle(page)
            return await _observed(page, self.mgr, f"clicked '{label}', now at {page.url}", before=before)

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
            before = await page_state(page)
            try:
                await box.fill(args.value, timeout=8000)
            except Exception as e:
                raise ActionFailed(explain(e, f"'{label}'")) from e
            check = ""
            with contextlib.suppress(Exception):  # self-check: the field holds what was typed
                now = str(await box.input_value(timeout=2000))
                if now != args.value:
                    check = (f"\nSelf-check: the field now holds {len(now)} characters, not the {len(args.value)} "
                             "typed (the site may format or limit it). Check it before submitting.")
            self.mgr.last_fill_was_search = is_search
            if args.submit:
                await box.press("Enter")
                await settle(page)
            done = "filled and submitted" if args.submit else "filled"
            return await _observed(page, self.mgr, f"{done} '{label}'{check}",
                                   before=before if args.submit else None)

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
            return await _observed(page, self.mgr, f"searched '{args.query}' ({page.url})", force_full=True)

        return await self._guarded(go)


class ScrollArgs(BaseModel):
    direction: str = Field(default="down", pattern="^(up|down)$")
    pages: float = Field(default=1, gt=0, le=10)
    ref: int | None = Field(default=None, description="Scroll inside this element (a list or panel) instead of "
                                                      "the page, or bring this element into view")
    to: Literal["", "top", "bottom"] = Field(default="", description="Jump to the top or bottom instead")
    load_more: bool = Field(default=False, description="Infinite lists: keep scrolling down until no new items "
                                                       "appear (at most 10 rounds)")


# Scrolls the element numbered %(ref)d, or the page when it is not scrollable itself, by dy pixels (or to an end).
SCROLL_JS = """(el, a) => {
  const scrollable = (n) => n && n.scrollHeight > n.clientHeight + 2 &&
    ['auto', 'scroll'].includes(getComputedStyle(n).overflowY);
  let box = el;
  while (box && box !== document.body && !scrollable(box)) box = box.parentElement;
  const target = (box && box !== document.body) ? box : document.scrollingElement;
  if (a.to === 'top') target.scrollTop = 0;
  else if (a.to === 'bottom') target.scrollTop = target.scrollHeight;
  else target.scrollTop += a.dy;
  return {top: Math.round(target.scrollTop), height: target.scrollHeight, inner: target !== document.scrollingElement};
}"""
LOAD_MORE_ROUNDS = 10


class BrowserScroll(_BrowserTool[ScrollArgs]):
    name = "browser_scroll"
    description = ("Scroll the page (or, with ref, the list or panel that element is in) up or down by screen "
                   "heights; to=top/bottom jumps; load_more=true keeps scrolling an infinite list until nothing "
                   "new loads.")
    risk = Risk.ACT
    Args = ScrollArgs

    async def run(self, args: ScrollArgs) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            before = await page_state(page)
            height = await page.evaluate("window.innerHeight")
            sign = 1 if args.direction == "down" else -1
            target = None
            if args.ref is not None:
                try:
                    target, _, _ = await _ref_target(page, self.mgr, args.ref)
                except StaleRef as e:
                    return ToolResult(False, str(e))
            what = f"scrolled {args.direction}" if not args.to else f"scrolled to the {args.to}"
            if args.load_more:
                rounds, last = 0, -1
                for rounds in range(1, LOAD_MORE_ROUNDS + 1):  # noqa: B007 (rounds is reported)
                    if target is not None:
                        info = await target.evaluate(SCROLL_JS, {"to": "bottom", "dy": 0})
                        size = int(info.get("height", 0)) if isinstance(info, dict) else 0
                    else:
                        await page.mouse.wheel(0, height * 3)
                        size = int(await page.evaluate("document.documentElement.scrollHeight") or 0)
                    await settle(page, idle_ms=1500)
                    if size == last:
                        break
                    last = size
                what = f"scrolled to the end of the list ({rounds} round{'s' if rounds != 1 else ''})"
            elif target is not None:
                if not args.to and args.pages == 1 and args.direction == "down":
                    with contextlib.suppress(Exception):
                        await target.scroll_into_view_if_needed(timeout=3000)
                await target.evaluate(SCROLL_JS, {"to": args.to, "dy": height * args.pages * sign})
                await settle(page, idle_ms=800)
            elif args.to:
                await page.evaluate(f"window.scrollTo(0, {'0' if args.to == 'top' else 'document.body.scrollHeight'})")
                await settle(page, idle_ms=800)
            else:
                await page.mouse.wheel(0, height * args.pages * sign)
                await settle(page, idle_ms=800)
            return await _observed(page, self.mgr, what, before=before)

        return await self._guarded(go)


class BrowserBack(_BrowserTool[NoArgs]):
    name = "browser_back"
    description = "Go back to the previous page."
    risk = Risk.ACT
    Args = NoArgs

    async def run(self, args: NoArgs) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            # "commit", not "domcontentloaded": a page restored from the back-forward cache fires no
            # load events, so waiting for one hung for the full 15 s. settle() then waits for the page.
            await page.go_back(wait_until="commit", timeout=15000)
            await settle(page)
            return await _observed(page, self.mgr, "went back", force_full=True)

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
    from delta.tools.browser_actions import more_browser_tools

    return [BrowserOpen(mgr), BrowserSearch(mgr), BrowserRead(mgr), BrowserClick(mgr), BrowserFill(mgr),
            BrowserPress(mgr), BrowserScroll(mgr), BrowserBack(mgr), *more_browser_tools(mgr)]
