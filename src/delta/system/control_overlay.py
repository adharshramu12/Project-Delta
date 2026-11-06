"""The "Delta is in control" signal on screen, and the stop hotkeys.

While Delta drives the mouse, the keyboard or a browser, people must be able to tell at a glance. This
draws, on its own thread with plain Win32 calls (no extra packages):

- an orange ring that follows the mouse pointer (the real Windows cursor is never changed),
- a thin orange border around each screen Delta works on,
- a small label at the top of that screen: "Delta is working. Press Esc twice or say stop".

Every window is always on top, click-through and never takes focus (WS_EX_LAYERED | WS_EX_TRANSPARENT |
WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST), and is kept out of screenshots and screen recording
with SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) on Windows 10 2004 and later. Where Windows refuses
that, Delta hides the windows for the moment it takes its own screenshots (`hidden_for_capture`).

It disappears at once on task end, stop, error or crash:
- the controller (delta.system.agent_cursor) hides it when the action or task ends or is stopped;
- a heartbeat: the controller's event loop beats every second while it is shown; if Delta hangs and the
  beats stop for HEARTBEAT_TIMEOUT_S, the overlay hides itself;
- the overlay is a daemon thread of Delta's process: if Delta dies, Windows destroys its windows with it.

High contrast: when Windows high contrast is on, the system highlight colours are used instead of orange and
the lines are thicker. DPI: the thread is per-monitor DPI aware (v2) and sizes follow each monitor's scale.

Stop hotkeys (both only ever *stop* things; nothing is swallowed from other apps except the combination):
- Ctrl+Alt+Shift+S, registered with RegisterHotKey while Delta runs (if another app owns it, it is skipped).
- Esc pressed twice within one second, only while the overlay is shown (Delta is driving): a low-level
  keyboard hook watches for Esc only, ignores keys Delta itself sends (LLKHF_INJECTED) and passes every key
  on unchanged. The hook is removed the moment Delta stops driving.
"""

from __future__ import annotations

import contextlib
import ctypes
import queue
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from delta.system.input_stop import DoubleEsc
from delta.system.logging import log

ORANGE = (249, 115, 22)        # #F97316
LABEL_TEXT = "Delta is working. Press Esc twice or say stop"
HEARTBEAT_TIMEOUT_S = 5.0
TICK_MS = 16                   # pointer follow rate (about 60 per second)
TOPMOST_EVERY_TICKS = 60       # re-assert "always on top" about once a second
CLASS_NAME = "DeltaControlOverlay"

# Win32 constants
WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x00000008, 0x00000020, 0x00000080
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x00080000, 0x08000000
OVERLAY_EX_STYLE = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST
LWA_COLORKEY, LWA_ALPHA = 0x1, 0x2
WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40
HWND_TOPMOST = -1
WM_DESTROY, WM_PAINT, WM_ERASEBKGND, WM_TIMER, WM_HOTKEY, WM_CLOSE = 0x2, 0xF, 0x14, 0x113, 0x312, 0x10
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_APP = 0x8000
WM_RUN, WM_STOPKEY = WM_APP + 1, WM_APP + 2
WH_KEYBOARD_LL, HC_ACTION, LLKHF_INJECTED = 13, 0, 0x10
VK_ESCAPE, VK_S = 0x1B, 0x53
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
HOTKEY_ID = 0xD17A
MONITOR_DEFAULTTONEAREST = 2
SPI_GETHIGHCONTRAST, HCF_HIGHCONTRASTON = 0x0042, 0x1
COLOR_HIGHLIGHT, COLOR_HIGHLIGHTTEXT = 13, 14
DT_CENTER, DT_VCENTER, DT_SINGLELINE, DT_NOPREFIX = 0x1, 0x4, 0x20, 0x800
TRANSPARENT_BK = 1
NULL_BRUSH = 5
KEY_COLOR = 0x00FF00FF         # magenta: transparent inside the ring window
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
TIMER_ID = 1


# --- geometry and colours (pure: tested on every platform) ----------------------------------------------------

def scale_for(dpi: int) -> float:
    return max(1.0, (dpi or 96) / 96)


