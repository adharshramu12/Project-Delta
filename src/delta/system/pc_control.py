"""Control the PC from anywhere (Telegram): volume, brightness, the open apps, a screenshot, a webcam photo.

Each function does one thing, never raises for an expected problem (it raises PCControlError with a
plain-words message instead), and is safe to call from a worker thread (COM is set up per call).

Privacy: the webcam's own light comes on while a photo is taken (Windows enforces it; Delta does not and
cannot hide it), and every photo, screenshot and closed app is written to the audit log by the caller."""

from __future__ import annotations

import contextlib
import ctypes
import subprocess
import tempfile
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import psutil


class PCControlError(RuntimeError):
    """Shown to the user as is."""


# --- volume (Windows Core Audio through pycaw) ---------------------------------------------------------
def _endpoint() -> object:
    import comtypes
    from pycaw.pycaw import AudioUtilities

    comtypes.CoInitialize()
    speakers = AudioUtilities.GetSpeakers()
    if speakers is None:
        raise PCControlError("No speaker or headphone output is active on the PC.")
    ep = getattr(speakers, "EndpointVolume", None)  # newer pycaw
    if ep is None:  # older pycaw
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import IAudioEndpointVolume

        ep = ctypes.cast(speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None),
                         ctypes.POINTER(IAudioEndpointVolume))
    return ep


def volume() -> tuple[int, bool]:
    """(level 0-100, muted)."""
    ep = _endpoint()
    return round(ep.GetMasterVolumeLevelScalar() * 100), bool(ep.GetMute())  # type: ignore[attr-defined]


def set_volume(level: int | None = None, step: int = 0, mute: bool | None = None) -> tuple[int, bool]:
    """Set an exact level, or move by `step`, and/or (un)mute. Changing the level unmutes."""
    ep = _endpoint()
    now = round(ep.GetMasterVolumeLevelScalar() * 100)  # type: ignore[attr-defined]
    target = now + step if level is None else level
    if level is not None or step:
        ep.SetMasterVolumeLevelScalar(max(0, min(100, target)) / 100, None)  # type: ignore[attr-defined]
        ep.SetMute(0, None)  # type: ignore[attr-defined]
    if mute is not None:
        ep.SetMute(int(mute), None)  # type: ignore[attr-defined]
    return volume()


# --- brightness (WMI: laptop panels; most external monitors do not allow it) -----------------------------
def _ps(command: str) -> str:
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                       capture_output=True, text=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise PCControlError("This screen doesn't let Windows change its brightness (usually an external "
                             "monitor: use its own buttons).")
    return r.stdout.strip()


def brightness() -> int:
    out = _ps("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction Stop | "
              "Select-Object -First 1).CurrentBrightness")
    if not out.isdigit():
        raise PCControlError("I couldn't read the screen brightness.")
    return int(out)


def set_brightness(level: int | None = None, step: int = 0) -> int:
    target = max(5, min(100, (brightness() + step) if level is None else level))  # 0% would look like "off"
    _ps("$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods -ErrorAction Stop; "
        f"Invoke-CimMethod -InputObject $m -MethodName WmiSetBrightness -Arguments @{{Timeout=1; Brightness={target}}}"
        " | Out-Null")
    return brightness()


# --- open apps (visible windows) ------------------------------------------------------------------------
@dataclass
class OpenWindow:
    hwnd: int
    title: str
    app: str  # process name, e.g. chrome.exe
    pid: int


_HIDDEN = {"Program Manager", "Windows Input Experience", "Settings", "Microsoft Text Input Application"}
_NO_UNSAVED_WORK = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vlc.exe",
                    "spotify.exe", "calculatorapp.exe", "photos.exe", "explorer.exe", "whatsapp.exe",
                    "telegram.exe", "steam.exe"}  # closing these cannot lose a document


THEME_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


