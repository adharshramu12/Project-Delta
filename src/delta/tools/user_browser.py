"""Your browser mode: Delta uses YOUR normal, signed-in Chrome or Edge, like a person would.

Chrome (136+) refuses remote control of the default profile, to protect logged-in sessions
from malware, and copied cookies stop working (they are encrypted to the real profile). So
instead of a debugging port, Delta drives one window of your real browser through Windows UI
Automation (what screen readers use) plus keyboard input:

- read, click and Back go through accessibility: no mouse movement, work in the background
- typing (address bar, form fields) only happens while Delta's window is really in front,
  never into another window; password fields are refused; moving the mouse stops Delta
- Delta opens ONE window of its own and keeps using it, so your tabs are left alone; it is
  never closed by Delta (it is your browser)
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import time
from typing import Any

import psutil

from delta.tools.base import Risk, Tool, ToolResult
from delta.tools.browser import (
    SEARCHY,
    ClickArgs,
    FillArgs,
    NoArgs,
    OpenArgs,
    PressArgs,
    ReadArgs,
    ScrollArgs,
    SearchArgs,
)
from delta.tools.desktop import (
    DesktopController,
    UserOverrideError,
    _focused_is_password,
    _is_foreground,
    _pg,
    _send_unicode,
    click_risk,
)

KNOWN = {  # registry ProgId prefix -> (name, process, install paths)
    "ChromeHTML": ("chrome", "chrome.exe", [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                                            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                                            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"]),
    "MSEdge": ("edge", "msedge.exe", [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                                      r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"]),
}
PASTE_OVER = 200  # longer values are pasted in one go (the user's clipboard is put back afterwards)


def _paste(text: str) -> None:
    """Paste `text` with Ctrl+V, then restore whatever text the user had on the clipboard."""
    import win32clipboard as cb

    def put(value: str | None) -> None:
        cb.OpenClipboard()
        try:
            cb.EmptyClipboard()
            if value is not None:
                cb.SetClipboardText(value, cb.CF_UNICODETEXT)
        finally:
            cb.CloseClipboard()

    saved: str | None = None
    with contextlib.suppress(Exception):
        cb.OpenClipboard()
        try:
            if cb.IsClipboardFormatAvailable(cb.CF_UNICODETEXT):
                saved = cb.GetClipboardData(cb.CF_UNICODETEXT)
        finally:
            cb.CloseClipboard()
    put(text)
    _pg().hotkey("ctrl", "v")
    time.sleep(0.4)  # let the page take the paste before the clipboard changes back
    with contextlib.suppress(Exception):
        put(saved)


# Menu items in web apps (ChatGPT's "+" menu, Gemini's Tools) only react to a real pointer: UIA Invoke
# reports success and nothing happens. These get a real mouse click on Delta's own window, in front.
POINTER_ONLY = {"MenuItem", "ListItem", "RadioButton", "CheckBox"}
CLICKABLE = {"Hyperlink", "Button", "MenuItem", "CheckBox", "RadioButton", "TabItem", "ListItem", "SplitButton"}
FIELDS = {"Edit", "ComboBox"}
READ_TYPES = {"Hyperlink": "link", "Button": "button", "Edit": "field", "ComboBox": "field", "CheckBox": "checkbox",
              "RadioButton": "option", "TabItem": "tab", "MenuItem": "menu"}
MAX_READ_NODES = 900
MAX_READ_CHARS = 7000  # one page must leave room for history within free-tier request limits
FOCUS_CONTEXT = 2


def select_lines(lines: list[str], focus: str | None, budget: int) -> str:
    """Whole page up to `budget`, or with `focus`, only lines mentioning those words (plus a little
    context): a long article becomes the few lines that answer the question."""
    if focus:
        words = [w for w in re.findall(r"\w{3,}", focus.lower())] or [focus.lower()]
        keep: set[int] = set()
        for i, line in enumerate(lines):
            if any(w in line.lower() for w in words):
                keep.update(range(max(0, i - FOCUS_CONTEXT), min(len(lines), i + FOCUS_CONTEXT + 1)))
        if keep:
            picked, out, size, prev = sorted(keep), [], 0, -2
            for i in picked:
                if i != prev + 1:
                    out.append("…")
                out.append(lines[i])
                size += len(lines[i])
                prev = i
                if size > budget // 2:
                    out.append("…[more matches; narrow the focus]")
                    break
            return "\n".join(out)
        lines = [f"(nothing on this page mentions '{focus}'; showing the start of the page)", *lines]
    out, size = [], 0
    for line in lines:
        out.append(line)
        size += len(line)
        if size > budget:
            out.append("…[page continues; use browser_read with a focus, or browser_scroll]")
            break
    return "\n".join(out)


def default_browser(prefer: str = "auto") -> tuple[str, str, str] | None:
    """(name, exe, process name) to use: the preferred browser if installed ('chrome'/'edge'),
    else the Windows default browser if it is Chrome or Edge, else whichever is installed."""
    prog = ""
    with contextlib.suppress(OSError):
        import winreg

        key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            prog = str(winreg.QueryValueEx(k, "ProgId")[0])
    def rank(kv: tuple[str, tuple[str, str, list[str]]]) -> tuple[bool, bool]:
        return (kv[1][0] != prefer, not prog.startswith(kv[0]))

    order = sorted(KNOWN.items(), key=rank)  # preferred first, then the Windows default
    for _, (name, proc, paths) in order:
        for p in (os.path.expandvars(x) for x in paths):
            if os.path.exists(p):
                return name, p, proc
    return None


class UserBrowser:
    """State for Delta's one window in the user's browser. All UIA calls run on the desktop
    controller's thread (COM objects belong to the thread that created them)."""

    def __init__(self, ctl: DesktopController, prefer: str = "auto") -> None:
        self.ctl = ctl
        self.handle: int | None = None
        self.current_url = ""
        self.last_fill_was_search = False
        self.use(prefer)

    def use(self, prefer: str) -> None:
        """Switch browser (Settings page); the next action opens a window there."""
        found = default_browser(prefer)
        self.name, self.exe, self.process = found if found else ("", "", "")
        self.handle = None

    # --- window ------------------------------------------------------------------------------
    def _window(self) -> Any | None:
        import win32gui
        from pywinauto import Desktop

        if self.handle and win32gui.IsWindow(self.handle):
            return Desktop(backend="uia").window(handle=self.handle).wrapper_object()
        self.handle = None
        return None

    def _browser_windows(self) -> dict[int, Any]:
        from pywinauto import Desktop

        out = {}
        for w in Desktop(backend="uia").windows():
            with contextlib.suppress(Exception):
                if w.is_visible() and psutil.Process(w.process_id()).name().lower() == self.process:
                    out[w.handle] = w
        return out

    def _new_window(self, url: str) -> Any:
        if not self.exe:
            raise LookupError("Neither Google Chrome nor Microsoft Edge is installed.")
        before = set(self._browser_windows())
        subprocess.Popen([self.exe, "--new-window", url], close_fds=True)
        for _ in range(40):
            time.sleep(0.25)
            new = [w for h, w in self._browser_windows().items() if h not in before]
            if new:
                self.handle = new[0].handle
                target = url.split("://", 1)[-1].split("/", 1)[0].lower()
                end = time.monotonic() + 12  # wait until the requested site (not the New Tab page) loads
                while time.monotonic() < end:
                    time.sleep(0.4)
                    with contextlib.suppress(Exception):
                        if target in self._url(new[0]).lower() and self._document(new[0]) is not None:
                            break
                time.sleep(0.6)
                return new[0]
        raise LookupError(f"{self.name} did not open a window")

    def _front(self, w: Any) -> None:
        """Typing must only ever reach Delta's window."""
        if not _is_foreground(w):
            with contextlib.suppress(Exception):
                if w.is_minimized():
                    w.restore()
            w.set_focus()
            time.sleep(0.25)
            if not _is_foreground(w):
                raise LookupError("Windows did not let me bring the browser window to the front, so I did not "
                                  "type anything. Click the browser window once, then ask me again.")

    # --- page ----------------------------------------------------------------------------------
    @staticmethod
    def _document(w: Any, wait_s: float = 0) -> Any | None:
        """The page's Document element. Browsers build the accessibility view lazily (and a new
        window shows a New Tab page first), so callers that need content wait for it."""
        end = time.monotonic() + wait_s
        while True:
            docs: list[Any] = []
            with contextlib.suppress(Exception):
                docs = list(w.descendants(control_type="Document"))
            for doc in docs:  # each checked on its own: one stale element must not hide the rest
                with contextlib.suppress(Exception):
                    if doc.element_info.name:
                        return doc
            if docs:
                return docs[0]
            if time.monotonic() >= end:
                return None
            time.sleep(0.3)

    def _url(self, w: Any) -> str:
        url = ""
        doc = self._document(w)
        with contextlib.suppress(Exception):
            url = str(doc.iface_value.CurrentValue) if doc else ""
        if not url:
            with contextlib.suppress(Exception):
                bar = next(e for e in w.descendants(control_type="Edit")
                           if "address" in (e.element_info.name or "").lower())
                url = bar.get_value()
        if url and "://" not in url:
            url = "https://" + url
        self.current_url = url
        return url

    def evidence(self, w: Any) -> dict[str, Any]:
        doc = self._document(w)
        return {"url": self._url(w), "title": doc.element_info.name if doc else w.window_text(),
                "browser": self.name}

    def _wait_for_change(self, w: Any, old_url: str, timeout: float = 10) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            time.sleep(0.4)
            with contextlib.suppress(Exception):
                if self._url(w) != old_url:
                    time.sleep(0.8)  # content settles after the URL changes
                    return

    def _nodes(self, root: Any, limit: int) -> list[tuple[str, str, bool, Any]]:
        """(name, control type, offscreen, raw element) for the subtree, fetched with one cached call."""
        from pywinauto.uia_defines import IUIA

        u = IUIA()
        dll = u.UIA_dll
        cr = u.iuia.CreateCacheRequest()
        for prop in (dll.UIA_NamePropertyId, dll.UIA_ControlTypePropertyId, dll.UIA_IsOffscreenPropertyId):
            cr.AddProperty(prop)
        cr.TreeScope = dll.TreeScope_Subtree
        cr.TreeFilter = u.iuia.ControlViewCondition
        top = root.element_info.element.BuildUpdatedCache(cr)
        names = {v: k for k, v in u.known_control_types.items()}
        out: list[tuple[str, str, bool, Any]] = []
        stack = [top]
        while stack and len(out) < limit:
            el = stack.pop()
            kids = el.GetCachedChildren()
            children = [kids.GetElement(i) for i in range(kids.Length)] if kids else []
            for child in reversed(children):
                stack.append(child)
            if el is not top:
                out.append(((el.CachedName or "").strip(), names.get(el.CachedControlType, "Custom"),
                            bool(el.CachedIsOffscreen), el))
        return out

    def read(self, focus: str | None = None) -> tuple[str, dict[str, Any]]:
        w = self._window()
        if w is None:
            raise LookupError("No browser window open yet: use browser_open or browser_search first.")
        doc = self._document(w, wait_s=8)
        if doc is None:
            raise LookupError("The browser page is not readable yet; try again in a moment.")
        lines, last = [], ""
        for name, ctype, _off, _el in self._nodes(doc, MAX_READ_NODES * (3 if focus else 1)):
            if not name or name == last:
                continue
            tag = READ_TYPES.get(ctype)
            lines.append(f"[{tag}] {name[:120]}" if tag else name[:300])
            last = name
        ev = self.evidence(w)
        return f"{ev['title']} — {ev['url']}\n\n" + select_lines(lines, focus, MAX_READ_CHARS), ev

    def _find(self, w: Any, text: str, types: set[str]) -> Any | None:
        """Best visible match: exact name, then starts-with, then contains."""
        from pywinauto.controls.uiawrapper import UIAWrapper
        from pywinauto.uia_element_info import UIAElementInfo

        doc = self._document(w, wait_s=8)
        if doc is None:
            return None
        want = text.strip().lower()
        cands = [(n.lower(), off, el) for n, t, off, el in self._nodes(doc, 4000) if t in types and n]
        for test in (lambda n: n == want, lambda n: n.startswith(want), lambda n: want in n):
            hits = sorted(((off, el) for n, off, el in cands if test(n)), key=lambda h: h[0])  # on-screen first
            if hits:
                return UIAWrapper(UIAElementInfo(hits[0][1]))
        return None

    # --- actions (all return a short message; raise LookupError for user-facing problems) -------
    def open(self, url: str) -> str:
        url = url if "://" in url or url.startswith("about:") else f"https://{url}"
        w = self._window()
        if w is None:
            w = self._new_window(url)
        else:
            self._navigate(w, url)
        return f"opened in your {self.name}"

    def search(self, query: str) -> str:
        w = self._window()
        if w is None:
            from urllib.parse import quote_plus

            self._new_window(f"https://www.google.com/search?q={quote_plus(query)}")
        else:
            self._navigate(w, query)  # address bar search = the user's own default engine
        return f"searched '{query}' in your {self.name}"

    def _navigate(self, w: Any, text: str) -> None:
        self.ctl.guard()
        self._front(w)
        old = self._url(w)
        pg = _pg()
        pg.hotkey("ctrl", "l")  # focus the address bar
        time.sleep(0.15)
        _send_unicode(text + "\n")
        self.ctl.mark()
        self._wait_for_change(w, old)

    def click(self, text: str) -> str:
        w = self._window()
        if w is None:
            raise LookupError("No browser window open yet.")
        el = self._find(w, text, CLICKABLE)
        if el is None:
            raise LookupError(f"Nothing called '{text}' on the page; use browser_read to see what is there.")
        old = self._url(w)
        try:
            if el.element_info.control_type in POINTER_ONLY:
                raise RuntimeError("web menus ignore an accessibility click")
            el.invoke()  # accessibility click: no mouse, works in the background
        except Exception:
            self.ctl.guard()
            self._front(w)
            el.click_input()
            self.ctl.mark()
        self._wait_for_change(w, old, timeout=6)
        return f"clicked '{el.element_info.name}'"

    def fill(self, field: str, value: str, submit: bool) -> str:
        w = self._window()
        if w is None:
            raise LookupError("No browser window open yet.")
        el = self._find(w, field, FIELDS)
        if el is None and SEARCHY.search(field):
            el = self._find(w, "search", FIELDS)
        if el is None:
            raise LookupError(f"No field called '{field}'; use browser_read to see the page.")
        with contextlib.suppress(Exception):
            if el.element_info.element.CurrentIsPassword:
                raise LookupError("REFUSED: that is a password field. Ask the user to type it.")
        self.ctl.guard()
        self._front(w)
        el.set_focus()
        time.sleep(0.15)
        if _focused_is_password():
            raise LookupError("REFUSED: that is a password field. Ask the user to type it.")
        _pg().hotkey("ctrl", "a")
        if len(value) > PASTE_OVER:
            _paste(value)  # a long brief typed key by key takes a minute and breaks if anything moves
        else:
            _send_unicode(value, soft_newlines=True)  # only `submit` may send; a newline must not
        self.last_fill_was_search = bool(SEARCHY.search(f"{field} {el.element_info.name}"))
        old = self._url(w)
        if submit:
            _pg().press("enter")
            self._wait_for_change(w, old, timeout=8)
        self.ctl.mark()
        return ("filled and submitted" if submit else "filled") + f" '{el.element_info.name or field}'"

    def press(self, key: str) -> str:
        w = self._window()
        if w is None:
            raise LookupError("No browser window open yet.")
        self.ctl.guard()
        self._front(w)
        _pg().press(key.lower())
        self.ctl.mark()
        if key.lower() in {"enter", "return"}:
            self.last_fill_was_search = False
        return f"pressed {key}"

    def scroll(self, direction: str, pages: float) -> str:
        w = self._window()
        if w is None:
            raise LookupError("No browser window open yet.")
        doc = self._document(w)
        with contextlib.suppress(Exception):
            if doc is not None:
                for _ in range(max(1, round(pages))):
                    doc.scroll(direction, "page")  # UIA scroll pattern: no focus needed
                return f"scrolled {direction}"
        self.ctl.guard()
        self._front(w)
        _pg().press("pagedown" if direction == "down" else "pageup", presses=max(1, round(pages)))
        self.ctl.mark()
        return f"scrolled {direction}"

    def back(self) -> str:
        w = self._window()
        if w is None:
            raise LookupError("No browser window open yet.")
        old = self._url(w)
        btn = next((b for b in w.descendants(control_type="Button") if b.element_info.name == "Back"), None)
        if btn is None:
            raise LookupError("Could not find the browser's Back button.")
        btn.invoke()
        self._wait_for_change(w, old, timeout=6)
        return "went back"


