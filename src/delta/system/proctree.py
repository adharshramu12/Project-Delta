"""Processes Delta itself started for a tool call, and stopping them with everything they started.

A tool that runs a program (run_shell, run_python, a PowerShell helper) registers the process with
`track(pid)`. The watchdog opens a scope around each tool call; on a timeout or a Stop it ends every
tracked process together with its whole tree of children (a `Start-Process`, a script's own
subprocesses), not just the top one (audit finding M5).

Only processes registered here are ever touched: never the user's own browser, an app the user (or
`open_app`) opened, or anything Delta did not start for that call. A process is identified by pid and
start time, so a pid Windows reused for an unrelated program is left alone.
"""

from __future__ import annotations

import contextlib
import os
from contextvars import ContextVar, Token

import psutil

Tracked = list[tuple[int, float]]  # (pid, create_time)
_SCOPE: ContextVar[Tracked | None] = ContextVar("delta_process_scope", default=None)


def begin_scope() -> tuple[Token[Tracked | None], Tracked]:
    scope: Tracked = []
    return _SCOPE.set(scope), scope


def end_scope(token: Token[Tracked | None]) -> None:
    with contextlib.suppress(ValueError):
        _SCOPE.reset(token)


def track(pid: int | None) -> None:
    """Remember a process this tool call started (no-op outside a watchdog scope)."""
    scope = _SCOPE.get()
    if scope is None or not pid:
        return
    try:
        scope.append((pid, psutil.Process(pid).create_time()))
    except psutil.Error:
        scope.append((pid, 0.0))


def _protected() -> set[int]:
    """Delta itself and its parents: never ended, whatever was registered."""
    out = {os.getpid()}
    with contextlib.suppress(psutil.Error):
        out |= {p.pid for p in psutil.Process().parents()}
    return out


def kill_tree(pid: int, create_time: float = 0.0, grace_s: float = 3.0) -> int:
    """End `pid` and all its descendants: politely first, then forcefully. Returns how many were ended."""
    if pid in _protected():
        return 0
    try:
        root = psutil.Process(pid)
        if create_time and abs(root.create_time() - create_time) > 1:
            return 0  # the pid now belongs to another program
        procs = [*root.children(recursive=True), root]
    except psutil.Error:
        return 0
    protected = _protected()
    procs = [p for p in procs if p.pid not in protected]
    for p in procs:
        with contextlib.suppress(psutil.Error):
            p.terminate()
    _, alive = psutil.wait_procs(procs, timeout=grace_s)
    for p in alive:
        with contextlib.suppress(psutil.Error):
            p.kill()
    if alive:
        psutil.wait_procs(alive, timeout=grace_s)
    return len(procs)


def kill_scope(scope: Tracked) -> int:
    return sum(kill_tree(pid, created) for pid, created in scope)


def kill_process(proc: object) -> int:
    """End a subprocess Delta started (asyncio or subprocess.Popen) with its whole tree."""
    pid = getattr(proc, "pid", None)
    if pid:
        killed = kill_tree(int(pid))
        if killed:
            return killed
    with contextlib.suppress(Exception):
        proc.kill()  # type: ignore[attr-defined]
    return 1
