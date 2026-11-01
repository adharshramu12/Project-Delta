"""Desktop control: read windows through Windows UI Automation (text, no screenshots, no GPU),
then click and type like a person.

Safety:
- If you move the mouse while Delta works, Delta notices, stops and backs off for 30 s.
- pyautogui fail-safe: slam the mouse into a screen corner to abort instantly.
- Typing into password fields is refused. Clicking buttons named like Send/Pay/Delete is
  classified EXTERNAL/DESTRUCTIVE, so it needs your approval.

All UI Automation calls run on one dedicated thread, because COM objects belong to the thread
that created them.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from delta.tools.base import Risk, Tool, ToolResult

T = TypeVar("T")

SENDY = re.compile(r"\b(send|submit|pay|buy|purchase|order|checkout|post|publish|transfer|share|sign up|"
                   r"confirm|accept|install|uninstall)\b", re.I)
DELETY = re.compile(r"\b(delete|remove|erase|discard|empty|format|reset|clear all)\b", re.I)
MAX_NODES = 250
OVERRIDE_PX = 40
BACKOFF_S = 30


class UserOverrideError(RuntimeError):
    pass


def _pg() -> Any:
    """pyautogui, imported on first use and configured for safety."""
    import pyautogui

    pyautogui.FAILSAFE = True  # mouse in a screen corner aborts any action
    pyautogui.PAUSE = 0.05
    return pyautogui


class DesktopController:
    """Owns the UIA thread, the input devices and the 'user took over' detection."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="delta-uia")
        self._last_pos: tuple[int, int] | None = None
        self._paused_until = 0.0
        self.position: Callable[[], tuple[int, int]] = self._real_position

    async def call(self, fn: Callable[..., T], *args: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    @staticmethod
    def _real_position() -> tuple[int, int]:
        p = _pg().position()
        return int(p[0]), int(p[1])

    def guard(self) -> None:
        """Call before every input action."""
        now = time.monotonic()
        if now < self._paused_until:
            raise UserOverrideError(f"you are using the computer; desktop actions paused for "
                                    f"{int(self._paused_until - now)} more seconds")
        if self._last_pos is not None:
            x, y = self.position()
            lx, ly = self._last_pos
            if abs(x - lx) > OVERRIDE_PX or abs(y - ly) > OVERRIDE_PX:
                self._paused_until = now + BACKOFF_S
                self._last_pos = None
                raise UserOverrideError("you moved the mouse, so I stopped and handed control back to you")

    def mark(self) -> None:
        """Call after every input action: remember where *we* left the mouse."""
        self._last_pos = self.position()

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


# --- UIA helpers (run on the UIA thread) ------------------------------------------------------

def _desktop() -> Any:
    from pywinauto import Desktop

    return Desktop(backend="uia")


def _foreground() -> Any:
    import win32gui

    return _desktop().window(handle=win32gui.GetForegroundWindow()).wrapper_object()


def _find_window(title: str | None) -> Any:
    """Active window, or a visible window/dialog whose title contains `title`.
    Dialogs (Save as, Open…) live inside their app window, so those are searched too;
    an open dialog wins over its parent because it is where input goes."""
    if not title:
        return _foreground()
    t = title.lower()
    tops = [w for w in _desktop().windows() if w.is_visible()]
    for w in tops:
        for c in w.children():
            if c.element_info.control_type == "Window" and t in c.window_text().lower():
                return c
    for w in tops:
        if t in w.window_text().lower():
            return w
    raise LookupError(f"no visible window with '{title}' in its title")


LEAF_GROUPS = {"ListItem", "DataItem", "TreeItem"}  # their children are column cells: pure noise


def _walk(elem: Any, depth: int, out: list[str], max_depth: int) -> None:
    if len(out) >= MAX_NODES or depth > max_depth:
        return
    for child in elem.children():
        if len(out) >= MAX_NODES:
            return
        info = child.element_info
        name, ctype = (info.name or "").strip(), info.control_type or ""
        if name or ctype in {"Edit", "Document", "ComboBox"}:
            try:
                r = child.rectangle()
                where = f" @({r.mid_point().x},{r.mid_point().y})" if r.width() and r.height() else ""
            except Exception:
                where = ""
            out.append(f"{'  ' * depth}[{ctype}] {name[:80]}{where}")
        if ctype not in LEAF_GROUPS:
            _walk(child, depth + 1, out, max_depth)


def _walk_cached(hwnd: int, out: list[str], max_depth: int) -> None:
    """Fast path for read_window: one UI Automation cache request fetches name, type, position
    and visibility of the whole control tree in a single cross-process call, instead of several
    calls per control. Typically 5-20x faster than walking wrappers."""
    from pywinauto.uia_defines import IUIA

    u = IUIA()
    dll = u.UIA_dll
    cr = u.iuia.CreateCacheRequest()
    for prop in (dll.UIA_NamePropertyId, dll.UIA_ControlTypePropertyId, dll.UIA_BoundingRectanglePropertyId,
                 dll.UIA_IsOffscreenPropertyId):
        cr.AddProperty(prop)
    cr.TreeScope = dll.TreeScope_Subtree
    cr.TreeFilter = u.iuia.ControlViewCondition  # skip raw layout noise, like Inspect's control view
    root = u.iuia.ElementFromHandle(hwnd).BuildUpdatedCache(cr)
    names = {v: k for k, v in u.known_control_types.items()}

    def visit(el: Any, depth: int) -> None:
        if len(out) >= MAX_NODES or depth > max_depth:
            return
        kids = el.GetCachedChildren()
        for i in range(kids.Length if kids else 0):
            if len(out) >= MAX_NODES:
                return
            child = kids.GetElement(i)
            name = (child.CachedName or "").strip()
            ctype = names.get(child.CachedControlType, "Custom")
            if (name or ctype in {"Edit", "Document", "ComboBox"}) and not child.CachedIsOffscreen:
                r = child.CachedBoundingRectangle
                w, h = r.right - r.left, r.bottom - r.top
                where = f" @({r.left + w // 2},{r.top + h // 2})" if w > 0 and h > 0 else ""
                out.append(f"{'  ' * depth}[{ctype}] {name[:80]}{where}")
            if ctype not in LEAF_GROUPS:
                visit(child, depth + 1)

    visit(root, 0)


def _is_foreground(w: Any) -> bool:
    import win32gui

    fg = win32gui.GetForegroundWindow()
    try:
        top = w.top_level_parent().handle
    except Exception:
        top = w.handle
    return fg in (w.handle, top) or win32gui.GetAncestor(fg, 3) == top  # GA_ROOTOWNER


def _ensure_foreground(title: str | None) -> Any:
    """Keystrokes go to whatever has focus. Never type unless the intended window really has it."""
    w = _find_window(title)
    if title and not _is_foreground(w):
        w.set_focus()
        time.sleep(0.2)
        if not _is_foreground(w):
            raise LookupError(f"Windows did not let me bring '{w.window_text()}' to the front, so I did not "
                              "type anything (it would go to the wrong window). Use set_field/click_element.")
    return w


def _focused_is_password() -> bool:
    try:
        from pywinauto.uia_defines import IUIA

        return bool(IUIA().iuia.GetFocusedElement().CurrentIsPassword)
    except Exception:
        return False


def _focused_text() -> str | None:
    """Current text of the focused control, if it exposes one (Value or legacy Value pattern)."""
    try:
        from pywinauto.controls.uiawrapper import UIAWrapper
        from pywinauto.uia_defines import IUIA
        from pywinauto.uia_element_info import UIAElementInfo

        el = UIAWrapper(UIAElementInfo(IUIA().iuia.GetFocusedElement()))
        with contextlib.suppress(Exception):
            if v := el.legacy_properties().get("Value"):
                return str(v)
        with contextlib.suppress(Exception):
            return str(el.get_value())
    except Exception:
        pass
    return None


def unicode_inputs(text: str) -> list[tuple[int, int, int]]:
    """(vk, scan, flags) per key event. Characters go as KEYEVENTF_UNICODE so no keyboard layout,
    modifier or IME can reinterpret them; newline/tab are real Enter/Tab keys."""
    KEYUP, UNICODE = 0x0002, 0x0004
    special = {"\n": 0x0D, "\t": 0x09}
    events: list[tuple[int, int, int]] = []
    for ch in text.replace("\r\n", "\n"):
        if ch in special:
            events += [(special[ch], 0, 0), (special[ch], 0, KEYUP)]
            continue
        units = ch.encode("utf-16-le")
        for i in range(0, len(units), 2):  # astral chars (emoji) are two UTF-16 units
            code = int.from_bytes(units[i:i + 2], "little")
            events += [(0, code, UNICODE), (0, code, UNICODE | KEYUP)]
    return events


def _send_unicode(text: str) -> None:
    import ctypes
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    send = ctypes.windll.user32.SendInput
    for vk, scan, flags in unicode_inputs(text):
        inp = INPUT(type=1, ki=KEYBDINPUT(vk, scan, flags, 0, 0))
        send(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.01)  # WinUI apps (new Notepad) drop keys that arrive faster than this


def click_risk(name: str) -> Risk:
    if DELETY.search(name):
        return Risk.DESTRUCTIVE
    if SENDY.search(name):
        return Risk.EXTERNAL
    return Risk.ACT


# --- tools -------------------------------------------------------------------------------------

class _DesktopTool[A: BaseModel](Tool[A]):
    def __init__(self, ctl: DesktopController) -> None:
        self.ctl = ctl

    async def _act(self, fn: Callable[[], str], evidence: Callable[[], dict[str, Any]] | None = None) -> ToolResult:
        try:
            self.ctl.guard()
            msg = await self.ctl.call(fn)
            await asyncio.sleep(0.4)  # let the UI react before we look again
            self.ctl.mark()
            ev = await self.ctl.call(evidence) if evidence else {}
            return ToolResult(True, msg, ev)
        except UserOverrideError as e:
            return ToolResult(False, f"STOPPED: {e}. Do not continue desktop actions; tell the user.")
        except LookupError as e:
            return ToolResult(False, str(e))


def _window_evidence() -> dict[str, Any]:
    w = _foreground()
    return {"foreground_window": w.window_text()}


class NoArgs(BaseModel):
    pass


class ListWindows(_DesktopTool[NoArgs]):
    name = "list_windows"
    description = "List visible top-level windows (title and process). Use to find or verify apps."
    risk = Risk.READ
    Args = NoArgs

    async def run(self, args: NoArgs) -> ToolResult:
        def go() -> list[str]:
            return [w.window_text() for w in _desktop().windows() if w.is_visible() and w.window_text()]

        titles = await self.ctl.call(go)
        return ToolResult(True, "\n".join(titles) or "(none)", {"windows": titles})


class ReadArgs(BaseModel):
    window: str | None = Field(default=None, description="Part of the window title; default: active window")
    depth: int = Field(default=6, le=12)


class ReadWindow(_DesktopTool[ReadArgs]):
    name = "read_window"
    description = ("Read a window's controls as text (buttons, fields, menus, with screen positions). "
                   "Cheap and fast: always try this before take_screenshot/look_at_screen.")
    risk = Risk.READ
    Args = ReadArgs

    async def run(self, args: ReadArgs) -> ToolResult:
        def go() -> tuple[str, str]:
            w = _find_window(args.window)
            out: list[str] = []
            try:
                _walk_cached(w.handle, out, args.depth)
            except Exception:  # some apps reject cache requests: fall back to the slow walk
                out.clear()
                _walk(w, 0, out, args.depth)
            return w.window_text(), "\n".join(out)

        try:
            title, tree = await self.ctl.call(go)
        except LookupError as e:
            return ToolResult(False, str(e))
        return ToolResult(True, f"WINDOW: {title}\n{tree or '(no readable controls; try look_at_screen)'}",
                          {"window": title})


class FocusArgs(BaseModel):
    window: str = Field(description="Part of the window title")


class FocusWindow(_DesktopTool[FocusArgs]):
    name = "focus_window"
    description = "Bring a window to the front."
    risk = Risk.ACT
    Args = FocusArgs

    async def run(self, args: FocusArgs) -> ToolResult:
        def go() -> str:
            w = _find_window(args.window)
            w.set_focus()
            return f"focused {w.window_text()}"

        return await self._act(go, _window_evidence)


class ClickElementArgs(BaseModel):
    name: str = Field(description="Visible name of the button/link/field, as shown by read_window")
    control_type: str | None = Field(default=None, description="e.g. Button, Edit, MenuItem")
    window: str | None = None


class ClickElement(_DesktopTool[ClickElementArgs]):
    name = "click_element"
    description = "Click a control by its visible name in a window (preferred over click_at)."
    risk = Risk.ACT
    Args = ClickElementArgs

    def risk_for(self, args: ClickElementArgs) -> Risk:
        return click_risk(args.name)

    def summary(self, args: ClickElementArgs) -> str:
        return f"click '{args.name}'" + (f" in {args.window}" if args.window else "")

    async def run(self, args: ClickElementArgs) -> ToolResult:
        def go() -> str:
            w = _find_window(args.window)
            want = args.name.lower()
            matches = [el for el in (w.descendants(control_type=args.control_type) if args.control_type
                                     else w.descendants())
                       if want in (el.element_info.name or "").lower() and el.is_visible()]
            # exact name first: "Save" must not hit "Save as type"
            matches.sort(key=lambda el: (el.element_info.name or "").lower() != want)
            for el in matches[:1]:
                how = "invoked"
                try:
                    el.invoke()  # accessibility action: no mouse movement, works without focus
                except Exception:
                    el.click_input()
                    how = "clicked"
                return f"{how} [{el.element_info.control_type}] {el.element_info.name}"
            raise LookupError(f"no visible control named '{args.name}'; call read_window to see what exists")

        return await self._act(go, _window_evidence)


class ClickAtArgs(BaseModel):
    x: int
    y: int
    button: str = Field(default="left", pattern="^(left|right)$")
    double: bool = False


class ClickAt(_DesktopTool[ClickAtArgs]):
    name = "click_at"
    description = "Click at screen coordinates. Only when click_element cannot find the control."
    risk = Risk.ACT
    Args = ClickAtArgs

    async def run(self, args: ClickAtArgs) -> ToolResult:
        def go() -> str:
            pyautogui = _pg()
            pyautogui.click(args.x, args.y, clicks=2 if args.double else 1, button=args.button)
            return f"clicked at ({args.x},{args.y})"

        return await self._act(go, _window_evidence)


class TypeArgs(BaseModel):
    text: str
    window: str | None = Field(default=None, description="Focus this window first (part of title)")


class TypeText(_DesktopTool[TypeArgs]):
    name = "type_text"
    description = "Type text into the focused field (newlines press Enter). Refuses password fields."
    risk = Risk.ACT
    Args = TypeArgs

    def summary(self, args: TypeArgs) -> str:
        return f"type {len(args.text)} chars" + (f" into {args.window}" if args.window else "")

    async def run(self, args: TypeArgs) -> ToolResult:
        def go() -> str:
            _ensure_foreground(args.window)
            if _focused_is_password():
                raise LookupError("REFUSED: the focused field is a password field. Ask the user to type it.")
            _send_unicode(args.text)
            return f"typed {len(args.text)} characters"

        def evidence() -> dict[str, Any]:
            ev = _window_evidence()
            text = _focused_text()
            if text is not None:  # proof for the verifier that the text really landed
                ev["field_text_tail"] = text[-300:]
                ev["typed_text_present"] = args.text.strip()[-40:].replace("\n", "\r") in text.replace("\n", "\r")
            return ev

        return await self._act(go, evidence)


class KeysArgs(BaseModel):
    keys: list[str] = Field(description="Keys pressed together, e.g. ['ctrl','s'] or ['enter']")
    window: str | None = Field(default=None, description="Window that must receive the keys (part of title)")


class PressKeys(_DesktopTool[KeysArgs]):
    name = "press_keys"
    description = "Press a key or shortcut, e.g. ['ctrl','s'], ['alt','tab'], ['enter']."
    risk = Risk.ACT
    Args = KeysArgs

    def risk_for(self, args: KeysArgs) -> Risk:
        k = {x.lower() for x in args.keys}
        if k & {"delete", "del"} or {"shift", "delete"} <= k:
            return Risk.DESTRUCTIVE
        if "enter" in k or "return" in k:
            return Risk.WRITE  # Enter can submit forms/messages: needs approval in ask-risky
        if {"alt", "f4"} <= k or {"ctrl", "w"} <= k:
            return Risk.WRITE  # closing things can lose unsaved work
        return Risk.ACT

    def summary(self, args: KeysArgs) -> str:
        return "press " + "+".join(args.keys)

    async def run(self, args: KeysArgs) -> ToolResult:
        def go() -> str:
            _ensure_foreground(args.window)
            pyautogui = _pg()
            pyautogui.hotkey(*[k.lower() for k in args.keys])
            return "pressed " + "+".join(args.keys)

        return await self._act(go, _window_evidence)


class SetFieldArgs(BaseModel):
    name: str = Field(description="Name/label of the text field as shown by read_window, e.g. 'File name:'")
    text: str
    window: str | None = Field(default=None, description="Window or dialog title (part of it)")


class SetField(_DesktopTool[SetFieldArgs]):
    name = "set_field"
    description = ("Fill a text field directly through accessibility (no keystrokes, no focus races). "
                   "Preferred over type_text for form fields and dialogs. Refuses password fields.")
    risk = Risk.ACT
    Args = SetFieldArgs

    def summary(self, args: SetFieldArgs) -> str:
        return f"set field '{args.name}'" + (f" in {args.window}" if args.window else "")

    async def run(self, args: SetFieldArgs) -> ToolResult:
        state: dict[str, Any] = {}

        def go() -> str:
            w = _find_window(args.window)
            want = args.name.lower()
            fields = [el for el in w.descendants(control_type="Edit")
                      if want in (el.element_info.name or "").lower() and el.is_visible()]
            fields.sort(key=lambda el: (el.element_info.name or "").lower() != want)
            if not fields:
                raise LookupError(f"no text field named '{args.name}'; call read_window to see the fields")
            el = fields[0]
            with contextlib.suppress(Exception):
                if el.element_info.element.CurrentIsPassword:
                    raise LookupError("REFUSED: that is a password field. Ask the user to type it.")
            with contextlib.suppress(Exception):
                el.set_focus()
            if _is_foreground(w):
                # Real typing: some apps (the Windows file dialogs) ignore values set behind their back.
                _pg().hotkey("ctrl", "a")
                _send_unicode(args.text)
            else:
                el.set_edit_text(args.text)  # window not in front: never send keys elsewhere
            time.sleep(0.2)
            state["value"] = el.get_value()
            return f"set '{el.element_info.name}'"

        result = await self._act(go)
        if result.ok:
            result.evidence["field_value"] = state.get("value")
            result.ok = state.get("value") == args.text
            if not result.ok:
                result.output += f" but it now reads {state.get('value')!r}"
        return result


class CloseArgs(BaseModel):
    window: str = Field(description="Part of the window or dialog title")


class CloseWindow(_DesktopTool[CloseArgs]):
    name = "close_window"
    description = "Ask a window or dialog to close (like clicking its X; the app may prompt to save)."
    risk = Risk.WRITE  # closing can discard unsaved work, so it is a human decision by default
    Args = CloseArgs

    def summary(self, args: CloseArgs) -> str:
        return f"close window '{args.window}'"

    async def run(self, args: CloseArgs) -> ToolResult:
        def go() -> str:
            import win32con
            import win32gui

            w = _find_window(args.window)
            win32gui.PostMessage(w.handle, win32con.WM_CLOSE, 0, 0)
            return f"asked '{w.window_text()}' to close"

        return await self._act(go)


class ScrollArgs(BaseModel):
    amount: int = Field(description="Positive scrolls up, negative scrolls down (wheel clicks)")


class Scroll(_DesktopTool[ScrollArgs]):
    name = "scroll"
    description = "Scroll the window under the mouse."
    risk = Risk.ACT
    Args = ScrollArgs

    async def run(self, args: ScrollArgs) -> ToolResult:
        def go() -> str:
            pyautogui = _pg()
            pyautogui.scroll(args.amount * 120)
            return f"scrolled {args.amount}"

        return await self._act(go)


def desktop_tools(ctl: DesktopController) -> list[Tool]:
    return [ListWindows(ctl), ReadWindow(ctl), FocusWindow(ctl), ClickElement(ctl), ClickAt(ctl),
            TypeText(ctl), PressKeys(ctl), SetField(ctl), CloseWindow(ctl), Scroll(ctl)]