@dataclass(frozen=True)
class Style:
    ring: tuple[int, int, int]
    text: tuple[int, int, int]
    high_contrast: bool


def style_for(high_contrast: bool, highlight: tuple[int, int, int] | None = None,
              highlight_text: tuple[int, int, int] | None = None) -> Style:
    """Orange normally; Windows' own highlight colours in high contrast mode."""
    if high_contrast and highlight is not None:
        return Style(highlight, highlight_text or (255, 255, 255), True)
    return Style(ORANGE, (255, 255, 255), high_contrast)


def ring_box(x: int, y: int, dpi: int, high_contrast: bool = False) -> tuple[int, int, int, int]:
    """(left, top, size, pen width) of the ring window centred on the pointer."""
    s = scale_for(dpi)
    size = round(44 * s)
    pen = max(2, round((6 if high_contrast else 4) * s))
    return x - size // 2, y - size // 2, size, pen


def border_strips(rect: tuple[int, int, int, int], dpi: int,
                  high_contrast: bool = False) -> list[tuple[int, int, int, int]]:
    """The four strips (left, top, width, height) of a thin border just inside a monitor's rectangle."""
    left, top, right, bottom = rect
    t = max(2, round((5 if high_contrast else 3) * scale_for(dpi)))
    w, h = right - left, bottom - top
    return [(left, top, w, t), (left, bottom - t, w, t), (left, top, t, h), (right - t, top, t, h)]


def label_box(rect: tuple[int, int, int, int], dpi: int) -> tuple[int, int, int, int]:
    """(left, top, width, height) of the label: centred near the top of the monitor, under the border."""
    s = scale_for(dpi)
    w, h = round(420 * s), round(30 * s)
    left, top, right, _ = rect
    return left + (right - left - w) // 2, top + round(10 * s), w, h


def colorref(rgb: tuple[int, int, int]) -> int:
    r, g, b = rgb
    return r | (g << 8) | (b << 16)


def rgb_of(ref: int) -> tuple[int, int, int]:
    return ref & 0xFF, (ref >> 8) & 0xFF, (ref >> 16) & 0xFF


# --- the overlay ---------------------------------------------------------------------------------------------

class NullOverlay:
    """Where there is no Windows desktop: every call does nothing."""

    started = False
    shown = False
    capture_excluded = True

    def start(self) -> bool:
        return False

    def show(self, label: bool = True) -> None:
        pass

    def hide(self) -> None:
        pass

    def beat(self) -> None:
        pass

    def close(self) -> None:
        pass

    @contextlib.contextmanager
    def hidden_for_capture(self) -> Iterator[None]:
        yield


def make_overlay(on_stop: Callable[[str], None] | None = None) -> Any:
    return Win32Overlay(on_stop) if sys.platform == "win32" else NullOverlay()