class _UBTool(Tool[Any]):
    def __init__(self, ub: UserBrowser) -> None:
        self.ub = ub

    def domains(self, args: Any) -> list[str]:
        return [self.ub.current_url] if self.ub.current_url.startswith("http") else []

    async def _do(self, fn: Any, *args: Any, read: bool = False, resumed: bool = False) -> ToolResult:
        try:
            if read:
                text, ev = await self.ub.ctl.call(self.ub.read, *args)
                return ToolResult(True, text, ev)
            with self.ub.ctl.acting():  # orange pointer while Delta drives the browser
                msg = await self.ub.ctl.call(fn, *args)
            ev = await self.ub.ctl.call(lambda: self.ub.evidence(self.ub._window()) if self.ub._window() else {})
            title = ev.get("title", "")
            return ToolResult(True, f"{msg}: {title} ({ev.get('url', '')})" if title else msg, ev)
        except UserOverrideError as e:
            # Pause, don't quit: carry on once the user leaves the mouse alone for a few seconds.
            if not resumed and await self.ub.ctl.wait_until_idle():
                return await self._do(fn, *args, read=read, resumed=True)
            return ToolResult(False, f"STOPPED: {e}, and you kept using the computer. Do not continue; "
                                     "tell the user where you got to.")
        except LookupError as e:
            return ToolResult(False, str(e))
        except Exception as e:  # UIA hiccups are normal outcomes for the agent to react to
            return ToolResult(False, f"{type(e).__name__}: {str(e)[:200]}")


