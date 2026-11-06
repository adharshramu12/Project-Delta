"""`delta schedule add|list|pause|resume|remove|run`: schedules from a terminal on the PC.

Works whether or not Delta is running: it reads and writes the same schedules.db in the data folder,
and the running service picks changes up within seconds. `run` asks the running service to start a
schedule now (through the local API and its token), because runs happen inside the service.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from delta.config import Settings
from delta.scheduler.parse import ParseError, parse_schedule
from delta.scheduler.rules import describe, fmt_when, next_run, now_utc, zone_label
from delta.scheduler.store import ScheduleStore


def add_parser(sub: Any) -> None:
    p = sub.add_parser("schedule", help="scheduled tasks and reminders (add, list, pause, resume, remove, run)")
    p.add_argument("action", choices=["add", "list", "pause", "resume", "remove", "run"])
    p.add_argument("text", nargs="*", help="for add: the schedule in plain words; otherwise the schedule id or name")
    p.add_argument("--name", default="", help="for add: a short name")
    p.add_argument("-y", "--yes", action="store_true", help="for add/remove: do not ask to confirm")


def run_cli(settings: Settings, args: argparse.Namespace, ask: Callable[[str], str] = input,
            out: Callable[[str], None] = print, post: Callable[[str], dict[str, Any]] | None = None) -> int:
    store = ScheduleStore(settings.data_dir / "schedules.db", settings.scheduler.history_limit)
    try:
        return _run(settings, store, args, ask, out, post)
    finally:
        store.close()


def _run(settings: Settings, store: ScheduleStore, args: argparse.Namespace, ask: Callable[[str], str],
         out: Callable[[str], None], post: Callable[[str], dict[str, Any]] | None) -> int:
    tz = settings.scheduler.timezone or ""
    text = " ".join(args.text).strip()
    if args.action == "list":
        items = store.list()
        if not items:
            out("No schedules yet. Add one: delta schedule add \"every day at 9 am check my attendance\"")
            return 0
        for item in items:
            nxt_text = fmt_when(item.next_run, item.tz) if item.enabled else "paused"
            last = f"  last: {item.last_status}" if item.last_status else ""
            kind = " (reminder)" if item.kind == "reminder" else ""
            out(f"{item.id}  {item.name}{kind}\n          {item.when} · next: {nxt_text}{last}")
        return 0
    if not text:
        out("Say which one: delta schedule " + args.action + (" \"<when and what>\"" if args.action == "add"
                                                             else " <id or name>"))
        return 2
    if args.action == "add":
        try:
            p = parse_schedule(text, tz)
        except ParseError as e:
            out(f"✗ {e}")
            return 1
        nxt = next_run(p.rule, tz, now_utc())
        what = f"remind you to {p.prompt}" if p.kind == "reminder" else p.prompt
        when = describe(p.rule, tz)
        out(f"{when[0].upper()}{when[1:]}: {what}.\nNext run: {fmt_when(nxt, tz)} ({zone_label(tz)}).")
        if not args.yes and ask("Save it? [y/N] ").strip().lower() not in ("y", "yes"):
            out("Not saved.")
            return 1
        saved = store.add(args.name or p.name, p.prompt, p.rule, kind=p.kind, tz=tz, owner_channel="cli",
                          owner_user="owner", autonomy=str(settings.boundaries.autonomy))
        out(f"✓ Saved as {saved.id}. It runs while Delta is running (delta run or the Delta app).")
        return 0
    s = store.find(text)
    if s is None:
        out(f"✗ No single schedule matches '{text}'. See: delta schedule list")
        return 1
    if args.action == "pause":
        store.set_enabled(s.id, False)
        out(f"✓ Paused \"{s.name}\".")
    elif args.action == "resume":
        try:
            s2 = store.set_enabled(s.id, True)
        except ValueError as e:
            out(f"✗ {e}")
            return 1
        out(f"✓ Resumed \"{s.name}\". Next run: {fmt_when(s2.next_run if s2 else None, s.tz)}.")
    elif args.action == "remove":
        if not args.yes and ask(f"Delete \"{s.name}\"? [y/N] ").strip().lower() not in ("y", "yes"):
            out("Kept.")
            return 1
        store.delete(s.id)
        out(f"✓ Deleted \"{s.name}\".")
    elif args.action == "run":
        try:
            result = (post or _post(settings))(f"/api/schedules/{s.id}/run")
        except Exception:
            out("✗ Delta is not running. Start it (the Delta app, or `delta run`), then try again.")
            return 1
        out(("✓ " if result.get("ok") else "✗ ") + str(result.get("message", "")))
        return 0 if result.get("ok") else 1
    return 0


def _post(settings: Settings) -> Callable[[str], dict[str, Any]]:
    def post(path: str) -> dict[str, Any]:
        import httpx

        token_file = settings.data_dir / "api_token"
        token = token_file.read_text().strip()
        r = httpx.post(f"http://{settings.api.host}:{settings.api.port}{path}",
                       headers={"Authorization": f"Bearer {token}"}, timeout=10)
        r.raise_for_status()
        return dict(r.json())

    return post
