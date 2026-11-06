"""Stop means stop: the switch every input action checks, and letting go of anything Delta holds down.

When the user stops a task (Stop button, /stop, Esc twice, Ctrl+Alt+Shift+S) the asyncio task is cancelled,
but an input action already running on the UI Automation thread (typing a long text, a shortcut) would
carry on to its end. So:

- `request()` flips a process-wide switch. Delta's own input loops (`check()` between characters) and every
  key-down / mouse-down pyautogui sends (`track_pyautogui`) raise `StopRequestedError` once it is set. A key-up
  or mouse-up is never blocked: letting go is always allowed.
- `release_held()` sends the key-up / mouse-up for every key or button Delta pressed and has not released,
  so no Ctrl, Shift, Alt or mouse button stays stuck down after a stop, an error or a crash in the middle of
  a shortcut. Keys the user holds are not touched: only what Delta itself pressed is tracked.
- `reset()` clears the switch when the next task starts.

`DoubleEsc` is the "Esc twice within one second" detector the overlay's keyboard hook feeds.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from typing import Any

from delta.system.logging import log

_stop = threading.Event()
_lock = threading.Lock()
_held_keys: list[str] = []                  # pyautogui key names Delta pressed and still holds, in order
_held_buttons: dict[str, tuple[Any, Any]] = {}  # button -> (x, y) where Delta pressed it
_originals: dict[str, Callable[..., Any]] = {}


class StopRequestedError(RuntimeError):
    """The user stopped the task: the input action ends here."""

    def __init__(self) -> None:
        super().__init__("STOPPED: you stopped this task, so I let go of the mouse and keyboard.")


def request() -> None:
    _stop.set()


def reset() -> None:
    _stop.clear()


def requested() -> bool:
    return _stop.is_set()


def check() -> None:
    """Call between input events of a long action (each typed character)."""
    if _stop.is_set():
        raise StopRequestedError


def held() -> tuple[list[str], list[str]]:
    with _lock:
        return list(_held_keys), list(_held_buttons)


def track_pyautogui(pyautogui: Any) -> None:
    """Wrap pyautogui's Windows key/mouse calls (once) so Delta knows what it holds down, and so a stop request
    ends a shortcut, drag or click before its next press. pyautogui's public functions (hotkey, press, click,
    mouseDown...) all go through these platform functions."""
    platform = getattr(pyautogui, "platformModule", None)
    if platform is None or getattr(platform, "_delta_tracked", False):
        return
    for name in ("_keyDown", "_keyUp", "_mouseDown", "_mouseUp"):
        if not callable(getattr(platform, name, None)):
            return  # an unexpected pyautogui: leave it exactly as it is
    _originals.update({n: getattr(platform, n) for n in ("_keyDown", "_keyUp", "_mouseDown", "_mouseUp")})

    def key_down(key: str) -> Any:
        check()
        out = _originals["_keyDown"](key)
        with _lock:
            _held_keys.append(key)
        return out

    def key_up(key: str) -> Any:
        try:
            return _originals["_keyUp"](key)
        finally:
            with _lock, contextlib.suppress(ValueError):
                _held_keys.remove(key)

    def mouse_down(x: Any, y: Any, button: str) -> Any:
        check()
        out = _originals["_mouseDown"](x, y, button)
        with _lock:
            _held_buttons[button] = (x, y)
        return out

    def mouse_up(x: Any, y: Any, button: str) -> Any:
        try:
            return _originals["_mouseUp"](x, y, button)
        finally:
            with _lock:
                _held_buttons.pop(button, None)

    platform._keyDown, platform._keyUp = key_down, key_up
    platform._mouseDown, platform._mouseUp = mouse_down, mouse_up
    for name in CHECKED:  # one-shot actions (a click is its own down and up): just refused after a stop
        fn = getattr(platform, name, None)
        if callable(fn):
            setattr(platform, name, _checked(fn))
    platform._delta_tracked = True


CHECKED = ("_click", "_multiClick", "_moveTo", "_scroll", "_vscroll", "_hscroll")


def _checked(fn: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        check()
        return fn(*args, **kwargs)

    return wrapper


def release_held() -> list[str]:
    """Let go of every key and mouse button Delta pressed and still holds (last pressed first). Returns what
    was released, for the log. Safe to call from any thread, any number of times."""
    with _lock:
        keys, buttons = list(reversed(_held_keys)), dict(_held_buttons)
        _held_keys.clear()
        _held_buttons.clear()
    released: list[str] = []
    for key in keys:
        if up := _originals.get("_keyUp"):
            try:
                up(key)
                released.append(key)
            except Exception as e:
                log.warning("stop.release_key_failed", key=key, error=str(e))
    for button, (x, y) in buttons.items():
        if up := _originals.get("_mouseUp"):
            try:
                up(x, y, button)
                released.append(f"mouse {button}")
            except Exception as e:
                log.warning("stop.release_button_failed", button=button, error=str(e))
    if released:
        log.info("stop.released_input", released=released)
    return released


class DoubleEsc:
    """True on the second physical Esc press within `window_s` of the first. Key repeat (holding Esc down)
    does not count: only a press after a release does."""

    def __init__(self, window_s: float = 1.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.window_s = window_s
        self.clock = clock
        self._last = -1e9
        self._down = False

    def down(self) -> bool:
        if self._down:  # auto-repeat while held
            return False
        self._down = True
        now = self.clock()
        if now - self._last <= self.window_s:
            self._last = -1e9
            return True
        self._last = now
        return False

    def up(self) -> None:
        self._down = False

    def clear(self) -> None:
        self._last, self._down = -1e9, False
