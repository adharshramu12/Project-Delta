"""Deterministic checks for eval tasks. Each one reads real state after the task ran (files in the sandbox,
the fixture site's recorded page state, Delta's memory, the task's steps and its event log) and returns
(passed, detail). No model is ever asked whether a task worked.

  status: done                          the task's final status
  file_exists: notes.txt | {path, wait_s}   relative paths are inside the sandbox; globs allowed
  file_absent: old.txt
  file_contains: {path, text}           (text may be a list: all must appear)
  file_equals: {path, text}
  json_field: {path, field: a.b, equals}
  answer_contains: text | [any of these]
  answer_not_contains: text
  page_state: {key: value}              what the local site recorded (form fields as submit.<name>)
  memory_contains: text                 a memory Delta saved
  steps_include: [tool, ...]            these tools ran (in any order)
  steps_ok: true                        every tool step succeeded
  max_steps: 3
  step_output_contains: {tool, text}
  any_output_contains: text             some step's output (auto-observed page reads included)
  step_failed_with: {tool, text}        that tool ran and failed with this text (e.g. a watchdog stop)
  no_process: marker                    no running process has this text in its command line
  events_recorded: {min_steps, absent}  tasks.db holds the steps; `absent` (a secret) is not in the file
"""

from __future__ import annotations

import glob
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CheckContext:
    sandbox: Path
    status: str
    answer: str
    steps: list[Any]  # StepRecord
    page_state: dict[str, str] = field(default_factory=dict)
    memories: list[str] = field(default_factory=list)
    tasks_db: Path | None = None
    timeline: dict[str, Any] | None = None


Check = Callable[[CheckContext, Any], tuple[bool, str]]
CHECKS: dict[str, Check] = {}


def check(name: str) -> Callable[[Check], Check]:
    def add(fn: Check) -> Check:
        CHECKS[name] = fn
        return fn
    return add


def _paths(ctx: CheckContext, pattern: str) -> list[Path]:
    p = Path(pattern)
    full = p if p.is_absolute() else ctx.sandbox / p
    if any(ch in str(full) for ch in "*?["):
        return [Path(x) for x in sorted(glob.glob(str(full), recursive=True))]
    return [full] if full.exists() else []


def _read(ctx: CheckContext, pattern: str) -> tuple[Path | None, str]:
    found = [p for p in _paths(ctx, pattern) if p.is_file()]
    if not found:
        return None, ""
    return found[0], found[0].read_text(encoding="utf-8", errors="replace")


