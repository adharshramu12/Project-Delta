"""Screen co-pilot: press Ctrl+Alt+D anywhere and Delta takes a picture of the screen, opens the dashboard on a new
chat with that picture attached, and waits for you: "explain this", "fix this error", "what do I do next?".

Nothing is done on its own. The picture goes no further than the chat box until you press Send, and anything Delta
then does on the PC goes through the usual approvals. The same masking as every Delta screenshot applies (Delta's
own windows and the in-control ring are left out). The hotkey is registered with RegisterHotKey, so it never sees
other keys; if another app already owns Ctrl+Alt+D it is skipped and the log says so.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ctypes
import sys
import threading
from collections.abc import Callable
from typing import Any

from delta.system.logging import log

HOTKEY = "Ctrl+Alt+D"
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
VK_D = 0x44
WM_HOTKEY, WM_QUIT = 0x312, 0x12
HOTKEY_ID = 0xD17B
MAX_WIDTH = 1600  # wide enough to read an error message, small enough for any vision model


class HotkeyThread:
    """One global hotkey on its own thread with a message loop. `on_press` runs on that thread: keep it short."""

    def __init__(self, on_press: Callable[[], None]) -> None:
        self.on_press = on_press
        self.registered = False
        self._thread: threading.Thread | None = None
        self._tid = 0
        self._ready = threading.Event()

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        self._thread = threading.Thread(target=self._main, name="copilot-hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return self.registered

    def _main(self) -> None:
        from ctypes import wintypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        self._tid = kernel32.GetCurrentThreadId()
        self.registered = bool(user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_D))
        self._ready.set()
        if not self.registered:
            log.info("copilot.hotkey_unavailable", hotkey=HOTKEY)  # another app owns it
            return
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    try:
                        self.on_press()
                    except Exception as e:
                        log.warning("copilot.press_failed", error=str(e))
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)

    def stop(self) -> None:
        if self._tid:
            with contextlib.suppress(Exception):
                ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)


async def capture_to_chat(c: Any, show: Callable[[], None]) -> dict[str, Any] | None:
    """Take the screen, keep it as an unsent attachment, tell the dashboard, then bring the dashboard up.
    Returns the attachment as the dashboard sees it (no path), or None when it could not be taken."""
    svc = getattr(getattr(c, "orchestrator", None), "attachments", None)
    if svc is None:
        log.info("copilot.no_attachments")
        return None
    from delta.tools.screen import capture_jpeg

    try:
        b64, _, _ = await asyncio.to_thread(capture_jpeg, MAX_WIDTH)
        att = await asyncio.to_thread(svc.store.save_bytes, base64.b64decode(b64), "Screen.jpg", "", "copilot")
    except Exception as e:
        log.warning("copilot.capture_failed", error=str(e))
        return None
    public = att.public()
    await c.events.publish("copilot.screen", attachment=public)
    c.audit.record("copilot.screen_taken")  # that it happened, never the picture
    await asyncio.to_thread(show)
    return dict(public)


def start(c: Any, show: Callable[[], None]) -> HotkeyThread | None:
    """Register the hotkey (Windows only, unless copilot is switched off) and wire it to capture_to_chat."""
    loop = asyncio.get_running_loop()
    busy = threading.Lock()

    def pressed() -> None:
        if not busy.acquire(blocking=False):  # a second press while the first is being handled
            return

        def run() -> None:
            task = asyncio.ensure_future(capture_to_chat(c, show))
            task.add_done_callback(lambda _t: busy.release())

        loop.call_soon_threadsafe(run)

    hk = HotkeyThread(pressed)
    if not hk.start():
        return None
    log.info("copilot.ready", hotkey=HOTKEY)
    return hk
