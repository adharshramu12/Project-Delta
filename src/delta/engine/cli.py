"""`delta tasks`: see recent tasks, a task's steps, and resume or discard interrupted ones.

  delta tasks                        recent tasks (interrupted ones first)
  delta tasks list [--state S] [--limit N]
  delta tasks show <id>              the run timeline: each step with its result, model and tokens
  delta tasks resume <id> [--it-happened | --it-did-not-happen]
  delta tasks discard <id>
  delta tasks prune                  apply the retention limits now

Resume goes to the running Delta when there is one (so it shows in the dashboard); otherwise it runs
here, in this terminal, with the same approvals and the same permission level the task started with.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from typing import Any

from delta.config import Settings


def add_parser(sub: Any) -> None:
    p = sub.add_parser("tasks", help="recent tasks, their steps, resume or discard interrupted ones")
    p.add_argument("action", nargs="?", default="list", choices=["list", "show", "resume", "discard", "prune"])
    p.add_argument("id", nargs="?")
    p.add_argument("--state", default="", help="e.g. interrupted, done, failed (comma separated)")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--it-happened", dest="confirm", action="store_const", const="done",
                       help="the step that was cut off did happen: continue after it")
    group.add_argument("--it-did-not-happen", dest="confirm", action="store_const", const="not_done",
                       help="the step that was cut off did not happen: it may be done again")


def _when(ts: float | None) -> str:
    return time.strftime("%d %b %H:%M", time.localtime(ts)) if ts else ""


def _store(settings: Settings) -> Any:
    from delta.engine.store import TaskStore

    return TaskStore(settings.data_dir / "tasks.db", settings.engine.max_events_per_task)


def run_cli(settings: Settings, args: argparse.Namespace) -> int:
    if not settings.engine.enabled:
        print("The task engine is switched off (engine.enabled: false in settings).")
        return 1
    if args.action in ("show", "resume", "discard") and not args.id:
        print(f"task id required, e.g. `delta tasks {args.action} 1a2b3c4d5e`")
        return 2
    if args.action == "resume":
        return _resume(settings, args.id, args.confirm)
    from delta.engine.recorder import TaskEngine

    store = _store(settings)
    engine = TaskEngine(store, retention_days=settings.engine.retention_days, max_mb=settings.engine.max_mb)
    try:
        if args.action == "list":
            return _list(store, args)
        if args.action == "show":
            return _show(engine, args.id, args.json)
        if args.action == "prune":
            n = store.prune(settings.engine.retention_days, settings.engine.max_mb)
            print(f"removed {n} old task(s)")
            return 0
        if args.action == "discard":
            if _service_running(settings):
                return _api(settings, "POST", f"/api/tasks/{args.id}/discard")
            ok = engine.discard(args.id, by="cli")
            print("Discarded. Nothing more will run for it." if ok else "That task is not waiting to be resumed.")
            return 0 if ok else 1
    finally:
        store.close()
    return 2


def _list(store: Any, args: argparse.Namespace) -> int:
    states = [s for s in args.state.split(",") if s]
    rows = store.list(states, limit=args.limit)
    if not states:  # interrupted ones first: they are waiting for the user
        rows.sort(key=lambda t: t["state"] != "interrupted")
    if args.json:
        print(json.dumps(rows, indent=1, default=str))
        return 0
    if not rows:
        print("No tasks yet.")
        return 0
    for t in rows:
        tokens = t["tokens_in"] + t["tokens_out"]
        print(f"{t['id']}  {t['state']:<12} {_when(t['created']):<13} {t['channel']:<9} {t['steps']:>3} steps "
              f"{tokens:>8,} tok  {t['text'][:60]}")
    if any(t["state"] == "interrupted" for t in rows):
        print("\nInterrupted tasks wait for you: `delta tasks show <id>`, then `delta tasks resume <id>` or "
              "`delta tasks discard <id>`.")
    return 0


def _show(engine: Any, task_id: str, as_json: bool) -> int:
    data = engine.timeline(task_id)
    if data is None:
        print("unknown task")
        return 1
    if as_json:
        print(json.dumps(data, indent=1, default=str))
        return 0
    t = data["task"]
    print(f"{t['id']}  {t['state']}  {_when(t['created'])}  via {t['channel']}  {t['provider'] or 'no AI'}")
    print(f"request: {t['text']}")
    for s in data["steps"]:
        status = "STARTED, NEVER FINISHED" if s["unconfirmed"] else ("blocked" if not s["ran"] else
                                                                     ("ok" if s["ok"] else "failed"))
        took = f"{s['duration_ms'] / 1000:.1f}s" if s["duration_ms"] is not None else "?"
        tok = f" {s['tokens_in']}/{s['tokens_out']} tok" if s["tokens_in"] or s["tokens_out"] else ""
        print(f"  {s['step']:>3}. +{s['started'] - t['created']:6.1f}s {s['tool']:<18} {status:<8} {took:>6}"
              f"{tok} {s['model']}{'  [changes something]' if s['side_effect'] else ''}")
        if s["summary"]:
            print(f"       {s['summary'][:160]}")
    for c in data["checks"]:
        print(f"  check {'passed' if c['ok'] else 'failed'}: {c['summary']}")
    if t["answer"]:
        print(f"answer: {t['answer'][:500]}")
    return 0


def _service_running(settings: Settings) -> bool:
    import socket

    try:
        with socket.create_connection((settings.api.host, settings.api.port), timeout=0.3):
            return True
    except OSError:
        return False


def _api(settings: Settings, method: str, path: str, body: dict[str, Any] | None = None) -> int:
    import httpx

    from delta.api.server import load_token

    token = load_token(settings.data_dir)
    try:
        r = httpx.request(method, f"http://{settings.api.host}:{settings.api.port}{path}", json=body or {},
                          headers={"Authorization": f"Bearer {token}"}, timeout=15)
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        print(f"could not reach the running Delta: {e}")
        return 1
    if data.get("needs_confirmation"):
        print(data["message"])
        print("Run again with --it-happened or --it-did-not-happen.")
        return 3
    print(data.get("message") or ("ok" if data.get("ok") else "failed"))
    if data.get("ok") and "resume" in path:
        print("Resumed in the running Delta: follow it in the dashboard.")
    return 0 if data.get("ok") else 1


def _resume(settings: Settings, task_id: str, confirm: str | None) -> int:
    if _service_running(settings):
        return _api(settings, "POST", f"/api/tasks/{task_id}/resume", {"confirm_last": confirm} if confirm else {})
    return asyncio.run(_resume_here(settings, task_id, confirm))


async def _resume_here(settings: Settings, task_id: str, confirm: str | None) -> int:
    import logging

    from delta.bootstrap import build
    from delta.channels.cli import CliChannel
    from delta.engine.resume import prepare_resume
    from delta.system.lifecycle import AlreadyRunningError
    from delta.system.logging import setup_logging

    setup_logging(settings.data_dir, console_level=logging.ERROR)
    c = build(settings)
    try:
        await c.start()
    except AlreadyRunningError as e:
        print(e)
        return 1
    try:
        decision = prepare_resume(c, task_id, "cli", "owner", confirm)
        if decision.needs_confirmation:
            print(decision.message)
            print("Run again with --it-happened or --it-did-not-happen.")
            return 3
        if not decision.ok or decision.request is None:
            print(decision.message)
            return 1
        ui = CliChannel(c)  # approvals asked in this terminal (y / n), live steps shown
        c.approvals.add_presenter("cli", ui.present_approval)
        c.events.subscribe(ui.on_event)
        threading.Thread(target=ui._reader, args=(asyncio.get_running_loop(),), daemon=True).start()

        async def answer_approvals() -> None:
            while (line := await ui.lines.get()) is not None:
                pending, text = c.approvals.pending(), line.strip().lower()
                if pending and text in {"y", "yes", "n", "no"}:
                    c.approvals.resolve(pending[0].id, text.startswith("y"), by="cli")

        approver = asyncio.create_task(answer_approvals())
        try:
            res = await c.submit(decision.request, autonomy_cap=decision.autonomy_cap)
        finally:
            approver.cancel()
        print(f"\n{res.status}: {res.answer}")
        return 0 if res.status == "done" else 1
    finally:
        await c.stop()
