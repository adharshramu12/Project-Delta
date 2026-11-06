"""`delta watch list|run|remove`: watchers from a terminal on the PC. Adding one is easiest by asking Delta ("watch my
college portal for exams and timetable changes") or in Settings > Logins and watchers."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from typing import Any

from delta.config import Settings


def add_parser(sub: Any) -> None:
    p = sub.add_parser("watch", help="sites Delta watches with a saved login: list, run, remove")
    p.add_argument("action", choices=["list", "run", "remove"])
    p.add_argument("watcher", nargs="?", help="the watcher's name or id")


def run_cli(settings: Settings, args: argparse.Namespace, out: Callable[[str], None] = print,
            service: Any = None) -> int:
    from delta.watchers.store import WatchStore

    store = WatchStore(settings.data_dir / "watchers.db") if service is None else service.store
    if args.action == "list":
        items = store.list()
        if not items:
            out("No watchers. Ask Delta, for example: \"watch my college portal for exams\".")
        for w in items:
            out(f"{w.name} [{w.id}]: {w.what}; every {w.interval_minutes // 60} h; "
                f"last: {w.last_status or 'not yet'} {w.last_message}".rstrip())
        return 0
    found = store.find(args.watcher or "")
    if found is None:
        out(f"No watcher called '{args.watcher}'.")
        return 1
    if args.action == "remove":
        from delta.scheduler.store import ScheduleStore

        sched = ScheduleStore(settings.data_dir / "schedules.db")
        for sid in (found.schedule_id, found.digest_schedule_id):
            if sid:
                sched.delete(sid)
        store.delete(found.id)
        out(f"Stopped watching {found.name}.")
        return 0
    return asyncio.run(_run(settings, found.id, out))


async def _run(settings: Settings, wid: str, out: Callable[[str], None]) -> int:
    """One check now, in Delta's private browser (asked for from this terminal, so no approval is asked)."""
    from delta.logins.cli import store_for
    from delta.logins.service import LoginService
    from delta.safety.audit import AuditLog, load_or_create_key
    from delta.tools.browser import BrowserManager, settle
    from delta.watchers.service import WatchService
    from delta.watchers.store import WatchStore

    audit = AuditLog(settings.data_dir / "audit.jsonl", key_loader=load_or_create_key)
    logins = LoginService(store_for(settings), audit, None, settle=settle)

    async def allowed(login: Any, task_id: str = "", why: str = "") -> tuple[bool, str]:
        return True, "asked from the terminal"

    logins.allowed = allowed  # type: ignore[method-assign]
    mgr = BrowserManager(settings.data_dir)
    mgr.engine = settings.browser.private_engine

    class Hub:
        pass

    hub = Hub()
    hub.mgr = mgr  # type: ignore[attr-defined]
    service = WatchService(WatchStore(settings.data_dir / "watchers.db"), logins, hub, audit=audit, settle=settle)
    w = service.store.get(wid)
    assert w is not None
    report = await service.check(w, trigger="schedule")
    out(report.text or report.summary)
    return 0 if report.status == "done" else 1