class UBOpen(_UBTool):
    name = "browser_open"
    description = "Open a URL in the user's own browser (signed in to their sites)."
    risk = Risk.ACT
    Args = OpenArgs

    def domains(self, args: OpenArgs) -> list[str]:
        return [args.url]

    async def run(self, args: OpenArgs) -> ToolResult:
        return await self._do(self.ub.open, args.url)


class UBSearch(_UBTool):
    name = "browser_search"
    description = "Search the web in the user's own browser with their default search engine."
    risk = Risk.ACT
    Args = SearchArgs

    async def run(self, args: SearchArgs) -> ToolResult:
        return await self._do(self.ub.search, args.query)


class UBRead(_UBTool):
    name = "browser_read"
    description = ("Read the current page in the user's browser: title, URL, text, links, buttons, fields. "
                   "Pass focus (e.g. 'born', 'price') to get only the relevant parts of long pages.")
    risk = Risk.READ
    Args = ReadArgs

    async def run(self, args: ReadArgs) -> ToolResult:
        return await self._do(None, args.focus, read=True)


class UBClick(_UBTool):
    name = "browser_click"
    description = "Click a link or button on the current page by its visible text."
    risk = Risk.ACT
    Args = ClickArgs

    def risk_for(self, args: ClickArgs) -> Risk:
        return click_risk(args.text)

    def summary(self, args: ClickArgs) -> str:
        return f"click '{args.text}' on {self.ub.current_url or 'the page'}"

    async def run(self, args: ClickArgs) -> ToolResult:
        return await self._do(self.ub.click, args.text)


