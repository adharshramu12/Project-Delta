"""Startup/shutdown coordination. Shutdown must leave nothing behind: no child processes,
no model weights in VRAM, no browser left attached."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import psutil

from delta.system.logging import log

ShutdownHook = Callable[[], Awaitable[None]]


class AlreadyRunningError(RuntimeError):
    pass


class Lifecycle:
    def __init__(self, data_dir: Path) -> None:
        self.pid_file = data_dir / "delta.pid"
        self._hooks: list[tuple[str, ShutdownHook]] = []
        self.stopping = asyncio.Event()

    def acquire_single_instance(self) -> None:
        if self.pid_file.exists():
            with contextlib.suppress(ValueError, OSError):
                pid = int(self.pid_file.read_text())
                if pid != os.getpid() and psutil.pid_exists(pid):
                    raise AlreadyRunningError(f"Delta is already running (pid {pid}).")
        self.pid_file.write_text(str(os.getpid()))

    def on_shutdown(self, name: str, hook: ShutdownHook) -> None:
        self._hooks.append((name, hook))

    def request_stop(self) -> None:
        self.stopping.set()

    async def shutdown(self) -> None:
        # Reverse order: things started last stop first.
        for name, hook in reversed(self._hooks):
            try:
                await asyncio.wait_for(hook(), timeout=3)
                log.info("lifecycle.stopped", component=name)
            except Exception as e:
                log.error("lifecycle.stop_failed", component=name, error=str(e))
        self._kill_children()
        with contextlib.suppress(OSError):
            self.pid_file.unlink()

    @staticmethod
    def _kill_children() -> None:
        children = psutil.Process().children(recursive=True)
        for child in children:
            with contextlib.suppress(psutil.Error):
                child.terminate()
        _, alive = psutil.wait_procs(children, timeout=3)
        for child in alive:
            with contextlib.suppress(psutil.Error):
                child.kill()
