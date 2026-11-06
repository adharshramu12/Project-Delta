"""The "Delta is in control" signal: on while Delta drives the mouse, keyboard or a browser, gone when it stops.

`AgentCursor` decides *when* the signal is on; delta.system.control_overlay draws it (an orange ring that
follows the pointer, a thin orange border around the screen, a small label) and owns the stop hotkeys.

- `active()` wraps every action where Delta drives input (desktop tools, your-browser tools, Delta's own
  browser). Nested and back-to-back actions keep it on without flicker (a short hold after the last one).
- An action that fails or is cancelled hides it at once, without the hold. `stop()` (task end, Stop, the
  hotkeys) hides it at once whatever is still nested.
- Heartbeat: while it is on, the event loop that turned it on beats every second; if Delta hangs, the
  overlay hides itself after a few seconds without a beat.
- `enabled` (Settings > Appearance, `browser.orange_cursor`) turns the ring, border and label off. The stop
  hotkeys stay on: stopping is a safety feature, not decoration.

Earlier versions recoloured the real Windows pointer (SetSystemCursor) and left a marker file while it was
orange. That is gone: the pointer is never changed. `recover()` still gives a pointer back that an older
version left orange after a crash.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from delta.system.logging import log

HOLD_S = 1.0     # keep it on briefly between back-to-back steps
BEAT_S = 1.0     # heartbeat to the overlay while it is shown
SPI_SETCURSORS = 0x0057

_current: AgentCursor | None = None


class AgentCursor:
    def __init__(self, data_dir: Path | None = None, enabled: bool = True,
                 overlay_factory: Callable[[Callable[[str], None]], Any] | None = None) -> None:
        self.enabled = enabled
        self.data_dir = data_dir
        self._factory = overlay_factory
        self._overlay: Any = None
        self._on_stop: Callable[[str], None] | None = None
        self._depth = 0
        self._on = False
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._beating = False
        atexit.register(self.close)

    # --- set-up -------------------------------------------------------------------------------------------
    def arm(self, on_stop: Callable[[str], None] | None = None) -> None:
        """Start the overlay thread (idle until an action) so the stop hotkeys work for the whole task.
        Called at the start of every task; cheap after the first time."""
        if on_stop is not None:
            self._on_stop = on_stop
        with contextlib.suppress(RuntimeError):
            self._loop = asyncio.get_running_loop()
        self._ensure_overlay()

    def _ensure_overlay(self) -> Any:
        if self._overlay is None:
            factory = self._factory
            if factory is None:
                from delta.system.control_overlay import make_overlay

                factory = make_overlay
            try:
                self._overlay = factory(self._stop_key)
                self._overlay.start()
            except Exception as e:  # a missing signal must never break an action
                log.warning("signal.overlay_failed", error=str(e))
                from delta.system.control_overlay import NullOverlay

                self._overlay = NullOverlay()
        return self._overlay

    def _stop_key(self, kind: str) -> None:
        """Esc twice or Ctrl+Alt+Shift+S (called on the overlay thread)."""
        if self._on_stop is not None:
            self._on_stop(kind)

    # --- on and off ---------------------------------------------------------------------------------------
    @contextlib.contextmanager
    def active(self) -> Iterator[None]:
        """Wrap any action where Delta drives the mouse, the keyboard or a browser."""
        self.begin()
        try:
            yield
        except BaseException:
            self.end(now=True)  # failed or stopped: gone at once
            raise
        self.end()

    def begin(self) -> None:
        with self._lock:
            self._depth += 1
            if self._timer:
                self._timer.cancel()
                self._timer = None
            if not self._on:
                self._show()

    def end(self, now: bool = False) -> None:
        with self._lock:
            self._depth = max(0, self._depth - 1)
            if now:
                self._depth = 0
                self._hide()
            elif self._depth == 0 and self._on:
                self._timer = threading.Timer(HOLD_S, self._hide_if_idle)
                self._timer.daemon = True
                self._timer.start()

    def stop(self) -> None:
        """Task ended or was stopped: hide now, whatever is still running."""
        with self._lock:
            self._depth = 0
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._hide()

    restore = stop  # older name (shutdown hooks and the settings page call it)

    @property
    def on(self) -> bool:
        return self._on

    def _hide_if_idle(self) -> None:
        with self._lock:
            if self._depth == 0:
                self._hide()

    def _show(self) -> None:
        self._on = True
        if not self.enabled:
            return
        try:
            self._ensure_overlay().show()
        except Exception as e:
            log.warning("signal.show_failed", error=str(e))
        self._start_beat()

    def _hide(self) -> None:
        was = self._on
        self._on = False
        if was and self._overlay is not None:
            try:
                self._overlay.hide()
            except Exception as e:
                log.warning("signal.hide_failed", error=str(e))

    def _start_beat(self) -> None:
        if self._beating:
            return
        loop = self._loop
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
        if loop is None or loop.is_closed():
            self._beat_from_thread()  # no event loop to watch: keep it on while the action runs
            return
        self._beating = True

        def tick() -> None:
            if not self._on or self._overlay is None:
                self._beating = False
                return
            self._overlay.beat()
            loop.call_later(BEAT_S, tick)

        try:
            loop.call_soon_threadsafe(tick)
        except RuntimeError:
            self._beating = False

    def _beat_from_thread(self) -> None:
        self._beating = True

        def run() -> None:
            stop = threading.Event()
            while self._on and self._overlay is not None:
                self._overlay.beat()
                stop.wait(BEAT_S)
            self._beating = False

        threading.Thread(target=run, name="delta-signal-beat", daemon=True).start()

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.enabled = enabled
            if not enabled and self._overlay is not None:
                self._overlay.hide()
            elif enabled and self._on:
                self._show()

    @contextlib.contextmanager
    def hidden_for_capture(self) -> Iterator[None]:
        overlay = self._overlay
        if overlay is None or not self._on:
            yield
            return
        with overlay.hidden_for_capture():
            yield

    def close(self) -> None:
        self.stop()
        overlay, self._overlay = self._overlay, None
        if overlay is not None:
            with contextlib.suppress(Exception):
                overlay.close()

    # --- older versions ------------------------------------------------------------------------------------
    @staticmethod
    def recover(data_dir: Path) -> bool:
        """At start-up: an older Delta that died while it had the pointer orange gives it back."""
        marker = data_dir / "cursor.active"
        if not marker.exists():
            return False
        reset_system_cursors()
        with contextlib.suppress(OSError):
            marker.unlink()
        log.info("cursor.recovered_after_crash")
        return True


def reset_system_cursors() -> None:
    """Reload the user's own cursor scheme (undoes an older version's SetSystemCursor)."""
    with contextlib.suppress(Exception):
        import ctypes

        ctypes.windll.user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)


# --- one signal per Delta process, for the places that have no controller handed to them ---------------------

def install(cursor: AgentCursor | None) -> None:
    global _current
    _current = cursor


def current() -> AgentCursor | None:
    return _current


def driving() -> Any:
    """Context for an action that drives input where no controller was handed in (Delta's own browser)."""
    c = _current
    return c.active() if c is not None else contextlib.nullcontext()


def capture_guard() -> Any:
    """Context around Delta's own screenshots: keeps the signal out of them where Windows can't."""
    c = _current
    return c.hidden_for_capture() if c is not None else contextlib.nullcontext()