def windows_theme() -> str:
    """'light' or 'dark': what Windows apps use now."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, THEME_KEY) as k:
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return "light"


def set_windows_theme(mode: str) -> str:
    """Switch Windows (apps and taskbar) to 'light', 'dark' or the opposite ('toggle'). Only the user's own
    personal colour choice; open apps are told so they repaint right away."""
    import ctypes
    import winreg

    if mode == "toggle":
        mode = "light" if windows_theme() == "dark" else "dark"
    if mode not in ("light", "dark"):
        raise PCControlError("Theme must be light, dark or toggle.")
    value = 1 if mode == "light" else 0
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, THEME_KEY, 0, winreg.KEY_SET_VALUE) as k:
            for name in ("AppsUseLightTheme", "SystemUsesLightTheme"):
                winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, value)
    except OSError as e:
        raise PCControlError(f"Windows did not allow the theme change: {e}") from e
    with contextlib.suppress(Exception):  # WM_SETTINGCHANGE "ImmersiveColorSet": apps repaint now
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "ImmersiveColorSet", 0x0002, 2000, None)
    return mode


def open_windows(exclude_titles: tuple[str, ...] = ("Delta",)) -> list[OpenWindow]:
    """The windows a person would see on the taskbar, newest first in z-order."""
    user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
    found: list[OpenWindow] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd: int, _: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # GW_OWNER: dialogs/tool windows
            return True
        ex = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
        if ex & (0x80 | 0x20 | 0x08000000) and not ex & 0x40000:  # tool/overlay/no-activate, unless APPWINDOW
            return True  # e.g. dictation bubbles, GPU overlays: not apps on the taskbar
        cloaked = ctypes.c_int(0)
        dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))  # DWMWA_CLOAKED
        if cloaked.value:  # suspended Store apps keep invisible windows
            return True
        buf = ctypes.create_unicode_buffer(300)
        user32.GetWindowTextW(hwnd, buf, 300)
        title = buf.value.strip()
        if not title or title in _HIDDEN or title in exclude_titles or title.endswith(" · Delta"):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            app = psutil.Process(pid.value).name()
        except psutil.Error:
            app = "?"
        found.append(OpenWindow(int(hwnd), title, app, pid.value))
        return True

    user32.EnumWindows(visit, 0)
    return found


def may_lose_work(w: OpenWindow) -> bool:
    """Closing it could lose unsaved work (a document app, or a title marked as changed)."""
    return w.app.lower() not in _NO_UNSAVED_WORK or w.title.startswith("*")


def close_window(hwnd: int, force: bool = False, wait_s: float = 4) -> str:
    """Ask the window to close (as clicking X does). 'closed', or 'still_open' when the app is asking
    something (e.g. save changes?). force=True ends the app's process instead."""
    user32 = ctypes.windll.user32
    if not user32.IsWindow(hwnd):
        return "closed"
    if force:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            psutil.Process(pid.value).kill()
        except psutil.Error as e:
            raise PCControlError(f"Windows didn't let me end that app ({type(e).__name__}).") from e
        return "closed"
    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
            return "closed"
        time.sleep(0.2)
    return "still_open"


# --- pictures -------------------------------------------------------------------------------------------
def _out(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix="delta-")) / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}.jpg"


def screenshot() -> Path:
    """The whole screen (every monitor side by side), as a JPEG file."""
    import mss
    from PIL import Image

    with mss.mss() as grab:
        everything = grab.monitors[0]
        shot = grab.grab(everything)
    img = Image.frombytes("RGB", shot.size, shot.rgb)
    from delta.logins import mask

    if mask.active():  # password boxes Delta filled in the user's own browser
        mask.mask_screen(img, (int(everything.get("left", 0)), int(everything.get("top", 0))))
    if img.width > 2560:
        img.thumbnail((2560, 2560))
    path = _out("screen")
    img.save(path, "JPEG", quality=85)
    return path


def camera_photo(index: int | None = None) -> Path:
    """One photo from the webcam (the camera light shows while it is taken). Tries the built-in camera
    first; skips cameras that return only black frames (covered lens, virtual cameras like Camo idle)."""
    import cv2

    candidates = [index] if index is not None else [0, 1, 2]
    for i in candidates:
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        try:
            if not cap.isOpened():
                continue
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)  # HD when the camera has it (it keeps its best otherwise)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            # A cold camera needs a moment: wait (up to 4 s) for a real frame, then let exposure settle.
            frame, good, deadline = None, 0, time.monotonic() + 4
            while time.monotonic() < deadline and good < 6:
                ok, img = cap.read()
                if ok and img is not None and img.mean() >= 8:
                    frame, good = img, good + 1
            if frame is None:
                continue
            path = _out("camera")
            cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            return path
        finally:
            cap.release()
    raise PCControlError("No working camera: it may be in use by another app, covered, or turned off in "
                         "Windows Settings > Privacy > Camera.")