def _texts(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


@check("status")
def _status(ctx: CheckContext, want: Any) -> tuple[bool, str]:
    return ctx.status == str(want), f"status {ctx.status}"


@check("file_exists")
def _exists(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    path, wait = (arg["path"], float(arg.get("wait_s", 0))) if isinstance(arg, dict) else (arg, 0.0)
    deadline = time.monotonic() + wait
    while True:
        if _paths(ctx, str(path)):
            return True, f"{path} exists"
        if time.monotonic() >= deadline:
            return False, f"{path} not found"
        time.sleep(0.2)


@check("file_absent")
def _absent(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    return not _paths(ctx, str(arg)), f"{arg} {'still exists' if _paths(ctx, str(arg)) else 'is gone'}"


@check("file_contains")
def _contains(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    path, text = _read(ctx, str(arg["path"]))
    if path is None:
        return False, f"{arg['path']} not found"
    missing = [t for t in _texts(arg["text"]) if t.lower() not in text.lower()]
    return not missing, f"{path.name}: missing {missing}" if missing else f"{path.name} has the text"


@check("file_equals")
def _equals(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    path, text = _read(ctx, str(arg["path"]))
    if path is None:
        return False, f"{arg['path']} not found"
    ok = text.strip() == str(arg["text"]).strip()
    return ok, f"{path.name} {'matches' if ok else 'differs: ' + text[:80]!r}"


@check("json_field")
def _json_field(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    path, text = _read(ctx, str(arg["path"]))
    if path is None:
        return False, f"{arg['path']} not found"
    try:
        value: Any = json.loads(text)
        for key in str(arg["field"]).split("."):
            value = value[int(key)] if isinstance(value, list) else value[key]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        return False, f"{arg['field']} not readable: {type(e).__name__}"
    return value == arg["equals"], f"{arg['field']} = {value!r}"


@check("answer_contains")
def _answer(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    hits = [t for t in _texts(arg) if t.lower() in ctx.answer.lower()]
    return bool(hits), f"answer has {hits[0]!r}" if hits else f"answer lacks {_texts(arg)}: {ctx.answer[:120]!r}"


@check("answer_not_contains")
def _not_answer(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    hits = [t for t in _texts(arg) if t.lower() in ctx.answer.lower()]
    return not hits, f"answer has {hits}" if hits else "answer is clean"


@check("page_state")
def _page(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    wrong = {k: ctx.page_state.get(k) for k, v in arg.items() if ctx.page_state.get(k) != str(v)}
    return not wrong, f"page state {ctx.page_state}" if wrong else "page state as expected"


@check("memory_contains")
def _memory(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    hit = any(str(arg).lower() in m.lower() for m in ctx.memories)
    return hit, f"memory {'has' if hit else 'lacks'} {arg!r}"


@check("steps_include")
def _steps_include(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    ran = {s.tool for s in ctx.steps}
    missing = [t for t in _texts(arg) if t not in ran]
    return not missing, f"missing steps {missing}" if missing else "steps present"


@check("steps_ok")
def _steps_ok(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    bad = [s.tool for s in ctx.steps if not s.ok]
    return (not bad) == bool(arg), f"failed steps {bad}" if bad else "all steps ok"


@check("max_steps")
def _max_steps(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    return len(ctx.steps) <= int(arg), f"{len(ctx.steps)} steps"


@check("step_output_contains")
def _step_output(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    outs = [s.output for s in ctx.steps if s.tool == arg["tool"]]
    ok = any(str(arg["text"]).lower() in o.lower() for o in outs)
    return ok, f"{arg['tool']} output {'has' if ok else 'lacks'} {arg['text']!r}"


@check("any_output_contains")
def _any_output(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    ok = any(str(arg).lower() in s.output.lower() for s in ctx.steps)
    return ok, f"some step output {'has' if ok else 'lacks'} {arg!r}"


@check("step_failed_with")
def _step_failed(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    outs = [s.output for s in ctx.steps if s.tool == arg["tool"] and not s.ok]
    ok = any(str(arg["text"]).lower() in o.lower() for o in outs)
    return ok, f"{arg['tool']} {'failed as expected' if ok else 'did not fail with ' + repr(arg['text'])}"


@check("no_process")
def _no_process(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    import psutil

    marker = str(arg)
    deadline = time.monotonic() + 5
    while True:
        left = []
        for p in psutil.process_iter(["cmdline"]):
            try:
                if marker in " ".join(p.info["cmdline"] or []):
                    left.append(p.pid)
            except psutil.Error:
                continue
        if not left or time.monotonic() > deadline:
            break
        time.sleep(0.3)
    return not left, f"processes still running: {left}" if left else "no leftover processes"


@check("events_recorded")
def _events(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    if ctx.timeline is None or ctx.tasks_db is None:
        return False, "no task engine timeline"
    steps = ctx.timeline.get("steps", [])
    if len(steps) < int(arg.get("min_steps", 1)):
        return False, f"{len(steps)} step(s) recorded"
    secret = arg.get("absent")
    if secret:
        raw = b"".join(p.read_bytes() for p in ctx.tasks_db.parent.glob(ctx.tasks_db.name + "*") if p.is_file())
        if str(secret).encode() in raw:
            return False, "a secret reached tasks.db"
    return True, f"{len(steps)} step(s) recorded, final state {ctx.timeline['task']['state']}"


def run_checks(ctx: CheckContext, checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for item in checks:
        (name, arg), = item.items()
        fn = CHECKS.get(name)
        if fn is None:
            out.append({"check": name, "passed": False, "detail": "unknown check"})
            continue
        try:
            ok, detail = fn(ctx, arg)
        except Exception as e:  # a broken check fails the task, never the run
            ok, detail = False, f"check error: {type(e).__name__}: {e}"
        out.append({"check": name, "passed": bool(ok), "detail": detail})
    return out
