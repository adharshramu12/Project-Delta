"""TaskEngine: what the orchestrator and the tool executor tell tasks.db as a task runs, and what a
resumed task is told about the steps that already ran.

Every method is safe to call from the agent loop: a full disk or a locked file is logged, never raised,
so recording can never fail a task. A state change that is not an allowed transition is a bug; it is
logged and recorded as a note, and the task itself carries on.

Side effects. A step is side-effecting when its tool can change something: any risk other than "read"
(writes, deletes, sends, purchases, clicks that submit) and every shell or code tool, whatever the command
says. Resume never re-runs one: the resumed task is told which steps already happened, and the executor
asks the user before any call identical to one of them (`repeat_reason`). A side-effecting step that was
started but never finished (Delta stopped in the middle) is "unconfirmed": resume asks the user whether
it happened before anything else runs.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from delta.engine.redact import redact_args, redact_text, redact_value
from delta.engine.states import RESULT_STATE, RUNNING, IllegalTransitionError, TaskState
from delta.engine.store import TaskStore
from delta.selfheal.taxonomy import classify
from delta.system.logging import log

S = TaskState
READ_RISK = "read"
# Shell, code and messaging tools always count as side effects, even when a command looks read-only.
ALWAYS_SIDE_EFFECT = frozenset({"run_shell", "run_python", "send_message", "send_file", "chat_for_me",
                                "alert_emergency_contacts", "remember", "delete_path", "write_file", "move_path",
                                "edit_file", "make_dir", "make_document", "prepare_post", "schedule_task",
                                "delete_schedule", "pause_schedule", "resume_schedule"})
SUMMARY_CHARS = 300
TEXT_CHARS = 4000
LINE_KINDS = ("memory", "grounding", "learning")  # Batch D timeline lines


def provider_label(result: Any) -> str:
    """Which AI ran the task, as the task list shows it: "groq · openai/gpt-oss-120b", or the provider alone."""
    provider = str(getattr(result, "provider", "") or "")
    model = str(getattr(result, "model", "") or "")
    return f"{provider} · {model}" if provider and model and model != "default" else provider


def fingerprint(tool: str, raw_args: Any) -> str:
    """Same tool with the same arguments = same fingerprint. Only the hash is stored, never the args."""
    try:
        body = json.dumps(raw_args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        body = repr(raw_args)
    return hashlib.sha256(f"{tool}\n{body}".encode()).hexdigest()[:20]


def risk_of(tool: Any, raw_args: Any) -> str:
    """The tool's own risk for these arguments; '' when it cannot be told (unknown tool, bad args)."""
    if tool is None:
        return ""
    try:
        args = tool.Args.model_validate(raw_args if isinstance(raw_args, dict) else {})
        return str(tool.risk_for(args))
    except Exception:
        return str(getattr(tool, "risk", "") or "")


def is_side_effect(name: str, risk: str) -> bool:
    if name in ALWAYS_SIDE_EFFECT:
        return True
    if risk:
        return risk != READ_RISK
    # unknown risk: assume it changed something unless the name says it only looks
    return not name.startswith(("read_", "list_", "look_", "browser_read", "path_info", "recall", "media_status",
                                "search_", "web_search", "fetch_page"))


@dataclass
class StepView:
    step: int
    tool: str
    args: str
    risk: str
    side_effect: bool
    fp: str
    started: float
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    finished: float | None = None
    ok: bool | None = None
    summary: str = ""
    duration_ms: int | None = None
    screenshot: str = ""
    ran: bool = True  # False: blocked or denied before the tool ran

    def to_dict(self) -> dict[str, Any]:
        return {"step": self.step, "tool": self.tool, "args": self.args, "risk": self.risk,
                "side_effect": self.side_effect, "started": self.started, "finished": self.finished,
                "ok": self.ok, "summary": self.summary, "model": self.model, "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out, "duration_ms": self.duration_ms, "screenshot": self.screenshot,
                "ran": self.ran, "unconfirmed": self.finished is None and self.ran}


