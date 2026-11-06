"""`delta learn`: lessons and repaired recipe steps waiting to prove themselves (the promotion gate).

  delta learn                      everything learned on its own: candidates, in use, retired
  delta learn list [--status S] [--json]
  delta learn show <id>            one item's full history (L3 a lesson, R2 a repaired recipe)
  delta learn promote <id>         use it now (never allowed for items that came from outside content)
  delta learn reject <id>          never use it

Candidates are used only after they work in `learning.promote_after_runs` real runs or pass a scripted eval.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from delta.config import Settings


def add_parser(sub: Any) -> None:
    p = sub.add_parser("learn", help="lessons and repaired skill steps waiting to prove themselves")
    p.add_argument("action", nargs="?", default="list", choices=["list", "show", "promote", "reject"])
    p.add_argument("ref", nargs="?", help="L<id> (lesson) or R<id> (repaired recipe)")
    p.add_argument("--status", default="", choices=["", "candidate", "active", "rejected", "retired", "undone",
                                                    "superseded"])
    p.add_argument("--json", action="store_true", help="machine-readable output")


def _gate(settings: Settings) -> tuple[Any, Any, Any]:
    from delta.learning.gate import PromotionGate
    from delta.learning.store import LearningStore
    from delta.memory.skills import SkillStore

    learning = LearningStore(settings.data_dir / "learning.db")
    skills = SkillStore(settings.data_dir / "skills.db")
    return PromotionGate(settings.learning, learning, skills), learning, skills


def run_cli(settings: Settings, args: argparse.Namespace) -> int:
    gate, learning, skills = _gate(settings)
    try:
        return _run(gate, args)
    finally:
        learning.close()
        skills.close()


def _run(gate: Any, args: argparse.Namespace) -> int:
    from delta.learning.store import UntrustedPromotionError

    if args.action == "list":
        items = gate.items(args.status or None)
        if args.json:
            print(json.dumps(items, indent=1, default=str))
            return 0
        if not items:
            print("Nothing learned on its own yet. Lessons from recoveries and repaired skill steps show up here.")
            return 0
        for i in items:
            trust = "  [UNTRUSTED: never used]" if i["trust"] == "untrusted" else ""
            print(f"{i['ref']:<6} {i['kind']:<7} {i['status']:<10} {i['successes']} ok / {i['failures']} failed"
                  f"{' / eval passed' if i['evals_passed'] else ''}{trust}")
            print(f"       {i['summary'][:140]}")
        print(f"\nCandidates are used after {gate.cfg.promote_after_runs} successful real runs"
              f"{' or a passing scripted eval' if gate.cfg.promote_on_eval else ''}.")
        return 0
    if not args.ref:
        print(f"an id is required, e.g. `delta learn {args.action} R2`")
        return 2
    try:
        if args.action == "show":
            history = gate.history(args.ref)
            if args.json:
                print(json.dumps(history, indent=1, default=str))
                return 0
            if not history:
                print("no history for that id")
                return 1
            for h in reversed(history):
                task = f"  (task {h['task_id']})" if h["task_id"] else ""
                print(f"{h['ts'][:19]}  {h['event']:<16} {h['detail']}{task}")
            return 0
        change = gate.promote(args.ref, by="you") if args.action == "promote" else gate.reject(args.ref, by="you")
    except UntrustedPromotionError:
        print(f"{args.ref.upper()} came from outside content (a web page, file or message), so it can never become a "
              "rule or a recipe step. Reject it, or teach Delta the step yourself.")
        return 1
    except (KeyError, ValueError) as e:
        print(f"no such item: {e}")
        return 1
    print(change.summary)
    return 0
