"""The "Delta is in control" overlay and the stop path on a real Windows desktop (CI job e2e-windows).

Checks the real Win32 windows: click-through, never focused, always on top, kept out of screenshots (or hidden
for Delta's own), gone on hide / heartbeat loss / close, the stop hotkey messages reach the callback, and a key
Delta holds down is really released by a stop. Run with DELTA_E2E=1 pytest -m e2e tests/e2e."""

from __future__ import annotations

import ctypes
import os
import sys
import time

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DELTA_E2E") != "1", reason="e2e: set DELTA_E2E=1"),
    pytest.mark.skipif(sys.platform != "win32", reason="Windows only"),
]

GWL_EXSTYLE = -20


def wait(cond, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def visible(hwnd: int) -> bool:
    return bool(ctypes.windll.user32.IsWindowVisible(hwnd))


@pytest.fixture
def overlay():
    from delta.system.control_overlay import Win32Overlay

    hits: list[str] = []
    o = Win32Overlay(on_stop=hits.append)
    assert o.start(), o.failed
    o.hits = hits  # type: ignore[attr-defined]
    yield o
    o.close()


def report(capsys, line: str) -> None:
    """Facts about this Windows, in the job log even when the test passes."""
    with capsys.disabled():
        print(f"\n[overlay] {line}")


def test_windows_are_click_through_never_focused_on_top_and_out_of_captures(overlay, capsys):
    from delta.system import control_overlay as co

    ctypes.windll.user32.SetCursorPos(300, 300)
    overlay.show()
    assert wait(lambda: visible(overlay._hwnd)), "ring not shown"
    for hwnd in overlay._all_windows():
        ex = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        assert ex & co.OVERLAY_EX_STYLE == co.OVERLAY_EX_STYLE, hex(ex)
        aff = ctypes.c_uint32()
        ctypes.windll.user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(aff))
        assert aff.value == (co.WDA_EXCLUDEFROMCAPTURE if overlay.capture_excluded else co.WDA_NONE)
    assert overlay._strips, "no border on the screen with the pointer"
    assert ctypes.windll.user32.GetForegroundWindow() not in overlay._all_windows()  # never takes focus
    report(capsys, f"capture exclusion (WDA_EXCLUDEFROMCAPTURE) on this Windows: {overlay.capture_excluded}")
    overlay.hide()
    assert wait(lambda: not any(visible(h) for h in overlay._all_windows()), 1.0)


def orange_pixels(img) -> int:
    px = img.load()
    n = 0
    for x in range(img.width):
        for y in range(img.height):
            r, g, b = px[x, y][:3]
            if r > 200 and 80 < g < 150 and b < 70:
                n += 1
    return n


def test_delta_never_sees_its_own_ring_in_a_screenshot(overlay):
    import mss
    from PIL import Image

    from delta.system.agent_cursor import AgentCursor, install

    ctypes.windll.user32.SetCursorPos(400, 400)
    signal = AgentCursor(overlay_factory=lambda on_stop: overlay)
    install(signal)
    try:
        signal.begin()
        assert wait(lambda: visible(overlay._hwnd))
        time.sleep(0.2)
        from delta.system.agent_cursor import capture_guard

        with capture_guard(), mss.mss() as sct:
            shot = sct.grab({"left": 360, "top": 360, "width": 80, "height": 80})
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        assert orange_pixels(img) < 20, "the in-control ring showed up in Delta's own screenshot"
        assert visible(overlay._hwnd)  # and it is back on screen for the person
    finally:
        signal.stop()
        install(None)


def test_heartbeat_loss_hides_it_by_itself():
    from delta.system.control_overlay import Win32Overlay

    o = Win32Overlay(heartbeat_timeout_s=0.3)
    assert o.start()
    try:
        o.show()
        assert wait(lambda: visible(o._hwnd))
        assert wait(lambda: not visible(o._hwnd), 2.0), "a hung Delta must not leave the ring on screen"
    finally:
        o.close()
    assert not o.alive


def test_stop_hotkey_and_esc_messages_reach_the_stop_callback(overlay, capsys):
    from delta.system import control_overlay as co

    user32 = ctypes.WinDLL("user32")
    user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.PostMessageW(overlay._hwnd, co.WM_HOTKEY, co.HOTKEY_ID, 0)
    user32.PostMessageW(overlay._hwnd, co.WM_STOPKEY, 0, 0)
    assert wait(lambda: overlay.hits == ["hotkey", "esc"])
    report(capsys, f"Ctrl+Alt+Shift+S registered: {overlay.hotkey_registered}")


def test_on_and_off_are_immediate(tmp_path, capsys):
    from delta.system.agent_cursor import AgentCursor

    signal = AgentCursor(tmp_path)
    signal.arm()
    o = signal._overlay
    try:
        t0 = time.perf_counter()
        signal.begin()
        assert wait(lambda: visible(o._hwnd), 1.0)
        on = time.perf_counter() - t0
        t0 = time.perf_counter()
        signal.stop()
        assert wait(lambda: not visible(o._hwnd), 1.0)
        off = time.perf_counter() - t0
        report(capsys, f"ring on in {on * 1000:.0f} ms, off in {off * 1000:.0f} ms")
        assert off < 0.5
    finally:
        signal.close()
    assert wait(lambda: not o.alive, 2.0)


def test_stop_really_releases_a_key_delta_holds():
    import pyautogui

    from delta.system import input_stop

    VK_SHIFT = 0x10
    input_stop.track_pyautogui(pyautogui)
    input_stop.reset()
    pyautogui.keyDown("shift")
    try:
        assert ctypes.windll.user32.GetAsyncKeyState(VK_SHIFT) & 0x8000
        assert input_stop.release_held() == ["shift"]
        assert not ctypes.windll.user32.GetAsyncKeyState(VK_SHIFT) & 0x8000
        input_stop.request()
        with pytest.raises(input_stop.StopRequestedError):
            pyautogui.keyDown("ctrl")
        assert not ctypes.windll.user32.GetAsyncKeyState(0x11) & 0x8000
    finally:
        input_stop.reset()
        pyautogui.keyUp("shift")