def steps_of(events: list[dict[str, Any]]) -> list[StepView]:
    """The task's steps, start and end events joined, in order."""
    out: dict[int, StepView] = {}
    for e in events:
        n = e.get("step")
        if n is None:
            continue
        if e["kind"] == "step_start":
            out[n] = StepView(n, e["tool"], e["args"], e["risk"], bool(e["side_effect"]), e["fp"], e["ts"],
                              e["model"], e["tokens_in"], e["tokens_out"])
        elif e["kind"] in ("step_end", "step_blocked"):
            view = out.get(n)
            if view is None:
                view = out[n] = StepView(n, e["tool"], e["args"], e["risk"], bool(e["side_effect"]), e["fp"],
                                         e["ts"], ran=e["kind"] == "step_end")
            view.finished = e["ts"]
            view.ok = bool(e["ok"]) if e["ok"] is not None else None
            view.summary = e["summary"]
            view.duration_ms = e["duration_ms"]
            view.screenshot = e["screenshot"]
            if e["kind"] == "step_blocked":
                view.ran = False
    return [out[k] for k in sorted(out)]


@dataclass
class ResumePlan:
    task: dict[str, Any]
    steps: list[StepView]
    unconfirmed: StepView | None  # a side-effecting step that started and never finished
    context: str
    done_fps: set[str] = field(default_factory=set)