class Win32Overlay:
    """All windows live on one daemon thread with its own message loop. Other threads only post requests."""

    def __init__(self, on_stop: Callable[[str], None] | None = None,
                 heartbeat_timeout_s: float = HEARTBEAT_TIMEOUT_S) -> None:
        self.on_stop = on_stop
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.started = False
        self.shown = False
        self.capture_excluded = True   # False once Windows refused WDA_EXCLUDEFROMCAPTURE for any window
        self.hotkey_registered = False
        self.failed = ""
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._jobs: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._last_beat = 0.0
        self._label = True
        self._hwnd = 0                  # the ring window; also receives timers, hotkeys and requests
        self._label_hwnd = 0
        self._strips: dict[int, list[int]] = {}   # monitor handle -> its four border windows
        self._touched: list[int] = []             # monitors Delta has worked on while shown
        self._hook = None
        self._hook_proc: Any = None
        self._wndproc: Any = None
        self._esc = DoubleEsc()
        self._style = style_for(False)
        self._ring_size = 0
        self._ticks = 0
        self._capture_depth = 0
        self._w: Any = None
        self._class = ""

    # --- public, any thread ----------------------------------------------------------------------------------
    def start(self, timeout: float = 3.0) -> bool:
        if self.started:
            return not self.failed
        self.started = True
        self._thread = threading.Thread(target=self._main, name="delta-overlay", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return bool(self._hwnd) and not self.failed

    def show(self, label: bool = True) -> None:
        self._last_beat = time.monotonic()
        self._label = label
        self._post(self._do_show)

    def hide(self) -> None:
        self._post(self._do_hide)

    def beat(self) -> None:
        self._last_beat = time.monotonic()

    def close(self) -> None:
        if self._hwnd:
            self._post(self._do_close)
        if self._thread is not None:
            self._thread.join(2)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @contextlib.contextmanager
    def hidden_for_capture(self) -> Iterator[None]:
        """Delta's own screenshot: where Windows could not keep these windows out of captures, hide them for
        the moment of the capture."""
        if self.capture_excluded or not self.shown or not self.alive:
            yield
            return
        done = threading.Event()

        def hide_now() -> None:
            self._capture(True)
            done.set()

        self._post(hide_now)
        done.wait(0.5)
        time.sleep(0.05)  # one frame for the screen to recompose without them
        try:
            yield
        finally:
            self._post(lambda: self._capture(False))

    # --- the overlay thread ----------------------------------------------------------------------------------
    def _post(self, job: Callable[[], None]) -> None:
        if not self._hwnd:
            return
        self._jobs.put(job)
        self._w.user32.PostMessageW(self._hwnd, WM_RUN, 0, 0)

    def _main(self) -> None:
        try:
            self._w = _Win32()
            w = self._w
            with contextlib.suppress(Exception):  # Windows 10 1607+; older: system DPI, still works
                w.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
            self._wndproc = w.WNDPROC(self._proc)
            self._register_class()
            self._hwnd = self._create(LWA_COLORKEY)
            self._label_hwnd = self._create(LWA_ALPHA, alpha=235)
            self.hotkey_registered = bool(w.user32.RegisterHotKey(
                self._hwnd, HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, VK_S))
            if not self.hotkey_registered:
                log.info("overlay.hotkey_unavailable", hotkey="Ctrl+Alt+Shift+S")  # another app owns it
        except Exception as e:
            self.failed = f"{type(e).__name__}: {e}"
            log.warning("overlay.start_failed", error=self.failed)
            self._ready.set()
            return
        self._ready.set()
        msg = w.MSG()
        while w.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            w.user32.TranslateMessage(ctypes.byref(msg))
            w.user32.DispatchMessageW(ctypes.byref(msg))
        self._unhook()
        log.info("overlay.stopped")

    def _register_class(self) -> None:
        w = self._w
        wc = w.WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(w.WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = w.kernel32.GetModuleHandleW(None)
        wc.lpszClassName = f"{CLASS_NAME}{id(self)}"
        if not w.user32.RegisterClassExW(ctypes.byref(wc)):
            raise OSError(f"RegisterClassExW failed ({ctypes.get_last_error()})")
        self._class = wc.lpszClassName

    def _create(self, mode: int, alpha: int = 255) -> int:
        w = self._w
        hwnd = w.user32.CreateWindowExW(OVERLAY_EX_STYLE, self._class, "", WS_POPUP, 0, 0, 1, 1,
                                        None, None, w.kernel32.GetModuleHandleW(None), None)
        if not hwnd:
            raise OSError(f"CreateWindowExW failed ({ctypes.get_last_error()})")
        if mode == LWA_COLORKEY:
            w.user32.SetLayeredWindowAttributes(hwnd, KEY_COLOR, 0, LWA_COLORKEY)
        else:
            w.user32.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA)
        if not w.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
            if self.capture_excluded:
                log.info("overlay.capture_exclusion_unavailable", error=ctypes.get_last_error())
            self.capture_excluded = False  # older Windows: hidden during Delta's own captures instead
        return int(hwnd)

    def _proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        w = self._w
        try:
            if msg == WM_RUN:
                while True:
                    try:
                        job = self._jobs.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        job()
                    except Exception as e:
                        log.warning("overlay.job_failed", error=str(e))
                return 0
            if msg == WM_TIMER and wparam == TIMER_ID:
                self._tick()
                return 0
            if msg == WM_HOTKEY and wparam == HOTKEY_ID:
                self._fire("hotkey")
                return 0
            if msg == WM_STOPKEY:
                self._fire("esc")
                return 0
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_PAINT:
                self._paint(hwnd)
                return 0
            if msg == WM_DESTROY and hwnd == self._hwnd:
                w.user32.PostQuitMessage(0)
                return 0
        except Exception as e:  # a drawing problem must never take Delta down
            log.warning("overlay.proc_failed", msg=msg, error=str(e))
        return int(w.user32.DefWindowProcW(hwnd, msg, wparam, lparam))

    def _fire(self, kind: str) -> None:
        log.info("overlay.stop_key", key=kind)
        if self.on_stop is not None:
            try:
                self.on_stop(kind)
            except Exception as e:
                log.warning("overlay.on_stop_failed", error=str(e))

    # --- showing and hiding ----------------------------------------------------------------------------------
    def _read_style(self) -> Style:
        w = self._w
        hc = w.HIGHCONTRASTW()
        hc.cbSize = ctypes.sizeof(w.HIGHCONTRASTW)
        on = bool(w.user32.SystemParametersInfoW(SPI_GETHIGHCONTRAST, hc.cbSize, ctypes.byref(hc), 0)
                  and hc.dwFlags & HCF_HIGHCONTRASTON)
        if not on:
            return style_for(False)
        return style_for(True, rgb_of(w.user32.GetSysColor(COLOR_HIGHLIGHT)),
                         rgb_of(w.user32.GetSysColor(COLOR_HIGHLIGHTTEXT)))

    def _do_show(self) -> None:
        self._style = self._read_style()
        self.shown = True
        self._ticks = 0
        self._esc.clear()
        self._hook_keys()
        self._w.user32.SetTimer(self._hwnd, TIMER_ID, TICK_MS, None)
        self._tick()

    def _do_hide(self) -> None:
        w = self._w
        self.shown = False
        w.user32.KillTimer(self._hwnd, TIMER_ID)
        self._unhook()
        for hwnd in self._all_windows():
            w.user32.ShowWindow(hwnd, SW_HIDE)
        self._touched.clear()

    def _do_close(self) -> None:
        w = self._w
        self._do_hide()
        if self.hotkey_registered:
            w.user32.UnregisterHotKey(self._hwnd, HOTKEY_ID)
            self.hotkey_registered = False
        for hwnd in self._all_windows():
            if hwnd != self._hwnd:
                w.user32.DestroyWindow(hwnd)
        w.user32.DestroyWindow(self._hwnd)

    def _capture(self, hiding: bool) -> None:
        self._capture_depth += 1 if hiding else -1
        if hiding and self._capture_depth == 1:
            for hwnd in self._all_windows():
                self._w.user32.ShowWindow(hwnd, SW_HIDE)
        elif not hiding and self._capture_depth <= 0:
            self._capture_depth = 0
            if self.shown:
                self._ticks = 0
                self._tick(force=True)

    def _all_windows(self) -> list[int]:
        out = [self._hwnd, self._label_hwnd]
        for strips in self._strips.values():
            out += strips
        return [h for h in out if h]

    def _tick(self, force: bool = False) -> None:
        w = self._w
        if not self.shown:
            return
        if time.monotonic() - self._last_beat > self.heartbeat_timeout_s:
            log.warning("overlay.heartbeat_lost", seconds=self.heartbeat_timeout_s)
            self._do_hide()
            return
        if self._capture_depth:
            return
        pt = w.POINT()
        if not w.user32.GetCursorPos(ctypes.byref(pt)):
            return  # no interactive desktop (locked screen, service session): nothing to follow
        mon = w.user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
        dpi = self._dpi(mon)
        topmost = force or self._ticks % TOPMOST_EVERY_TICKS == 0
        self._ticks += 1
        left, top, size, _pen = ring_box(pt.x, pt.y, dpi, self._style.high_contrast)
        flags = SWP_NOACTIVATE | SWP_SHOWWINDOW
        if size == self._ring_size and not topmost:
            flags |= SWP_NOSIZE
        w.user32.SetWindowPos(self._hwnd, ctypes.c_void_p(HWND_TOPMOST), left, top, size, size, flags)
        if size != self._ring_size or topmost:
            self._ring_size = size
            w.user32.InvalidateRect(self._hwnd, None, True)
        if mon and (mon not in self._touched or topmost):
            if mon not in self._touched:
                self._touched.append(mon)
            self._place_border(mon, dpi)
            if self._label:
                rect = self._monitor_rect(mon)
                lx, ly, lw, lh = label_box(rect, dpi)
                w.user32.SetWindowPos(self._label_hwnd, ctypes.c_void_p(HWND_TOPMOST), lx, ly, lw, lh,
                                      SWP_NOACTIVATE | SWP_SHOWWINDOW)
                w.user32.InvalidateRect(self._label_hwnd, None, True)
            for other in self._touched:
                if other != mon and topmost:
                    self._place_border(other, self._dpi(other))

    def _place_border(self, mon: int, dpi: int) -> None:
        w = self._w
        strips = self._strips.get(mon)
        if strips is None:
            strips = self._strips[mon] = [self._create(LWA_ALPHA, alpha=230) for _ in range(4)]
        for hwnd, (x, y, cx, cy) in zip(strips, border_strips(self._monitor_rect(mon), dpi,
                                                               self._style.high_contrast), strict=True):
            w.user32.SetWindowPos(hwnd, ctypes.c_void_p(HWND_TOPMOST), x, y, cx, cy, SWP_NOACTIVATE | SWP_SHOWWINDOW)
            w.user32.InvalidateRect(hwnd, None, True)

    def _monitor_rect(self, mon: int) -> tuple[int, int, int, int]:
        w = self._w
        info = w.MONITORINFO()
        info.cbSize = ctypes.sizeof(w.MONITORINFO)
        if not w.user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            return 0, 0, 0, 0
        r = info.rcMonitor
        return r.left, r.top, r.right, r.bottom

    def _dpi(self, mon: int) -> int:
        w = self._w
        if w.shcore is not None and mon:
            x, y = ctypes.c_uint(), ctypes.c_uint()
            with contextlib.suppress(Exception):
                if w.shcore.GetDpiForMonitor(mon, 0, ctypes.byref(x), ctypes.byref(y)) == 0 and x.value:
                    return int(x.value)
        return 96

    def _paint(self, hwnd: int) -> None:
        w = self._w
        ps = w.PAINTSTRUCT()
        hdc = w.user32.BeginPaint(hwnd, ctypes.byref(ps))
        try:
            rect = w.RECT()
            w.user32.GetClientRect(hwnd, ctypes.byref(rect))
            color = colorref(self._style.ring)
            if hwnd == self._hwnd:
                self._fill(hdc, rect, KEY_COLOR)
                _, _, _, pen_w = ring_box(0, 0, self._dpi(w.user32.MonitorFromWindow(hwnd, 2)),
                                             self._style.high_contrast)
                pen = w.gdi32.CreatePen(0, pen_w, color)
                old_pen = w.gdi32.SelectObject(hdc, pen)
                old_brush = w.gdi32.SelectObject(hdc, w.gdi32.GetStockObject(NULL_BRUSH))
                half = pen_w // 2 + 1
                w.gdi32.Ellipse(hdc, half, half, rect.right - half, rect.bottom - half)
                w.gdi32.SelectObject(hdc, old_brush)
                w.gdi32.SelectObject(hdc, old_pen)
                w.gdi32.DeleteObject(pen)
            elif hwnd == self._label_hwnd:
                self._fill(hdc, rect, color)
                height = -max(12, round((rect.bottom - rect.top) * 0.5))
                font = w.gdi32.CreateFontW(height, 0, 0, 0, 600, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")
                old_font = w.gdi32.SelectObject(hdc, font)
                w.gdi32.SetBkMode(hdc, TRANSPARENT_BK)
                w.gdi32.SetTextColor(hdc, colorref(self._style.text))
                w.user32.DrawTextW(hdc, LABEL_TEXT, -1, ctypes.byref(rect),
                                   DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX)
                w.gdi32.SelectObject(hdc, old_font)
                w.gdi32.DeleteObject(font)
            else:
                self._fill(hdc, rect, color)
        finally:
            w.user32.EndPaint(hwnd, ctypes.byref(ps))

    def _fill(self, hdc: Any, rect: Any, ref: int) -> None:
        w = self._w
        brush = w.gdi32.CreateSolidBrush(ref)
        w.user32.FillRect(hdc, ctypes.byref(rect), brush)
        w.gdi32.DeleteObject(brush)

    # --- Esc twice (only while shown) ------------------------------------------------------------------------
    def _hook_keys(self) -> None:
        w = self._w
        if self._hook:
            return
        if self._hook_proc is None:
            self._hook_proc = w.HOOKPROC(self._keyboard)
        self._hook = w.user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_proc, w.kernel32.GetModuleHandleW(None), 0)
        if not self._hook:
            log.info("overlay.esc_hook_unavailable", error=ctypes.get_last_error())

    def _unhook(self) -> None:
        if self._hook:
            self._w.user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        self._esc.clear()

    def _keyboard(self, code: int, wparam: int, lparam: int) -> int:
        w = self._w
        try:
            if code == HC_ACTION:
                kb = ctypes.cast(lparam, ctypes.POINTER(w.KBDLLHOOKSTRUCT)).contents
                if kb.vkCode == VK_ESCAPE and not kb.flags & LLKHF_INJECTED:  # the person, not Delta's own keys
                    if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        if self._esc.down():
                            w.user32.PostMessageW(self._hwnd, WM_STOPKEY, 0, 0)
                    elif wparam in (WM_KEYUP, WM_SYSKEYUP):
                        self._esc.up()
        except Exception:  # never let a hook problem eat a key
            pass
        return int(w.user32.CallNextHookEx(None, code, wparam, lparam))


class _Win32:
    """Win32 functions with their exact signatures, on private DLL handles so the argtypes set here never
    change how the rest of Delta calls the same functions."""

    def __init__(self) -> None:
        from ctypes import wintypes as W

        LRESULT = ctypes.c_ssize_t
        self.WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
        self.HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, W.WPARAM, W.LPARAM)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", W.UINT), ("style", W.UINT), ("lpfnWndProc", self.WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE),
                        ("hIcon", W.HANDLE), ("hCursor", W.HANDLE), ("hbrBackground", W.HANDLE),
                        ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR), ("hIconSm", W.HANDLE)]

        class PAINTSTRUCT(ctypes.Structure):
            _fields_ = [("hdc", W.HDC), ("fErase", W.BOOL), ("rcPaint", W.RECT), ("fRestore", W.BOOL),
                        ("fIncUpdate", W.BOOL), ("rgbReserved", ctypes.c_byte * 32)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", W.DWORD)]

        class HIGHCONTRASTW(ctypes.Structure):
            _fields_ = [("cbSize", W.UINT), ("dwFlags", W.DWORD), ("lpszDefaultScheme", W.LPWSTR)]

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", W.DWORD), ("scanCode", W.DWORD), ("flags", W.DWORD), ("time", W.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        self.WNDCLASSEXW, self.PAINTSTRUCT, self.MONITORINFO = WNDCLASSEXW, PAINTSTRUCT, MONITORINFO
        self.HIGHCONTRASTW, self.KBDLLHOOKSTRUCT = HIGHCONTRASTW, KBDLLHOOKSTRUCT
        self.MSG, self.RECT, self.POINT = W.MSG, W.RECT, W.POINT

        u = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        g = self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        k = self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            self.shcore: Any = ctypes.WinDLL("shcore")
            self.shcore.GetDpiForMonitor.argtypes = [W.HANDLE, ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
                                                     ctypes.POINTER(ctypes.c_uint)]
            self.shcore.GetDpiForMonitor.restype = ctypes.c_long
        except OSError:
            self.shcore = None

        def sig(fn: Any, res: Any, *args: Any) -> None:
            fn.restype, fn.argtypes = res, list(args)

        sig(k.GetModuleHandleW, W.HMODULE, W.LPCWSTR)
        sig(u.RegisterClassExW, W.ATOM, ctypes.POINTER(WNDCLASSEXW))
        sig(u.CreateWindowExW, W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID)
        sig(u.DefWindowProcW, LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
        sig(u.DestroyWindow, W.BOOL, W.HWND)
        sig(u.PostQuitMessage, None, ctypes.c_int)
        sig(u.PostMessageW, W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
        sig(u.GetMessageW, W.BOOL, ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT)
        sig(u.TranslateMessage, W.BOOL, ctypes.POINTER(W.MSG))
        sig(u.DispatchMessageW, LRESULT, ctypes.POINTER(W.MSG))
        sig(u.SetLayeredWindowAttributes, W.BOOL, W.HWND, W.DWORD, W.BYTE, W.DWORD)
        sig(u.SetWindowDisplayAffinity, W.BOOL, W.HWND, W.DWORD)
        sig(u.GetWindowDisplayAffinity, W.BOOL, W.HWND, ctypes.POINTER(W.DWORD))
        sig(u.SetWindowPos, W.BOOL, W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.UINT)
        sig(u.ShowWindow, W.BOOL, W.HWND, ctypes.c_int)
        sig(u.IsWindowVisible, W.BOOL, W.HWND)
        sig(u.InvalidateRect, W.BOOL, W.HWND, ctypes.c_void_p, W.BOOL)
        sig(u.SetTimer, ctypes.c_size_t, W.HWND, ctypes.c_size_t, W.UINT, ctypes.c_void_p)
        sig(u.KillTimer, W.BOOL, W.HWND, ctypes.c_size_t)
        sig(u.GetCursorPos, W.BOOL, ctypes.POINTER(W.POINT))
        sig(u.MonitorFromPoint, W.HANDLE, W.POINT, W.DWORD)
        sig(u.MonitorFromWindow, W.HANDLE, W.HWND, W.DWORD)
        sig(u.GetMonitorInfoW, W.BOOL, W.HANDLE, ctypes.POINTER(MONITORINFO))
        sig(u.RegisterHotKey, W.BOOL, W.HWND, ctypes.c_int, W.UINT, W.UINT)
        sig(u.UnregisterHotKey, W.BOOL, W.HWND, ctypes.c_int)
        sig(u.SetWindowsHookExW, W.HANDLE, ctypes.c_int, self.HOOKPROC, W.HINSTANCE, W.DWORD)
        sig(u.CallNextHookEx, LRESULT, W.HANDLE, ctypes.c_int, W.WPARAM, W.LPARAM)
        sig(u.UnhookWindowsHookEx, W.BOOL, W.HANDLE)
        sig(u.BeginPaint, W.HDC, W.HWND, ctypes.POINTER(PAINTSTRUCT))
        sig(u.EndPaint, W.BOOL, W.HWND, ctypes.POINTER(PAINTSTRUCT))
        sig(u.GetClientRect, W.BOOL, W.HWND, ctypes.POINTER(W.RECT))
        sig(u.FillRect, ctypes.c_int, W.HDC, ctypes.POINTER(W.RECT), W.HANDLE)
        sig(u.DrawTextW, ctypes.c_int, W.HDC, W.LPCWSTR, ctypes.c_int, ctypes.POINTER(W.RECT), W.UINT)
        sig(u.SystemParametersInfoW, W.BOOL, W.UINT, W.UINT, ctypes.c_void_p, W.UINT)
        sig(u.GetSysColor, W.DWORD, ctypes.c_int)
        sig(u.GetWindowLongW, W.LONG, W.HWND, ctypes.c_int)
        with contextlib.suppress(AttributeError):  # Windows 10 1607+
            sig(u.SetThreadDpiAwarenessContext, ctypes.c_void_p, ctypes.c_void_p)
        sig(g.CreatePen, W.HANDLE, ctypes.c_int, ctypes.c_int, W.DWORD)
        sig(g.CreateSolidBrush, W.HANDLE, W.DWORD)
        sig(g.SelectObject, W.HANDLE, W.HDC, W.HANDLE)
        sig(g.DeleteObject, W.BOOL, W.HANDLE)
        sig(g.GetStockObject, W.HANDLE, ctypes.c_int)
        sig(g.Ellipse, W.BOOL, W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
        sig(g.SetBkMode, ctypes.c_int, W.HDC, ctypes.c_int)
        sig(g.SetTextColor, W.DWORD, W.HDC, W.DWORD)
        sig(g.CreateFontW, W.HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.LPCWSTR)
