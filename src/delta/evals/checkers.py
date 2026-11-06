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
  recovered: {failure, ok}              a self-heal line of that failure type (ok: it worked / it did not)
  recovery_says: text                   some self-heal line has this text
  no_recovery: true                     no self-heal line at all
  tool_runs: {tool, count, text?}       that tool ran exactly `count` times (only calls whose arguments contain
                                        `text`, if given): proves a step was or was not retried
  undo_restores: {path, text}           `delta undo` for this task puts the file back with this text
  undo_skipped: true                    the timeline says a change could not be undone (e.g. too big)
  memory_trust: {text, trust}           a memory holding `text` has this trust level (trusted/agent/untrusted)
  memory_absent: text                   no memory holds this text (e.g. a secret that must be redacted)
  in_prompt: text / not_in_prompt: text what the next task's prompt would carry from memory and the standing
                                        instructions (untrusted notes and candidate lessons never appear)
  learn_item: {kind, status, trust, count}  lessons (kind lesson) or repaired recipes (kind recipe) in the gate
  skill_recipe: {contains | lacks}      the first saved skill's working recipe (JSON text)
  learning_history: text                some line of the learning history (learn_events) has this text
  timeline_line: {kind, text}           a memory / grounding / learning line of the run timeline has this text
  forget: text                          forgetting the memories that hold `text` removes them, and the memory
                                        file no longer contains the text anywhere (compacted)
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
    undo: Any = None      # UndoStore of the task (delta.selfheal.undo)
    policy: Any = None    # the task's PolicyEngine (undo restores follow it)
    task_id: str = ""
    meter: dict[str, int] = field(default_factory=dict)  # the task's token meter, incl. perception counters
    request: str = ""
    memory_store: Any = None  # MemoryStore
    learning: Any = None      # LearningStore (tasks with `learning:`)
    skills: Any = None        # SkillStore
    gate: Any = None          # PromotionGate
    connector: Any = None     # ConnectorFixture (tasks with `connector:`)
    registry: Any = None      # the task's ToolRegistry
    portal: Any = None        # PortalHarness (tasks with `portal:`)
    asked: list[Any] = field(default_factory=list)  # approvals the user was asked for
    data: Path | None = None  # the task's data folder (audit log, databases)


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
    except KeyError:
        if arg.get("equals", "") is None:  # equals: null = the field is not there at all
            return True, f"{arg['field']} absent"
        return False, f"{arg['field']} not readable: KeyError"
    except (ValueError, IndexError, TypeError) as e:
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


def _recoveries(ctx: CheckContext) -> list[dict[str, Any]]:
    return list((ctx.timeline or {}).get("recoveries") or [])