class TaskEngine:
    def __init__(self, store: TaskStore, router: Any = None, proof: Any = None, registry: Any = None,
                 retention_days: float = 30, max_mb: float = 50) -> None:
        self.store = store
        self.router = router
        self.proof = proof
        self.registry = registry
        self.retention_days = retention_days
        self.max_mb = max_mb
        self._meter: dict[str, tuple[int, int]] = {}  # task -> tokens counted so far (in, out)
        self._shots: dict[str, set[str]] = {}  # task -> screenshots already attributed to a step
        self._repeat: dict[str, set[str]] = {}  # task -> fingerprints of side effects done before a resume
        self.undo: Any = None  # UndoStore (delta.selfheal.undo), set at start-up: undo availability per task

    # --- helpers --------------------------------------------------------------------------------
    def _safe(self, what: str, fn: Any, *args: Any, **kw: Any) -> Any:
        try:
            return fn(*args, **kw)
        except IllegalTransitionError as e:
            log.error("engine.illegal_transition", error=str(e))
            try:
                self.store.append(e.task_id, "note", summary=f"refused state change {e.current} -> {e.new}")
            except Exception:
                log.exception("engine.note_failed")
        except Exception:
            log.exception("engine.failed", what=what)
        return None

    def _tokens(self, task_id: str) -> tuple[int, int]:
        """Tokens used since the last step of this task (the model call that chose this step)."""
        meter = getattr(self.router, "meter", None) or {}
        total_in = int(meter.get("input", 0)) + int(meter.get("est_input", 0))
        total_out = int(meter.get("output", 0)) + int(meter.get("est_output", 0))
        before_in, before_out = self._meter.get(task_id, (0, 0))
        if total_in < before_in or total_out < before_out:  # meter was reset for this task
            before_in = before_out = 0
        self._meter[task_id] = (total_in, total_out)
        return total_in - before_in, total_out - before_out

    def _model(self) -> str:
        name = str(getattr(self.router, "last_provider", "") or "")
        if not name:
            return ""
        provider = (getattr(self.router, "providers", None) or {}).get(name)
        model = ""
        cfg = getattr(provider, "cfg", None)
        models = getattr(cfg, "models", None)
        if isinstance(models, dict):
            model = str(models.get("chat") or "")
        return f"{name}/{model}" if model else name

    def _new_screenshot(self, task_id: str) -> str:
        if self.proof is None:
            return ""
        try:
            files = list(self.proof.files(task_id))
        except Exception:
            return ""
        seen = self._shots.setdefault(task_id, set())
        fresh = [f for f in files if f not in seen]
        seen.update(fresh)
        return fresh[-1] if fresh else ""

    def _move(self, task_id: str, new: TaskState, reason: str = "", **fields: Any) -> None:
        task = self.store.get(task_id)
        if task is None or task["state"] == new:
            return
        self.store.transition(task_id, new, reason, **fields)

    # --- called by the orchestrator ------------------------------------------------------------
    def begin(self, req: Any, autonomy: str = "") -> None:
        self._safe("begin", self._begin, req, autonomy)

    def _begin(self, req: Any, autonomy: str) -> None:
        task = self.store.get(req.id)
        if task is None:
            self.store.create(req.id, S.QUEUED, text=redact_text(req.text, TEXT_CHARS), channel=req.channel,
                              user=str(req.user), chat_id=req.chat_id, autonomy=autonomy)
        elif task["state"] == S.WAITING_USER:
            self.store.transition(req.id, S.PLANNING, "the user answered")
            return
        self._meter.pop(req.id, None)
        self._move(req.id, S.PLANNING)

    def verifying(self, task_id: str) -> None:
        self._safe("verifying", self._move, task_id, S.VERIFYING)

    def finish(self, task_id: str, result: Any, req: Any = None) -> None:
        self._safe("finish", self._finish, task_id, result, req)

    def _finish(self, task_id: str, result: Any, req: Any) -> None:
        if self.store.get(task_id) is None:  # failed before begin() (e.g. the proof folder could not be made)
            if req is None:
                return
            self.store.create(task_id, S.QUEUED, text=redact_text(req.text, TEXT_CHARS), channel=req.channel,
                              user=str(req.user), chat_id=req.chat_id)
        new = RESULT_STATE.get(str(result.status), S.FAILED)
        usage = getattr(result, "usage", None) or {}
        self._move(task_id, new, "", answer=redact_text(str(result.answer or ""), TEXT_CHARS),
                   provider=provider_label(result), tokens_in=int(usage.get("input", 0) or 0),
                   tokens_out=int(usage.get("output", 0) or 0), calls=int(usage.get("calls", 0) or 0))
        verification = getattr(result, "verification", None)
        if verification is not None:
            self.store.append(task_id, "verify", ok=int(bool(verification.passed)),
                              summary=redact_text(str(verification.reason), SUMMARY_CHARS),
                              data=json.dumps({"checks": redact_value(verification.checks, 200)},
                                              default=str)[:4000])
        self._meter.pop(task_id, None)
        self._shots.pop(task_id, None)
        self._repeat.pop(task_id, None)

    # --- called by the tool executor -----------------------------------------------------------
    def step_started(self, task_id: str, name: str, raw_args: Any, tool: Any = None) -> int | None:
        step = self._safe("step_started", self._step_started, task_id, name, raw_args, tool)
        return int(step) if step is not None else None

    def _step_started(self, task_id: str, name: str, raw_args: Any, tool: Any) -> int | None:
        task = self.store.get(task_id)
        if task is None:
            return None
        if task["state"] in (S.QUEUED, S.PLANNING, S.VERIFYING, S.WAITING_USER):
            self._move(task_id, S.ACTING)
        risk = risk_of(tool, raw_args)
        step = self.store.next_step(task_id)
        tokens_in, tokens_out = self._tokens(task_id)
        self.store.append(task_id, "step_start", step=step, tool=name, args=redact_args(raw_args), risk=risk,
                          side_effect=int(is_side_effect(name, risk)), fp=fingerprint(name, raw_args),
                          model=self._model(), tokens_in=tokens_in, tokens_out=tokens_out)
        self.store.update(task_id, steps=step)
        return step

    def step_finished(self, task_id: str, step: int | None, name: str, raw_args: Any, result: Any,
                      duration_s: float, tool: Any = None) -> None:
        self._safe("step_finished", self._step_finished, task_id, step, name, raw_args, result, duration_s, tool)

    def _step_finished(self, task_id: str, step: int | None, name: str, raw_args: Any, result: Any,
                       duration_s: float, tool: Any) -> None:
        if self.store.get(task_id) is None:
            return
        ok = bool(getattr(result, "ok", False)) if result is not None else False
        output = str(getattr(result, "output", "") or "") if result is not None else "stopped before it finished"
        evidence = getattr(result, "evidence", None) or {}
        keep = {k: evidence[k] for k in ("verified", "user_denied", "exit_code", "url", "title", "watchdog",
                                         "killed", "needs_pin", "recovery") if k in evidence}
        if not ok:  # the failure type (delta.selfheal.taxonomy): what went wrong, for the log and for self-heal
            keep["failure"] = str(classify(name, output, evidence))
        fields: dict[str, Any] = {"tool": name, "ok": int(ok), "summary": redact_text(output, SUMMARY_CHARS),
                                  "duration_ms": int(duration_s * 1000),
                                  "data": json.dumps(redact_value(keep, 200), default=str)[:2000]}
        if step is None:  # refused, denied or invalid before the tool ran: one event, no start
            risk = risk_of(tool, raw_args)
            step = self.store.next_step(task_id)
            fields.update(step=step, args=redact_args(raw_args), risk=risk,
                          side_effect=int(is_side_effect(name, risk)), fp=fingerprint(name, raw_args))
            self.store.append(task_id, "step_blocked", **fields)
            self.store.update(task_id, steps=step)
            return
        fields.update(step=step, screenshot=self._new_screenshot(task_id))
        self.store.append(task_id, "step_end", **fields)

    def repeat_reason(self, task_id: str, name: str, raw_args: Any) -> str | None:
        """Why a call must be confirmed: it is identical to a side effect that already happened before
        this task was interrupted. None for every ordinary task."""
        done = self._repeat.get(task_id)
        if not done or fingerprint(name, raw_args) not in done:
            return None
        return ("this exact step already ran before Delta was interrupted; running it again could do it twice "
                "(send twice, write twice). Please confirm")

    def recovery(self, task_id: str, failure: str, line: str, ok: bool, tool: str = "",
                 data: dict[str, Any] | None = None) -> None:
        """One self-heal line (delta.selfheal): what failed, what was tried, in plain English."""
        self._safe("recovery", self._recovery, task_id, failure, line, ok, tool, data or {})

    def _recovery(self, task_id: str, failure: str, line: str, ok: bool, tool: str, data: dict[str, Any]) -> None:
        if self.store.get(task_id) is None:
            return
        self.store.append(task_id, "recovery", tool=tool, ok=int(ok), summary=redact_text(line, SUMMARY_CHARS),
                          data=json.dumps(redact_value({"failure": failure, **data}, 200), default=str)[:2000])

    def line(self, task_id: str, kind: str, summary: str, ok: bool | None = None,
             data: dict[str, Any] | None = None) -> None:
        """One plain-English timeline line of kind memory, grounding or learning (Batch D)."""
        self._safe("line", self._line, task_id, kind, summary, ok, data or {})

    def _line(self, task_id: str, kind: str, summary: str, ok: bool | None, data: dict[str, Any]) -> None:
        if kind not in LINE_KINDS or self.store.get(task_id) is None:
            return
        self.store.append(task_id, kind, ok=None if ok is None else int(ok),
                          summary=redact_text(summary, SUMMARY_CHARS),
                          data=json.dumps(redact_value(data, 200), default=str)[:2000])

    def failure(self, task_id: str, failure: str, summary: str) -> None:
        """A failure outside a tool step (a model call), recorded with its type."""
        self._safe("failure", self._failure, task_id, failure, summary)

    def _failure(self, task_id: str, failure: str, summary: str) -> None:
        if self.store.get(task_id) is None:
            return
        self.store.append(task_id, "failure", ok=0, summary=redact_text(summary, SUMMARY_CHARS),
                          data=json.dumps({"failure": failure}))

    def helper(self, parent_id: str, child_id: str, data: dict[str, Any]) -> None:
        """A read-only helper (delta.helpers) that ran for this task: a child task row (already final, so a
        crash can never leave one to resume) with one event per source it read, and a line in the parent's
        log that the timeline shows."""
        self._safe("helper", self._helper, parent_id, child_id, data)

    def _helper(self, parent_id: str, child_id: str, data: dict[str, Any]) -> None:
        if self.store.get(parent_id) is None:
            return
        ok = bool(data.get("ok"))
        question = redact_text(str(data.get("question") or ""), SUMMARY_CHARS)
        if self.store.get(child_id) is None:
            self.store.create(child_id, TaskState.DONE if ok else TaskState.FAILED, text=question, channel="helper",
                              note=f"read-only helper of task {parent_id}",
                              answer=redact_text(str(data.get("answer") or data.get("error") or ""), 2000),
                              tokens_in=int(data.get("tokens") or 0), calls=int(data.get("calls") or 0),
                              steps=int(data.get("steps") or 0), finished=time.time())
            for source in data.get("sources") or []:
                name, _, what = str(source).partition(" ")
                self.store.append(child_id, "helper_read", tool=name, ok=1, risk="read",
                                  summary=redact_text(what, SUMMARY_CHARS))
        line = f"Helper {data.get('index', '')}: {question}" + ("" if ok else f" (not answered: {data.get('error')})")
        keep = {k: data.get(k) for k in ("index", "tokens", "calls", "steps", "seconds", "refused", "sources")}
        self.store.append(parent_id, "helper", tool="parallel_research", ok=int(ok), risk="read",
                          summary=redact_text(line, SUMMARY_CHARS),
                          data=json.dumps(redact_value({"child": child_id, **keep}, 200), default=str)[:2000])

    # --- start-up, resume, discard -------------------------------------------------------------
    def recover(self) -> list[dict[str, Any]]:
        """At start-up: tasks left running by a crash or power cut become interrupted. Also prunes."""
        found: list[dict[str, Any]] = []
        try:
            for task in self.store.running():
                self.store.transition(task["id"], S.INTERRUPTED, "Delta stopped while this task was running")
                found.append(task)
            if found:
                log.warning("engine.interrupted_tasks", tasks=[t["id"] for t in found])
        except Exception:
            log.exception("engine.recover_failed")
        self._safe("prune", self.store.prune, self.retention_days, self.max_mb)
        return found

    def interrupted(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            return self.store.list(states=[S.INTERRUPTED], limit=limit)
        except Exception:
            log.exception("engine.list_failed")
            return []

    def plan_resume(self, task_id: str) -> ResumePlan | None:
        task = self.store.get(task_id)
        if task is None:
            return None
        steps = steps_of(self.store.events(task_id))
        unconfirmed = next((s for s in reversed(steps) if s.ran and s.finished is None and s.side_effect), None)
        done = {s.fp for s in steps if s.side_effect and s.ran and s.finished is not None and s.ok}
        return ResumePlan(task, steps, unconfirmed, self._context(task, steps, unconfirmed), done)

    @staticmethod
    def _context(task: dict[str, Any], steps: list[StepView], unconfirmed: StepView | None) -> str:
        lines = ["THIS TASK WAS INTERRUPTED (Delta or the PC stopped) and the user asked to resume it.",
                 f"Original request: {task['text']}",
                 "Steps that already ran before the interruption, oldest first:"]
        for s in steps[-40:]:
            if not s.ran:
                state = "was refused or blocked"
            elif s.finished is None:
                state = "STARTED BUT NEVER FINISHED"
            else:
                state = "ok" if s.ok else "failed"
            mark = " [ALREADY DONE: changed something, do NOT repeat]" if (s.side_effect and s.ok and s.ran) else ""
            lines.append(f"{s.step}. {s.tool} {s.args[:200]} -> {state}: {s.summary[:160]}{mark}")
        if len(steps) > 40:
            lines.insert(3, f"(only the last 40 of {len(steps)} steps are listed)")
        if not steps:
            lines.append("(no steps had run yet)")
        if unconfirmed is not None:
            lines.append(f"Step {unconfirmed.step} ({unconfirmed.tool}) was started but Delta stopped before it "
                         "finished.")
        lines.append("Continue from where it stopped. First check the current state with read-only tools; never "
                     "repeat a step marked ALREADY DONE.")
        return "\n".join(lines)

    def start_resume(self, task_id: str, plan: ResumePlan, last_happened: bool | None) -> None:
        """interrupted -> queued, and arm the repeat check for this task's earlier side effects.
        last_happened: the user's answer about an unconfirmed step (True = it happened)."""
        done = set(plan.done_fps)
        if plan.unconfirmed is not None and last_happened:
            done.add(plan.unconfirmed.fp)
        note = ""
        if plan.unconfirmed is not None:
            note = (f"user says step {plan.unconfirmed.step} ({plan.unconfirmed.tool}) "
                    f"{'happened' if last_happened else 'did not happen'}")
        self.store.transition(task_id, S.QUEUED, "resumed by the user" + (f"; {note}" if note else ""),
                              resumes=int(plan.task.get("resumes", 0)) + 1)
        self._repeat[task_id] = done

    def discard(self, task_id: str, by: str = "") -> bool:
        task = self.store.get(task_id)
        if task is None or task["state"] != S.INTERRUPTED:
            return False
        self.store.transition(task_id, S.CANCELLED, f"discarded by {by or 'the user'}")
        return True

    def timeline(self, task_id: str) -> dict[str, Any] | None:
        task = self.store.get(task_id)
        if task is None:
            return None
        events = self.store.events(task_id)
        states = [{"ts": e["ts"], "summary": e["summary"]} for e in events if e["kind"] == "state"]
        checks = [{"ts": e["ts"], "ok": bool(e["ok"]), "summary": e["summary"]} for e in events
                  if e["kind"] == "verify"]
        notes = [{"ts": e["ts"], "summary": e["summary"]} for e in events if e["kind"] == "note"]
        recoveries = [{"ts": e["ts"], "ok": bool(e["ok"]), "summary": e["summary"], "tool": e["tool"],
                       **_data(e, "failure", "rung")} for e in events if e["kind"] == "recovery"]
        failures = [{"ts": e["ts"], "summary": e["summary"], **_data(e, "failure")} for e in events
                    if e["kind"] == "failure"]
        helpers = [{"ts": e["ts"], "ok": bool(e["ok"]), "summary": e["summary"],
                    **_data(e, "child", "tokens", "seconds", "sources", "refused")}
                   for e in events if e["kind"] == "helper"]
        lines = {k: [{"ts": e["ts"], "ok": None if e["ok"] is None else bool(e["ok"]), "summary": e["summary"],
                      **_data(e, "item", "event", "claims", "unverified", "contradicted", "hits", "untrusted")}
                     for e in events if e["kind"] == k] for k in LINE_KINDS}
        undo = self._undo_summary(task_id)
        steps = []
        for s in steps_of(events):
            view = s.to_dict()
            view["undo"] = s.step in undo.get("steps", [])
            steps.append(view)
        failed = {e["step"]: _data(e, "failure", "recovery") for e in events if e["kind"] == "step_end" and not e["ok"]}
        for view in steps:
            view.update(failed.get(view["step"], {}))
        return {"task": task, "steps": steps, "states": states, "checks": checks, "notes": notes,
                "recoveries": recoveries, "failures": failures, "undo": undo, "helpers": helpers,
                "memory": lines["memory"], "grounding": lines["grounding"], "learning": lines["learning"],
                "running": task["state"] in {str(s) for s in RUNNING}, "now": time.time()}

    def _undo_summary(self, task_id: str) -> dict[str, Any]:
        if self.undo is None:
            return {"available": False, "files": 0, "restored": False, "skipped": [], "steps": []}
        try:
            return dict(self.undo.summary(task_id))
        except Exception:
            log.exception("engine.undo_summary_failed")
            return {"available": False, "files": 0, "restored": False, "skipped": [], "steps": []}


def _data(event: dict[str, Any], *keys: str) -> dict[str, Any]:
    try:
        raw = json.loads(event.get("data") or "{}")
    except ValueError:
        return {}
    return {k: raw[k] for k in keys if isinstance(raw, dict) and k in raw}
