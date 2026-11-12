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
from functools import partial
from typing import Any, TypeVar

from pydantic import BaseModel, Field, field_validator
from pydantic.json_schema import SkipJsonSchema

from delta.system.input_stop import StopRequestedError
from delta.tools.base import Risk, Tool, ToolResult

T = TypeVar("T")

# Money leaving or a position changing is never a plain click: buying AND selling, trading, withdrawing.
SENDY = re.compile(r"\b(send|submit|pay|payment|buy|purchase|order|place order|checkout|check out|donate|"
                   r"sell|trade|square off|exit position|withdraw|withdrawal|redeem|invest|top up|add money|"
                   r"subscribe|upgrade|add to cart|book|reserve|post|publish|tweet|reply|comment|transfer|"
                   r"share|sign up|register|confirm|accept|agree|allow|authori[sz]e|install|uninstall)\b", re.I)
DELETY = re.compile(r"\b(delete|remove|erase|discard|empty|format|reset|clear all)\b", re.I)
MAX_NODES = 250
# Shell/system surfaces that are not apps the user thinks of as "open windows".
SYSTEM_WINDOWS = {"Program Manager", "Taskbar", "Status", "Start", "Search", "Notification Center",
                  "Windows Input Experience", "NVIDIA GeForce Overlay"}
OVERRIDE_PX = 40
BACKOFF_S = 30
IDLE_QUIET_S = 4    # the user left the mouse alone this long: Delta may carry on
IDLE_MAX_S = 120    # still busy after this long: stop and hand the task back


class UserOverrideError(RuntimeError):
    pass


def _pg() -> Any:
    """pyautogui, imported on first use and configured for safety."""
    import pyautogui

    from delta.system.input_stop import track_pyautogui

    pyautogui.FAILSAFE = True  # mouse in a screen corner aborts any action
    pyautogui.PAUSE = 0.05
    track_pyautogui(pyautogui)  # Stop lets go of held keys/buttons and refuses the next press
    return pyautogui