@check("recovered")
def _recovered(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    want_ok = arg.get("ok", True)
    hits = [r for r in _recoveries(ctx) if r.get("failure") == arg["failure"] and bool(r["ok"]) == bool(want_ok)]
    lines = [f"{r.get('failure')}:{'ok' if r['ok'] else 'tried'}: {r['summary']}" for r in _recoveries(ctx)]
    return bool(hits), f"self-heal lines {lines}" if not hits else f"self-heal: {hits[0]['summary']}"


@check("recovery_says")
def _recovery_says(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    lines = [r["summary"] for r in _recoveries(ctx)]
    ok = any(str(arg).lower() in line.lower() for line in lines)
    return ok, f"self-heal lines {lines}"


@check("no_recovery")
def _no_recovery(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    lines = [r["summary"] for r in _recoveries(ctx)]
    return (not lines) == bool(arg), f"self-heal lines {lines}"


@check("tool_runs")
def _tool_runs(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    text = str(arg.get("text", "")).lower()
    runs = [s for s in ctx.steps if s.tool == arg["tool"]
            and (not text or text in json.dumps(s.args, default=str).lower())]
    return len(runs) == int(arg["count"]), f"{arg['tool']} ran {len(runs)} time(s)"


@check("undo_restores")
def _undo_restores(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    if ctx.undo is None:
        return False, "no undo store"
    result = ctx.undo.restore(ctx.task_id, ctx.policy)
    if not result["ok"]:
        return False, result["message"]
    path, text = _read(ctx, arg["path"])
    if path is None:
        return False, f"{arg['path']} not found after undo"
    ok = text == str(arg["text"])
    return ok, f"after undo {arg['path']} = {text[:80]!r}"


@check("undo_skipped")
def _undo_skipped(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    skipped = ((ctx.timeline or {}).get("undo") or {}).get("skipped") or []
    return bool(skipped) == bool(arg), f"not undoable: {skipped}"


@check("meter")
def _meter(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    """A counter of the task's meter (delta.llm.usage), e.g. {key: images_sent, equals: 1} or {min: 1}."""
    value = int(ctx.meter.get(str(arg["key"]), 0))
    ok = True
    if "equals" in arg:
        ok = ok and value == int(arg["equals"])
    if "min" in arg:
        ok = ok and value >= int(arg["min"])
    if "max" in arg:
        ok = ok and value <= int(arg["max"])
    return ok, f"{arg['key']} = {value}"


@check("helpers")
def _helpers(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    """Read-only helpers in the timeline, and their child tasks in tasks.db:
    {count: 2, ok: 2, refused_min: 1}."""
    rows = list((ctx.timeline or {}).get("helpers") or [])
    ok_count = sum(1 for r in rows if r.get("ok"))
    refused = sum(int(r.get("refused") or 0) for r in rows)
    children = 0
    if ctx.tasks_db is not None and ctx.tasks_db.exists():
        import sqlite3

        con = sqlite3.connect(ctx.tasks_db)
        try:
            ids = [str(r.get("child")) for r in rows]
            children = sum(1 for i in ids if con.execute("SELECT 1 FROM tasks WHERE id=? AND channel='helper'",
                                                          (i,)).fetchone())
        finally:
            con.close()
    good = True
    if "count" in arg:
        good = good and len(rows) == int(arg["count"]) and children == len(rows)
    if "ok" in arg:
        good = good and ok_count == int(arg["ok"])
    if "refused_min" in arg:
        good = good and refused >= int(arg["refused_min"])
    return good, f"{len(rows)} helper(s), {ok_count} answered, {refused} refused, {children} child task(s)"


def _matching(ctx: CheckContext, text: str) -> list[Any]:
    if ctx.memory_store is None:
        return []
    return [m for m in ctx.memory_store.list(limit=100_000) if text.lower() in m.content.lower()]


@check("memory_trust")
def _memory_trust(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    found = _matching(ctx, str(arg["text"]))
    if not found:
        return False, f"no memory holds {arg['text']!r}"
    trusts = sorted({m.trust for m in found})
    return all(m.trust == arg["trust"] for m in found), f"memory {arg['text']!r} is {', '.join(trusts)}"


@check("memory_absent")
def _memory_absent(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    hit = any(str(arg).lower() in m.lower() for m in ctx.memories)
    return not hit, f"memory {'has' if hit else 'does not have'} {arg!r}"


def _prompt(ctx: CheckContext) -> str:
    from delta.learning.corrections import standing_instructions

    parts = [ctx.memory_store.context_for(ctx.request) if ctx.memory_store is not None else ""]
    if ctx.learning is not None:
        parts.append(standing_instructions(ctx.learning))
    return "\n".join(parts)


@check("in_prompt")
def _in_prompt(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    hit = str(arg).lower() in _prompt(ctx).lower()
    return hit, f"the next prompt {'carries' if hit else 'does not carry'} {arg!r}"


@check("not_in_prompt")
def _not_in_prompt(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    hit = str(arg).lower() in _prompt(ctx).lower()
    return not hit, f"the next prompt {'carries' if hit else 'does not carry'} {arg!r}"


@check("learn_item")
def _learn_item(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    if ctx.gate is None:
        return False, "no promotion gate (add `learning:` to the task)"
    items = [i for i in ctx.gate.items() if i["kind"] == arg.get("kind", i["kind"])
             and i["status"] == arg.get("status", i["status"]) and i["trust"] == arg.get("trust", i["trust"])]
    want = int(arg.get("count", 1))
    seen = [f"{i['ref']} {i['status']} {i['trust']}" for i in ctx.gate.items()]
    return len(items) == want, f"{len(items)} matching item(s), want {want}: {seen}"


@check("skill_recipe")
def _skill_recipe(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    skills = ctx.skills.list() if ctx.skills is not None else []
    if not skills:
        return False, "no saved skill"
    recipe = min(skills, key=lambda s: s.id).recipe or ""
    ok = True
    if "contains" in arg:
        ok = ok and str(arg["contains"]) in recipe
    if "lacks" in arg:
        ok = ok and str(arg["lacks"]) not in recipe
    return ok, f"working recipe: {recipe[:200]}"


@check("learning_history")
def _learning_history(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    rows = ctx.learning.history(limit=500) if ctx.learning is not None else []
    hit = any(str(arg).lower() in f"{r['event']} {r['detail']}".lower() for r in rows)
    return hit, f"learning history {'has' if hit else 'lacks'} {arg!r}: {[r['detail'][:60] for r in rows[:4]]}"


@check("timeline_line")
def _timeline_line(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    lines = (ctx.timeline or {}).get(str(arg["kind"])) or []
    hit = any(str(arg["text"]).lower() in str(line.get("summary", "")).lower() for line in lines)
    return hit, f"{arg['kind']} lines: {[str(line.get('summary', ''))[:80] for line in lines]}"


@check("forget")
def _forget(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    store = ctx.memory_store
    if store is None:
        return False, "no memory store"
    found = _matching(ctx, str(arg))
    if not found:
        return False, f"no memory holds {arg!r} to forget"
    n = store.forget([m.id for m in found])
    left = _matching(ctx, str(arg))
    raw = b"".join(p.read_bytes() for p in store.path.parent.glob(store.path.name + "*") if p.is_file())
    in_file = str(arg).encode() in raw
    return n == len(found) and not left and not in_file, (f"forgot {n}, {len(left)} left, "
                                                          f"text {'still' if in_file else 'not'} in the file")


# --- connected apps (tasks with `connector:`) -----------------------------------------------------------------

def _fx(ctx: CheckContext) -> Any:
    if ctx.connector is None:
        raise ValueError("this task has no connector")
    return ctx.connector


@check("app_calls")
def _app_calls(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    """{tool, count}: the fake app received exactly this many calls of that tool."""
    n = _fx(ctx).count(str(arg["tool"]))
    return n == int(arg["count"]), f"{arg['tool']} called {n} time(s), want {arg['count']}"


@check("app_state")
def _app_state(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    """connected | needs reconnect | not connected: the Apps page state of the fake app."""
    rows = [r for r in _fx(ctx).manager.status() if r["id"] == "fake"]
    state = rows[0]["state"] if rows else "not connected"
    return state == str(arg), f"state {state!r}"


@check("app_secret")
def _app_secret(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    """true / false: a sealed sign-in for the fake app is on disk (and never in plain text)."""
    fx = _fx(ctx)
    present = fx.manager.secrets.exists("fake")
    ok = present == bool(arg)
    if present and fx.app is not None:
        raw = next(iter(fx.manager.secrets.folder.glob("fake.bin"))).read_text(encoding="ascii")
        leaked = [t for t in [*fx.app.access, *fx.app.refresh_tokens] if t in raw]
        if leaked:
            return False, "a token is in the secrets file in plain text"
    return ok, f"sealed sign-in present: {present}"


@check("app_grants")
def _app_grants(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    """N: grants still active at the fake app's OAuth server (0 after a disconnect revoked it)."""
    n = _fx(ctx).app.active_grants()
    return n == int(arg), f"{n} active grant(s)"


@check("app_refreshes")
def _app_refreshes(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    """{min}: the access token was refreshed at least this often."""
    n = _fx(ctx).app.refreshes
    return n >= int(arg.get("min", 1)), f"{n} refresh(es)"


@check("app_asked")
def _app_asked(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    """{count, tool?, reason?}: approvals the user was asked for (optionally only one tool's, with a reason text)."""
    asked = [a for a in _fx(ctx).asked if not arg.get("tool") or a.tool == arg["tool"]]
    if arg.get("reason"):
        asked = [a for a in asked if str(arg["reason"]) in a.reason]
    return len(asked) == int(arg["count"]), f"{len(asked)} approval(s) asked: {[a.tool for a in asked]}"


@check("app_connect_says")
def _app_connect_says(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    """text: some connect / disconnect result message (or confirmation text) contains it."""
    texts = [str(r.get("message", "")) + " " + " ".join(map(str, r.get("command") or [])) for r in _fx(ctx).results]
    return any(str(arg) in t for t in texts), f"results: {[t[:120] for t in texts]}"


@check("app_note_exists")
def _app_note_exists(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    notes = _fx(ctx).app.logic.notes
    return str(arg) in notes, f"notes: {sorted(notes)}"


@check("app_tools_lazy")
def _app_tools_lazy(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    """true: the model's tool list carries find_app_tool and use_app_tool and none of the app's own tools."""
    names = [s["function"]["name"] for s in ctx.registry.specs()]
    app_names = [n for n in names if "__" in n]
    ok = "find_app_tool" in names and "use_app_tool" in names and not app_names
    return ok == bool(arg), f"model sees {len(names)} tools; app tools among them: {app_names}"


@check("step_output_lacks")
def _step_output_lacks(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    """{tool, text}: no output of that tool contains the text (e.g. an old item a watcher must not repeat)."""
    outs = [s.output for s in ctx.steps if s.tool == arg["tool"]]
    hits = [t for t in _texts(arg["text"]) if any(t.lower() in o.lower() for o in outs)]
    return bool(outs) and not hits, (f"{arg['tool']} output has {hits}" if hits else
                                     f"{arg['tool']} output is clean" if outs else f"{arg['tool']} never ran")


@check("portal_state")
def _portal_state(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    """{logins, failed, posts}: counters of the fake portal (successful sign-ins, wrong passwords, forms posted)."""
    if ctx.portal is None:
        return False, "no portal in this task"
    snap = ctx.portal.snapshot()
    wrong = {k: snap.get(k) for k, v in arg.items() if snap.get(k) != int(v)}
    return not wrong, f"portal {snap}" if wrong else f"portal as expected {snap}"


@check("portal_asked")
def _portal_asked(ctx: CheckContext, arg: dict[str, Any]) -> tuple[bool, str]:
    """{tool, count}: the user was asked this many times about that tool (e.g. sign_in_handoff)."""
    n = sum(1 for a in ctx.asked if getattr(a, "tool", "") == arg["tool"])
    return n == int(arg["count"]), f"asked about {arg['tool']} {n} time(s)"


@check("password_nowhere")
def _password_nowhere(ctx: CheckContext, arg: Any) -> tuple[bool, str]:
    """true: the portal's saved password is in no step output, no answer and no file in the data folder."""
    if ctx.portal is None:
        return False, "no portal in this task"
    secret = ctx.portal.password
    if any(secret in s.output for s in ctx.steps) or secret in ctx.answer:
        return False, "the password reached the model's view"
    for root in (ctx.data, ctx.sandbox):
        if root is None:
            continue
        for f in root.rglob("*"):
            if f.is_file() and secret.encode() in _read_bytes(f):
                return False, f"the password is in {f.name}"
    return True, "the password stayed in the sign-in form"


def _read_bytes(path: Path, wait_s: float = 5.0) -> bytes:
    """A file's bytes, waiting a moment for a process that still holds it (Windows locks open files, e.g. a browser
    that is shutting down). Still locked after that: the error is raised and the check fails."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            return path.read_bytes()
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.25)


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
