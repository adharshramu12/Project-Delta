"""Orange mouse pointer while Delta controls the mouse/keyboard, so people can always tell
Delta's actions from their own.

Uses the documented Win32 calls: SetSystemCursor to swap in an orange arrow, and
SystemParametersInfo(SPI_SETCURSORS) to reload the user's own cursor scheme afterwards, so a
custom theme comes back exactly as it was.

Windows keeps a swapped cursor until someone resets it, so restoring is guaranteed on every
path: after each action (with a short hold to avoid flicker between steps), at shutdown, at
process exit, and on the next start if a previous run died (a marker file records "orange on").
"""

from __future__ import annotations

import atexit
import contextlib
import io
import struct
import threading
from collections.abc import Iterator
from pathlib import Path

from delta.system.logging import log

ORANGE = (255, 122, 0, 255)
OUTLINE = (40, 20, 0, 255)
HOLD_S = 1.5  # keep orange briefly between back-to-back steps
SPI_SETCURSORS = 0x0057
# Pointer roles swapped while Delta acts: normal arrow, text I-beam, link hand, busy arrow.
CURSOR_IDS = (32512, 32513, 32649, 32650)


def make_orange_cursor(size: int = 32) -> bytes:
    """A .cur file: orange arrow with a dark outline, hotspot at the tip (1,1)."""
    from PIL import Image, ImageDraw

    s = size / 32
    arrow = [(1, 1), (1, 24), (7, 18), (11, 27), (15, 25), (11, 17), (19, 17)]
    pts = [(x * s, y * s) for x, y in arrow]
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(img).polygon(pts, fill=ORANGE, outline=OUTLINE, width=max(1, round(1.5 * s)))
    buf = io.BytesIO()
    img.save(buf, format="ICO", sizes=[(size, size)])
    data = bytearray(buf.getvalue())
    # ICO -> CUR: type 2, and the directory entry's planes/bitcount become the hotspot.
    struct.pack_into("<H", data, 2, 2)
    struct.pack_into("<HH", data, 6 + 4, 1, 1)
    return bytes(data)


class AgentCursor:
    def __init__(self, data_dir: Path, enabled: bool = True) -> None:
        self.enabled = enabled
        self.marker = data_dir / "cursor.active"
        self.file = data_dir / "cursors" / "delta-orange.cur"
        self._depth = 0
        self._on = False
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        atexit.register(self.restore)

    @contextlib.contextmanager
    def active(self) -> Iterator[None]:
        """Wrap any action where Delta drives the mouse or keyboard."""
        self.begin()
        try:
            yield
        finally:
            self.end()

    def begin(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._depth += 1
            if self._timer:
                self._timer.cancel()
                self._timer = None
            if not self._on:
                self._apply()

    def end(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._depth = max(0, self._depth - 1)
            if self._depth == 0 and self._on:
                self._timer = threading.Timer(HOLD_S, self._restore_if_idle)
                self._timer.daemon = True
                self._timer.start()

    def _restore_if_idle(self) -> None:
        with self._lock:
            if self._depth == 0:
                self._restore_locked()

    def _apply(self) -> None:
        try:
            import ctypes

            if not self.file.exists():
                self.file.parent.mkdir(parents=True, exist_ok=True)
                self.file.write_bytes(make_orange_cursor())
            self.marker.write_text("1")  # written first: a crash after this still gets restored
            user32 = ctypes.windll.user32
            user32.LoadCursorFromFileW.restype = ctypes.c_void_p
            for cid in CURSOR_IDS:
                handle = user32.LoadCursorFromFileW(str(self.file))  # SetSystemCursor consumes it
                if handle:
                    user32.SetSystemCursor(ctypes.c_void_p(handle), cid)
            self._on = True
        except Exception as e:  # a cosmetic feature must never break an action
            log.warning("cursor.apply_failed", error=str(e))

    def restore(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._restore_locked()

    def _restore_locked(self) -> None:
        if not self._on and not self.marker.exists():
            return
        reset_system_cursors()
        self._on = False
        with contextlib.suppress(OSError):
            self.marker.unlink()

    @staticmethod
    def recover(data_dir: Path) -> bool:
        """At start-up: if a previous run died while orange, give the user their cursor back."""
        marker = data_dir / "cursor.active"
        if not marker.exists():
            return False
        reset_system_cursors()
        with contextlib.suppress(OSError):
            marker.unlink()
        log.info("cursor.recovered_after_crash")
        return True


def reset_system_cursors() -> None:
    """Reload the user's own cursor scheme (undoes SetSystemCursor)."""
    with contextlib.suppress(Exception):
        import ctypes

        ctypes.windll.user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)