class DesktopController:
    """Owns the UIA thread, the input devices and the 'user took over' detection."""

    def __init__(self, cursor: Any = None) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="delta-uia")
        self._last_pos: tuple[int, int] | None = None
        self._paused_until = 0.0
        self.position: Callable[[], tuple[int, int]] = self._real_position
        self.cursor = cursor  # AgentCursor: orange pointer while Delta drives input

    def acting(self) -> Any:
        """Context for any action where Delta drives the mouse or keyboard."""
        return self.cursor.active() if self.cursor else contextlib.nullcontext()

    async def call(self, fn: Callable[..., T], *args: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    def call_now(self, fn: Callable[..., T], *args: Any, timeout: float = 2.0) -> T:
        """Run on the UIA thread and wait, from outside it (policy checks are synchronous)."""
        return self._pool.submit(fn, *args).result(timeout=timeout)

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

    async def wait_until_idle(self, quiet_s: float = IDLE_QUIET_S, max_s: float = IDLE_MAX_S) -> bool:
        """The user grabbed the mouse: do nothing until they leave it alone for `quiet_s`, then
        let Delta continue where it was (True). False if they keep using the PC for `max_s`."""
        loop = asyncio.get_running_loop()
        end = loop.time() + max_s
        last, still_since = self.position(), loop.time()
        while loop.time() < end:
            await asyncio.sleep(0.5)
            pos = self.position()
            if abs(pos[0] - last[0]) > 3 or abs(pos[1] - last[1]) > 3:
                last, still_since = pos, loop.time()
            elif loop.time() - still_since >= quiet_s:
                self._paused_until = 0.0
                self._last_pos = None
                return True
        return False

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
    if fg in (w.handle, top):
        return True
    try:  # no window is in front for a moment while Windows switches windows: fg is 0 and GetAncestor raises
        return bool(fg) and win32gui.GetAncestor(fg, 3) == top  # GA_ROOTOWNER
    except Exception:
        return False


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


def unicode_inputs(text: str, soft_newlines: bool = False) -> list[tuple[int, int, int]]:
    """(vk, scan, flags) per key event. Characters go as KEYEVENTF_UNICODE so no keyboard layout,
    modifier or IME can reinterpret them; newline/tab are real Enter/Tab keys. soft_newlines makes a
    newline Shift+Enter: in chat boxes (Gemini, ChatGPT) a plain Enter sends a half-typed message."""
    KEYUP, UNICODE, SHIFT = 0x0002, 0x0004, 0x10
    special = {"\n": 0x0D, "\t": 0x09}
    events: list[tuple[int, int, int]] = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n" and soft_newlines:
            events += [(SHIFT, 0, 0), (0x0D, 0, 0), (0x0D, 0, KEYUP), (SHIFT, 0, KEYUP)]
            continue
        if ch in special:
            events += [(special[ch], 0, 0), (special[ch], 0, KEYUP)]
            continue
        units = ch.encode("utf-16-le")
        for i in range(0, len(units), 2):  # astral chars (emoji) are two UTF-16 units
            code = int.from_bytes(units[i:i + 2], "little")
            events += [(0, code, UNICODE), (0, code, UNICODE | KEYUP)]
    return events


def _send_unicode(text: str, soft_newlines: bool = False) -> None:
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

    from delta.system.input_stop import check

    send = ctypes.windll.user32.SendInput
    for vk, scan, flags in unicode_inputs(text, soft_newlines):
        check()  # the user pressed Stop: not one more character
        inp = INPUT(type=1, ki=KEYBDINPUT(vk, scan, flags, 0, 0))
        send(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.01)  # WinUI apps (new Notepad) drop keys that arrive faster than this


# --- messaging apps: Enter there SENDS a message ------------------------------------------------
# A raw click + type + Enter in WhatsApp/Telegram/Slack is a message to another person, so it gets
# the same approval as send_message (EXTERNAL) and an approval card that says so in plain words.

def messaging_app(title: str) -> str | None:
    t = title.strip()
    if t == "WhatsApp":
        return "WhatsApp"
    if t.startswith("Telegram"):
        return "Telegram"
    if t == "Slack" or t.endswith("- Slack"):
        return "Slack"
    return None


def _title_at(x: int, y: int) -> str:
    try:
        import win32gui

        return str(win32gui.GetWindowText(win32gui.GetAncestor(win32gui.WindowFromPoint((x, y)), 2)))
    except Exception:
        return ""


def _title_of(window: str | None) -> str:
    """The title of the named window, or of the one in front."""
    try:
        import win32gui

        if window:
            from delta.tools.screen import find_window_handle

            h = find_window_handle(window)
            return str(win32gui.GetWindowText(h)) if h else window
        return str(win32gui.GetWindowText(win32gui.GetForegroundWindow()))
    except Exception:
        return window or ""


def _presses_enter(keys: list[str] | None) -> bool:
    return bool({k.lower() for k in keys or []} & {"enter", "return"})


def click_risk(name: str) -> Risk:
    if DELETY.search(name):
        return Risk.DESTRUCTIVE
    if SENDY.search(name):
        return Risk.EXTERNAL
    return Risk.ACT


# --- what an input lands on (for the policy, before anything moves) -----------------------------
# A target is (top-level window title, window class, process name). None means "could not tell", and
# the executor then asks the human instead of guessing (see delta.safety.exfil_guard).

Target = tuple[str, str, str]
_RISK_ORDER = [Risk.READ, Risk.ACT, Risk.WRITE, Risk.EXTERNAL, Risk.DESTRUCTIVE]
# Control types whose own name says what a click does. Containers (Document, Pane, Window) are named
# after the whole page or app, so a click on a page titled "Delete account" is not a Delete click.
CLICKABLE = {"Button", "SplitButton", "MenuItem", "Hyperlink", "ListItem", "TabItem", "CheckBox",
             "RadioButton", "Image", "Text", "Group", "Custom"}
MAX_LABEL = 40  # longer names are sentences or labels, not what a button says


def _window_info(hwnd: int) -> Target:
    import psutil
    import win32gui
    import win32process

    try:
        top = win32gui.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT
    except Exception:  # the window closed between finding it and asking about it
        top = hwnd
    _, pid = win32process.GetWindowThreadProcessId(top)
    try:
        process = psutil.Process(pid).name()
    except Exception:
        process = ""
    return str(win32gui.GetWindowText(top)), str(win32gui.GetClassName(top)), process


def target_at(x: int, y: int) -> Target | None:
    try:
        import win32gui

        return _window_info(win32gui.WindowFromPoint((x, y)))
    except Exception:
        return None


def target_of(window: str | None) -> Target | None:
    """The window keystrokes will go to: the named one, or the one in front."""
    try:
        import win32gui

        if window:
            from delta.tools.screen import find_window_handle

            h = find_window_handle(window)
            return _window_info(h) if h else None
        return _window_info(win32gui.GetForegroundWindow())
    except Exception:
        return None


def _labels_at(x: int, y: int) -> list[str]:
    """Names of the clickable element under (x, y) and of the button or link it sits in (UIA thread)."""
    from pywinauto.uia_element_info import UIAElementInfo

    el = UIAElementInfo.from_point(x, y)
    names: list[str] = []
    for _ in range(2):  # the element, then its parent (the Text inside a Button)
        if el is None or el.control_type not in CLICKABLE:
            break
        if (name := (el.name or "").strip()) and len(name) <= MAX_LABEL:
            names.append(name)
        if el.control_type not in ("Text", "Image"):
            break
        el = el.parent
    return names


def riskiest(*risks: Risk) -> Risk:
    return max(risks, key=_RISK_ORDER.index)


# --- tools -------------------------------------------------------------------------------------

class _DesktopTool[A: BaseModel](Tool[A]):
    def __init__(self, ctl: DesktopController) -> None:
        self.ctl = ctl

    def labels_at(self, x: int, y: int) -> list[str] | None:
        """What the element under a point is called, or None if it could not be read. Cached for a
        moment: the policy asks for the risk and for the target of the same call."""
        cache: dict[tuple[int, int], tuple[float, list[str] | None]] = self.__dict__.setdefault("_labels", {})
        hit = cache.get((x, y))
        if hit and time.monotonic() - hit[0] < 2:
            return hit[1]
        try:
            names: list[str] | None = self.ctl.call_now(_labels_at, x, y)
        except Exception:
            names = None
        cache.clear()
        cache[(x, y)] = (time.monotonic(), names)
        return names

    def click_risk_at(self, x: int, y: int) -> Risk:
        return riskiest(Risk.ACT, *(click_risk(n) for n in self.labels_at(x, y) or []))

    async def _act(self, fn: Callable[[], str], evidence: Callable[[], dict[str, Any]] | None = None,
                   settle: float = 0.4) -> ToolResult:
        resumed = False
        while True:
            try:
                self.ctl.guard()
                with self.ctl.acting():
                    msg = await self.ctl.call(fn)
                    await asyncio.sleep(settle)  # let the UI react before we look again
                self.ctl.mark()
                ev = await self.ctl.call(evidence) if evidence else {}
                if resumed:
                    msg += " (paused while you used the mouse, then continued)"
                    ev["resumed_after_user"] = True
                return ToolResult(True, msg, ev)
            except UserOverrideError as e:
                # Pause, don't quit: the user is often just glancing at something.
                if not resumed and await self.ctl.wait_until_idle():
                    resumed = True
                    continue
                return ToolResult(False, f"STOPPED: {e}, and you kept using the computer. Do not continue "
                                         "desktop actions; tell the user where you got to.")
            except LookupError as e:
                return ToolResult(False, str(e))
            except StopRequestedError as e:
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
            return [w.window_text() for w in _desktop().windows()
                    if w.is_visible() and w.window_text() and w.window_text() not in SYSTEM_WINDOWS]

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
    # Set only by click_mark (delta.perception): the exact UI Automation element of a mark. Not offered to the
    # model. Used only while that element still has this name; otherwise the name search below runs.
    handle: SkipJsonSchema[str | None] = None


def _marked_element(handle: str | None, name: str) -> Any:
    if not handle:
        return None
    from delta.perception.marks import BOOK

    el = BOOK.handle(handle)
    if el is None:
        return None
    try:
        if (el.element_info.name or "").strip().lower() == name.strip().lower() and el.is_visible():
            return el
    except Exception:  # the element is gone
        return None
    return None


class ClickElement(_DesktopTool[ClickElementArgs]):
    name = "click_element"
    description = "Click a control by its visible name in a window (preferred over click_at)."
    risk = Risk.ACT
    Args = ClickElementArgs

    def risk_for(self, args: ClickElementArgs) -> Risk:
        return click_risk(args.name)

    def summary(self, args: ClickElementArgs) -> str:
        return f"click '{args.name}'" + (f" in {args.window}" if args.window else "")

    def input_targets(self, args: ClickElementArgs) -> list[Target] | None:
        t = target_of(args.window)
        return [t] if t else None

    async def run(self, args: ClickElementArgs) -> ToolResult:
        def go() -> str:
            marked = _marked_element(args.handle, args.name)
            if marked is not None:  # the very element the user's mark pointed at
                how = "invoked"
                try:
                    marked.invoke()
                except Exception:
                    marked.click_input()
                    how = "clicked"
                return f"{how} [{marked.element_info.control_type}] {marked.element_info.name}"
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
    # Set only by click_mark / click_text (delta.perception): the text the click is aimed at (from OCR or a
    # mark). Not offered to the model. It can only make the click count as riskier, never safer.
    expect: SkipJsonSchema[str] = ""


class ClickAt(_DesktopTool[ClickAtArgs]):
    name = "click_at"
    description = "Click at screen coordinates. Only when click_element cannot find the control."
    risk = Risk.ACT
    Args = ClickAtArgs

    def risk_for(self, args: ClickAtArgs) -> Risk:
        # A coordinate click on a Send/Pay/Delete button is that action, exactly like click_element.
        return riskiest(self.click_risk_at(args.x, args.y), click_risk(args.expect) if args.expect else Risk.ACT)

    def summary(self, args: ClickAtArgs) -> str:
        names = self.labels_at(args.x, args.y)
        on = f" on '{names[-1]}'" if names else (f" on '{args.expect[:60]}'" if args.expect else "")
        return f"{'double-' if args.double else ''}{args.button}-click at ({args.x},{args.y}){on}"

    def input_targets(self, args: ClickAtArgs) -> list[Target] | None:
        t = target_at(args.x, args.y)
        return [t] if t and self.labels_at(args.x, args.y) is not None else None

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

    def risk_for(self, args: TypeArgs) -> Risk:
        # A newline is an Enter key press: it can submit a form or send a message, so it needs
        # the same approval as press_keys Enter (typing "hi\n" must not slip past it).
        if "\n" not in args.text and "\r" not in args.text:
            return Risk.ACT
        return Risk.EXTERNAL if messaging_app(_title_of(args.window)) else Risk.WRITE

    def summary(self, args: TypeArgs) -> str:
        enter = "\n" in args.text or "\r" in args.text
        if enter and (app := messaging_app(_title_of(args.window))):
            return f"SEND a message in {app}: \u201c{args.text.strip()}\u201d"
        return f"type {len(args.text)} chars{' and press Enter' if enter else ''}" + (
            f" into {args.window}" if args.window else "")

    def input_targets(self, args: TypeArgs) -> list[Target] | None:
        t = target_of(args.window)
        return [t] if t else None

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


def key_list(value: object) -> object:
    """Models often send keys as one string ("enter", "ctrl+s", "ctrl, s"): the same keys as a list.
    Rejecting it cost a free model several failed steps and a failed check in a Calculator task."""
    if isinstance(value, str):
        return [k.strip().lower() for k in value.replace("+", ",").replace(" ", ",").split(",") if k.strip()]
    return value


class KeysArgs(BaseModel):
    keys: list[str] = Field(description="Keys pressed together, e.g. ['ctrl','s'] or ['enter']")
    window: str | None = Field(default=None, description="Window that must receive the keys (part of title)")

    _keys = field_validator("keys", mode="before")(key_list)


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
            if messaging_app(_title_of(getattr(args, "window", None))):
                return Risk.EXTERNAL  # Enter in a chat app sends whatever is typed there
            return Risk.WRITE  # Enter can submit forms/messages: needs approval in ask-risky
        if {"alt", "f4"} <= k or {"ctrl", "w"} <= k:
            return Risk.WRITE  # closing things can lose unsaved work
        return Risk.ACT

    def summary(self, args: KeysArgs) -> str:
        if _presses_enter(args.keys) and (app := messaging_app(_title_of(args.window))):
            return f"press Enter in {app} (this SENDS whatever is typed in the chat)"
        return "press " + "+".join(args.keys)

    def input_targets(self, args: KeysArgs) -> list[Target] | None:
        t = target_of(args.window)
        return [t] if t else None

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

    def input_targets(self, args: SetFieldArgs) -> list[Target] | None:
        t = target_of(args.window)
        return [t] if t else None

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
    amount: int = Field(description="Wheel clicks: positive = up (or right), negative = down (or left)")
    x: int | None = Field(default=None, description="Scroll over this screen point (default: where the mouse is)")
    y: int | None = None
    horizontal: bool = Field(default=False, description="Scroll sideways instead of up/down")


class Scroll(_DesktopTool[ScrollArgs]):
    name = "scroll"
    description = ("Scroll a page or list. Give x,y of the area to scroll (e.g. the middle of a web page "
                   "or a side panel): otherwise whatever is under the mouse scrolls.")
    risk = Risk.ACT
    Args = ScrollArgs

    async def run(self, args: ScrollArgs) -> ToolResult:
        def go() -> str:
            _scroll(args.amount, args.x, args.y, args.horizontal)
            where = f" at ({args.x},{args.y})" if args.x is not None else ""
            return f"scrolled {args.amount}{' sideways' if args.horizontal else ''}{where}"

        return await self._act(go, _window_evidence)


def _scroll(amount: int, x: int | None, y: int | None, horizontal: bool) -> None:
    pyautogui = _pg()
    if x is not None and y is not None:
        pyautogui.moveTo(x, y)  # instant: the wheel goes to the window under the pointer
    if horizontal:
        pyautogui.hscroll(amount * 120)
    else:
        pyautogui.scroll(amount * 120)


# --- several mouse/keyboard steps in one go --------------------------------------------------

ACTION_KINDS = ("click", "double_click", "right_click", "middle_click", "move", "drag", "scroll", "type",
                "keys", "wait")
CLICKS = ("click", "double_click", "right_click", "middle_click")


class Step(BaseModel):
    action: str = Field(description=" | ".join(ACTION_KINDS))
    x: int | None = None
    y: int | None = None
    to_x: int | None = Field(default=None, description="drag: end point")
    to_y: int | None = None
    amount: int | None = Field(default=None, description="scroll: wheel clicks (+up / -down)")
    horizontal: bool = False
    text: str | None = Field(default=None, description="type: the text")
    keys: list[str] | None = Field(default=None, description="keys: e.g. ['ctrl','a']")
    seconds: float | None = Field(default=None, description="wait: 0.1-3 s, e.g. for a menu to open")

    _keys = field_validator("keys", mode="before")(key_list)


class ActionsArgs(BaseModel):
    steps: list[Step] = Field(min_length=1, max_length=15, description=(
        "Mouse/keyboard steps run back to back, e.g. [{action:'click',x:120,y:40}, "
        "{action:'wait',seconds:0.3}, {action:'click',x:150,y:210}]"))


class DoActions(_DesktopTool[ActionsArgs]):
    name = "do_actions"
    description = ("Run several mouse/keyboard steps in ONE call: click, double_click, right_click, "
                   "middle_click, move (hover), drag (x,y -> to_x,to_y), scroll (amount at x,y), type, keys, "
                   "wait. Much faster than one tool call per click: use it whenever you already know the "
                   "positions (from a screenshot), e.g. open a menu and pick an item, or scroll and click. "
                   "The screen is shown to you once, after the last step.")
    risk = Risk.ACT
    Args = ActionsArgs

    def risk_for(self, args: ActionsArgs) -> Risk:
        """The riskiest step decides: a batch can't slip an Enter or Delete past the approval rules."""
        order = [Risk.READ, Risk.ACT, Risk.WRITE, Risk.EXTERNAL, Risk.DESTRUCTIVE]
        worst = Risk.ACT
        if self._sends_in(args):
            worst = Risk.EXTERNAL
        for s in args.steps:
            if s.action == "type" and s.text and ("\n" in s.text or "\r" in s.text):
                r = Risk.WRITE
            elif s.action == "keys" and s.keys:
                r = PressKeys.risk_for(None, KeysArgs(keys=s.keys))  # type: ignore[arg-type]
            elif s.action in CLICKS and s.x is not None and s.y is not None:
                # Read before any step runs: a point inside a menu the batch opens is judged by what is
                # there now. The first click, and every click on something already on screen, is exact.
                r = self.click_risk_at(s.x, s.y)
            else:
                r = Risk.ACT
            worst = max(worst, r, key=order.index)
        return worst

    def input_targets(self, args: ActionsArgs) -> list[Target] | None:
        found: list[Target] = []
        focus: Target | None = None  # where keys go: the last window clicked, else the one in front
        for s in args.steps:
            if s.action in (*CLICKS, "drag") and s.x is not None and s.y is not None:
                t = focus = target_at(s.x, s.y)
                if s.action in CLICKS and self.labels_at(s.x, s.y) is None:
                    return None
            elif s.action in ("type", "keys"):
                t = focus or target_of(None)
            else:
                continue
            if t is None:
                return None
            found.append(t)
        return found

    @staticmethod
    def _sends_in(args: ActionsArgs) -> str | None:
        """The chat app this batch sends a message in (it presses Enter there), if any."""
        enter = any((s.action == "keys" and _presses_enter(s.keys)) or
                    (s.action == "type" and s.text and ("\n" in s.text or "\r" in s.text)) for s in args.steps)
        if not enter:
            return None
        point = next(((s.x, s.y) for s in args.steps if s.x is not None and s.y is not None), None)
        return messaging_app(_title_at(*point) if point else _title_of(None))

    def summary(self, args: ActionsArgs) -> str:
        if app := self._sends_in(args):
            typed = " ".join(s.text or "" for s in args.steps if s.action == "type").strip()
            return f"SEND a message in {app}: \u201c{typed}\u201d (by clicking and typing)"
        parts = []
        for s in args.steps:
            if s.action == "type":
                parts.append(f"type {len(s.text or '')} chars")
            elif s.action == "keys":
                parts.append("press " + "+".join(s.keys or []))
            elif s.action == "drag":
                parts.append(f"drag ({s.x},{s.y})->({s.to_x},{s.to_y})")
            elif s.action == "scroll":
                parts.append(f"scroll {s.amount}")
            elif s.action == "wait":
                parts.append(f"wait {s.seconds}s")
            else:
                parts.append(f"{s.action} ({s.x},{s.y})")
        return "; ".join(parts)

    async def run(self, args: ActionsArgs) -> ToolResult:
        for i, s in enumerate(args.steps):  # check everything before moving the mouse at all
            problem = _step_problem(s)
            if problem:
                return ToolResult(False, f"step {i + 1} ({s.action}): {problem}. Nothing was done.")
        done: list[str] = []
        last = len(args.steps) - 1
        for i, s in enumerate(args.steps):
            # Short pauses inside the batch, the full settle only before the screen is shown.
            r = await self._act(partial(_run_step, s), settle=0.4 if i == last else 0.12)
            if not r.ok:
                ran = "; ".join(done) or "nothing"
                return ToolResult(False, f"stopped at step {i + 1} ({s.action}): {r.output}. Done before: {ran}")
            done.append(r.output)
        ev = await self.ctl.call(_window_evidence)
        return ToolResult(True, "; ".join(done), {**ev, "steps_done": len(done)})


def _step_problem(s: Step) -> str:
    if s.action not in ACTION_KINDS:
        return f"unknown action, use one of {', '.join(ACTION_KINDS)}"
    if s.action in ("click", "double_click", "right_click", "middle_click", "move", "drag") and (
            s.x is None or s.y is None):
        return "needs x and y"
    if s.action == "drag" and (s.to_x is None or s.to_y is None):
        return "needs to_x and to_y"
    if s.action == "scroll" and not s.amount:
        return "needs amount"
    if s.action == "type" and not s.text:
        return "needs text"
    if s.action == "keys" and not s.keys:
        return "needs keys"
    return ""


def _run_step(s: Step) -> str:
    pyautogui = _pg()
    if s.action in ("click", "double_click", "right_click", "middle_click"):
        button = {"right_click": "right", "middle_click": "middle"}.get(s.action, "left")
        pyautogui.click(s.x, s.y, clicks=2 if s.action == "double_click" else 1, button=button)
        return f"{s.action} ({s.x},{s.y})"
    if s.action == "move":
        pyautogui.moveTo(s.x, s.y)  # instant
        return f"moved to ({s.x},{s.y})"
    if s.action == "drag":
        pyautogui.moveTo(s.x, s.y)
        # A short glide: many apps ignore a drag that jumps to the end point instantly.
        pyautogui.dragTo(s.to_x, s.to_y, duration=0.25, button="left")
        return f"dragged ({s.x},{s.y})->({s.to_x},{s.to_y})"
    if s.action == "scroll":
        _scroll(s.amount or 0, s.x, s.y, s.horizontal)
        return f"scrolled {s.amount}"
    if s.action == "type":
        if _focused_is_password():
            raise LookupError("REFUSED: the focused field is a password field. Ask the user to type it.")
        _send_unicode(s.text or "")
        return f"typed {len(s.text or '')} chars"
    if s.action == "keys":
        pyautogui.hotkey(*[k.lower() for k in s.keys or []])
        return "pressed " + "+".join(s.keys or [])
    time.sleep(min(max(s.seconds or 0.3, 0.1), 3.0))
    return f"waited {s.seconds}s"


def desktop_tools(ctl: DesktopController) -> list[Tool]:
    return [ListWindows(ctl), ReadWindow(ctl), FocusWindow(ctl), ClickElement(ctl), ClickAt(ctl),
            TypeText(ctl), PressKeys(ctl), SetField(ctl), CloseWindow(ctl), Scroll(ctl), DoActions(ctl)]
