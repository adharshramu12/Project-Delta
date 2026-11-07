"""Crash guard: an unexpected exception anywhere in Delta's service is logged and reported, never a silent death.

Before, a failure outside a task had no owner: a background thread (the apps index, a toast, the overlay) died
with a traceback only in service.log, an asyncio task's error was printed by asyncio and forgotten, a Telegram
handler that raised left the phone with no answer at all, and a dashboard route answered a bare "Internal Server
Error". Now each of those goes through one place (CrashGuard.report) that
  - writes the full traceback to the structured log and to <data>/logs/crashes.log (kept under 1 MB),
  - shows one plain line among the dashboard's warnings (the same kind of error at most once every 10 minutes),
  - and lets Delta carry on: the guard never re-raises.
`delta run` installs it for threads and the event loop (daemon.run); the dashboard API and the Telegram channel
call report() from their own error handlers.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from delta.system.logging import log

MAX_LOG_BYTES = 1_000_000
REPEAT_S = 600.0  # the same (where, kind of error) is shown again at most this often


class CrashGuard:
    def __init__(self, data_dir: Path | None = None, warn: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.file = Path(data_dir) / "logs" / "crashes.log" if data_dir is not None else None
        self.warn = warn
        self.clock = clock
        self.count = 0
        self._shown: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def report(self, where: str, exc: BaseException | None, detail: str = "") -> str:
        """Log and show one unexpected error; returns the one-line message. Never raises."""
        try:
            kind = type(exc).__name__ if exc is not None else "Error"
            text = (str(exc) if exc is not None else detail).strip().splitlines()
            line = f"{kind}: {text[0][:200]}" if text and text[0] else kind
            tb = "".join(traceback.format_exception(exc)) if exc is not None else detail
            with self._lock:
                self.count += 1
                log.error("crash.caught", where=where, error=line, traceback=tb[-4000:])
                self._write(where, line, tb)
                key = (where, kind)
                now = self.clock()
                show = now - self._shown.get(key, -1e9) >= REPEAT_S
                if show:
                    self._shown[key] = now
            message = (f"Something went wrong in {where} ({line}). Delta kept running"
                       + (f"; details in {self.file}." if self.file else "."))
            if show and self.warn is not None:
                with contextlib.suppress(Exception):
                    self.warn(message)
            return message
        except Exception:  # the guard itself must never be the next crash
            return f"Something went wrong in {where}."

    def _write(self, where: str, line: str, tb: str) -> None:
        if self.file is None:
            return
        with contextlib.suppress(OSError):
            self.file.parent.mkdir(parents=True, exist_ok=True)
            if self.file.exists() and self.file.stat().st_size > MAX_LOG_BYTES:
                self.file.replace(self.file.with_suffix(".log.1"))
            with self.file.open("a", encoding="utf-8") as f:
                f.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} {where}: {line}\n{tb}")

    # --- where it is installed -------------------------------------------------------------------------------
    def install_threads(self) -> None:
        """Any thread of Delta's that ends with an exception (not SystemExit) is reported instead of only printed."""
        def hook(args: threading.ExceptHookArgs) -> None:
            if args.exc_type is SystemExit:
                return
            name = args.thread.name if args.thread is not None else "a background thread"
            self.report(f"background thread '{name}'", args.exc_value)

        threading.excepthook = hook

    def install_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Errors the event loop would only print ("Task exception was never retrieved", a failing callback) are
        reported. Windows' harmless ConnectionResetError (a browser tab closing a socket) stays quiet."""
        def handler(lp: asyncio.AbstractEventLoop, ctx: dict[str, Any]) -> None:
            exc = ctx.get("exception")
            if isinstance(exc, ConnectionResetError):
                return
            task = ctx.get("task") or ctx.get("future")
            name = task.get_name() if isinstance(task, asyncio.Task) else ""
            where = f"background task '{name}'" if name else "a background job"
            self.report(where, exc if isinstance(exc, BaseException) else None, str(ctx.get("message", "")))

        loop.set_exception_handler(handler)


def guard_of(c: Any) -> CrashGuard:
    """The container's guard (made on first use, so tests that build a container get one too)."""
    guard = getattr(c, "crash_guard", None)
    if guard is None:
        settings = getattr(c, "settings", None)
        guard = CrashGuard(getattr(settings, "data_dir", None), getattr(c, "warn", None))
        with contextlib.suppress(Exception):
            c.crash_guard = guard
    return guard
