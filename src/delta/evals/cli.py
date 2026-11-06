"""`delta eval run`: the eval suite from the command line.

  delta eval run                         every task, scripted model (no keys, no network, no cost)
  delta eval run --suite files           one suite (files, documents, browser, apps, memory)
  delta eval run --task files-create-note
  delta eval run --model groq            a real provider from your settings (manual; it costs what it costs)
  delta eval list                        the tasks and their suites

Reports go to --out (default: <Delta data folder>/evals) as JSON and Markdown.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any


def add_parser(sub: Any) -> None:
    p = sub.add_parser("eval", help="run the eval suite (scripted by default: no keys, no cost)")
    p.add_argument("action", choices=["run", "list"])
    p.add_argument("--suite", action="append", help="files, documents, browser, apps or memory (repeatable)")
    p.add_argument("--task", action="append", help="one task id (repeatable)")
    p.add_argument("--model", default=None,
                   help="a provider from your settings (e.g. groq, claude_code). Omit for the scripted model")
    p.add_argument("--scripted", action="store_true", help="use the scripted model (the default)")
    p.add_argument("--specs", default=None, help="folder of task specs (default: evals/tasks)")
    p.add_argument("--out", default=None, help="folder for the JSON and Markdown reports")
    p.add_argument("--keep", action="store_true", help="keep each task's sandbox folder for inspection")
    p.add_argument("--min-success", type=float, default=None,
                   help="exit with an error when the success rate is below this (0-1)")


def run_cli(args: argparse.Namespace) -> int:
    from delta.evals.runner import SPECS_DIR, run_suite, write_report
    from delta.evals.spec import load_specs

    specs = Path(args.specs) if args.specs else SPECS_DIR
    if not specs.is_dir():
        print(f"no eval specs at {specs} (use --specs)")
        return 2
    tasks = load_specs(specs, args.suite, args.task)
    if args.action == "list":
        for t in tasks:
            needs = f"  (needs {', '.join(t.needs)})" if t.needs else ""
            print(f"{t.suite:<10} {t.id}{needs}")
        print(f"{len(tasks)} task(s)")
        return 0
    if not tasks:
        print("no tasks match")
        return 2
    model = None if args.scripted else args.model
    if args.out:
        out = Path(args.out)
    else:
        from delta.config import _data_dir

        out = _data_dir() / "evals"
    import logging

    from delta.system.logging import setup_logging

    setup_logging(out, console_level=logging.ERROR)  # Delta's own log goes to <out>/logs, not the screen
    print(f"Running {len(tasks)} eval task(s) with the {model or 'scripted'} model...")
    report = asyncio.run(run_suite(tasks, model, keep=args.keep))
    j, m = write_report(report, out)
    print(f"\n{report['passed']}/{report['ran']} passed ({report['success_rate'] * 100:.1f}%), "
          f"{report['skipped']} skipped, {report['seconds']:.1f}s, "
          f"{report['tokens_in'] + report['tokens_out']:,} tokens")
    print(f"Report: {m}\nJSON:   {j}")
    if args.min_success is not None and report["success_rate"] < args.min_success:
        return 1
    return 0