class UBFill(_UBTool):
    name = "browser_fill"
    description = ("Type into a field by its label or placeholder; submit=true presses Enter. "
                   "Never for passwords or card numbers.")
    risk = Risk.ACT
    Args = FillArgs

    def risk_for(self, args: FillArgs) -> Risk:
        return Risk.EXTERNAL if args.submit and not SEARCHY.search(args.field) else Risk.ACT

    def summary(self, args: FillArgs) -> str:
        return f"{'fill and submit' if args.submit else 'fill'} '{args.field}' on {self.ub.current_url or 'the page'}"

    async def run(self, args: FillArgs) -> ToolResult:
        return await self._do(self.ub.fill, args.field, args.value, args.submit)


class UBPress(_UBTool):
    name = "browser_press"
    description = "Press a key in the browser page."
    risk = Risk.ACT
    Args = PressArgs

    def risk_for(self, args: PressArgs) -> Risk:
        if args.key.lower() in {"enter", "return"} and not self.ub.last_fill_was_search:
            return Risk.EXTERNAL
        return Risk.ACT

    async def run(self, args: PressArgs) -> ToolResult:
        return await self._do(self.ub.press, args.key)


class UBScroll(_UBTool):
    name = "browser_scroll"
    description = "Scroll the current page up or down by screen heights."
    risk = Risk.ACT
    Args = ScrollArgs

    async def run(self, args: ScrollArgs) -> ToolResult:
        return await self._do(self.ub.scroll, args.direction, args.pages)


class UBBack(_UBTool):
    name = "browser_back"
    description = "Go back to the previous page."
    risk = Risk.ACT
    Args = NoArgs

    async def run(self, args: NoArgs) -> ToolResult:
        return await self._do(self.ub.back)


def user_browser_tools(ctl: DesktopController, prefer: str = "auto") -> list[Tool]:
    from delta.tools.user_browser_actions import user_browser_more_tools

    ub = UserBrowser(ctl, prefer)
    return [UBOpen(ub), UBSearch(ub), UBRead(ub), UBClick(ub), UBFill(ub), UBPress(ub), UBScroll(ub), UBBack(ub),
            *user_browser_more_tools(ub)]


__all__ = ["UserBrowser", "default_browser", "user_browser_tools"]
