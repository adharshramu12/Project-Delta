"""`delta memory`: see what Delta remembers, where each item came from, and forget it.

  delta memory                          the newest memories with where they came from
  delta memory list [--trust T] [--kind K] [--limit N] [--json]
  delta memory show <id>                one memory in full, with its provenance
  delta memory forget <id> [<id> ...]   delete these
  delta memory forget --task <task id>  delete everything a task saved
  delta memory forget --untrusted       delete every note that came from outside content
  delta memory forget --all --yes       delete everything (asks for --yes)
  delta memory prune                    apply the size and retention limits now

Works on this PC's memory file directly (the running Delta sees the change at once). Deleting also compacts
the file, so the deleted text does not stay behind in it.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from delta.config import Settings


def add_parser(sub: Any) -> None:
    p = sub.add_parser("memory", help="what Delta remembers about you, where it came from, forget it")
    p.add_argument("action", nargs="?", default="list", choices=["list", "show", "forget", "prune"])
    p.add_argument("ids", nargs="*", help="memory ids (show, forget)")
    p.add_argument("--trust", default="", choices=["", "trusted", "agent", "untrusted"])
    p.add_argument("--kind", default="", choices=["", "profile", "episode", "note"])
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--task", default="", help="forget: everything saved by this task")
    p.add_argument("--untrusted", action="store_true", help="forget: every note from outside content")
    p.add_argument("--all", action="store_true", help="forget: everything (needs --yes)")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--json", action="store_true", help="machine-readable output")


def _store(settings: Settings) -> Any:
    from delta.memory.store import MemoryStore

    return MemoryStore.from_config(settings.data_dir / "memory.db", settings.memory)


def run_cli(settings: Settings, args: argparse.Namespace) -> int:
    store = _store(settings)
    try:
        return _run(store, args)
    finally:
        store.close()


def _ids(raw: list[str]) -> list[int] | None:
    try:
        return [int(x.lstrip("#")) for x in raw]
    except ValueError:
        return None


def _run(store: Any, args: argparse.Namespace) -> int:
    if args.action == "list":
        items = store.list(args.kind or None, limit=max(1, args.limit), trust=args.trust or None)
        if args.json:
            print(json.dumps([m.to_dict() for m in items], indent=1, default=str))
            return 0
        if not items:
            print("Delta does not remember anything" + (" like that." if args.trust or args.kind else " yet."))
            return 0
        for m in items:
            tag = " [UNTRUSTED: data only]" if m.untrusted else ""
            print(f"#{m.id:<5} {m.kind:<8}{tag} {m.content[:110]}")
            print(f"        from {m.provenance()}")
        stats = store.stats()
        print(f"\n{stats.get('total', 0)} memories ({stats.get('trust:untrusted', 0)} untrusted). "
              "`delta memory forget <id>` deletes one.")
        return 0
    if args.action == "show":
        ids = _ids(args.ids)
        if not ids:
            print("memory id required, e.g. `delta memory show 12`")
            return 2
        m = store.get(ids[0])
        if m is None:
            print("no such memory")
            return 1
        if args.json:
            print(json.dumps(m.to_dict(), indent=1, default=str))
            return 0
        print(f"#{m.id} {m.kind}\n{m.content}\n")
        print(f"from:     {m.provenance()}")
        print(f"trust:    {m.trust}" + ("  (kept as data only; never used as a fact about you or an instruction)"
                                        if m.untrusted else ""))
        print(f"task:     {m.task_id or '-'}\nsaved:    {m.created}\nupdated:  {m.updated}\nrecalled: {m.hits} time(s)")
        return 0
    if args.action == "prune":
        print(f"removed {store.prune()} memory item(s) past the limits")
        return 0
    # forget
    if args.all:
        if not args.yes:
            print("This deletes everything Delta remembers. Run again with --yes to confirm.")
            return 2
        n = len(store.list(limit=1_000_000))
        store.clear()
        print(f"forgot {n} memory item(s)")
        return 0
    ids = _ids(args.ids)
    if ids is None:
        print("memory ids are numbers, e.g. `delta memory forget 12 13`")
        return 2
    if not ids and not args.task and not args.untrusted:
        print("say what to forget: ids, --task <task id>, --untrusted or --all --yes")
        return 2
    n = 0
    if ids:
        n += store.forget(ids)
    if args.task:
        n += store.forget(task_id=args.task)
    if args.untrusted:
        n += store.forget(trust="untrusted")
    print(f"forgot {n} memory item(s)")
    return 0 if n else 1
