"""`delta undo`: put back the files a task changed (delta.selfheal.undo).

  delta undo                 tasks whose file changes can be undone
  delta undo <task id>       restore them (newest change first)

Goes to the running Delta when there is one (same as the dashboard button); otherwise restores here with
the same rules (current allowed folders, nothing deleted for good).
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

from delta.config import Settings


def add_parser(sub: Any) -> None:
    p = sub.add_parser("undo", help="put back the files a task changed")
    p.add_argument("id", nargs="?")
    p.add_argument("--json", action="store_true", help="machine-readable output")


def _store(settings: Settings) -> Any:
    from delta.bootstrap import make_policy
    from delta.selfheal.undo import UndoStore

    policy = make_policy(settings)
    return UndoStore(settings.data_dir / "undo", settings.undo, list(policy.allowed)), policy


def run_cli(settings: Settings, args: argparse.Namespace) -> int:
    if not settings.undo.enabled:
        print("Undo for file changes is switched off (undo.enabled: false in settings).")
        return 1
    store, policy = _store(settings)
    if not args.id:
        rows = []
        if store.root.is_dir():
            for folder in sorted(store.root.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True):
                info = store.summary(folder.name)
                if info["available"]:
                    rows.append({"task": folder.name, "files": info["files"],
                                 "when": time.strftime("%d %b %H:%M", time.localtime(folder.stat().st_mtime))})
        if args.json:
            print(json.dumps(rows, indent=1))
        elif not rows:
            print("No task has file changes to undo.")
        else:
            for r in rows:
                print(f"{r['task']}  {r['when']}  {r['files']} file change(s)   delta undo {r['task']}")
        return 0
    from delta.engine.cli import _service_running

    if _service_running(settings):
        return _api(settings, args.id)
    result = store.restore(args.id, policy)
    print(json.dumps(result, indent=1) if args.json else result["message"])
    for s in result["skipped"]:
        if not args.json:
            print(f"  not undone: {s['path']} ({s['why']})")
    return 0 if result["ok"] else 1


def _api(settings: Settings, task_id: str) -> int:
    import httpx

    from delta.api.server import load_token

    token = load_token(settings.data_dir)
    try:
        r = httpx.post(f"http://{settings.api.host}:{settings.api.port}/api/tasks/{task_id}/undo", json={},
                       headers={"Authorization": f"Bearer {token}"}, timeout=30)
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        print(f"could not reach the running Delta: {e}")
        return 1
    print(data.get("message") or ("ok" if data.get("ok") else "failed"))
    return 0 if data.get("ok") else 1
