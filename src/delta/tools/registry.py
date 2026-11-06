"""ToolRegistry + ToolExecutor: the single gate every tool call passes through.

validate args -> policy decision -> (approval) -> run -> audit. Nothing runs any other way.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import replace
from typing import Any

from pydantic import AliasChoices, BaseModel, ValidationError

from delta.core.observations import ObservationLog
from delta.core.state import CURRENT_REQUEST
from delta.learning.store import TRUSTABLE_RISKS, Learned, LearningStore, is_folder_scope, split_key
from delta.safety.approvals import ApprovalBroker, ApprovalRequest
from delta.safety.audit import AuditLog
from delta.safety.exfil_guard import (
    AUTOPILOT,
    DESKTOP_INPUT,
    SEARCH_TOOLS,
    URL_TOOLS,
    ExfilGuard,
    GuardAsk,
    host_of,
    input_classes,
    site_of,
)
from delta.safety.policy import MESSAGING_TOOLS, Decision, PolicyEngine, Verdict, _help_request, _under, norm_path
from delta.safety.remote_guard import PIN_TO_APPROVE_RISKS, is_remote, needs_remote_pin, tool_refusal
from delta.system.logging import log
from delta.tools.base import Risk, Tool, ToolResult


def accepted_names(model: type[BaseModel]) -> set[str]:
    """Every key a tool's Args accepts: field names plus their validation aliases."""
    names: set[str] = set()
    for field_name, info in model.model_fields.items():
        names.add(field_name)
        alias = info.validation_alias
        if isinstance(alias, str):
            names.add(alias)
        elif isinstance(alias, AliasChoices):
            names.update(c for c in alias.choices if isinstance(c, str))
        if info.alias:
            names.add(info.alias)
    return names


# Names models commonly use for a tool's real argument. Renamed only when the tool has that field and
# it was not given: certain, unlike a fuzzy guess. Saves a whole AI round trip ("did you mean...").
SYNONYMS: dict[str, tuple[str, ...]] = {
    "text": ("content", "message"), "body": ("content", "message"), "data": ("content",),
    "file": ("path",), "filepath": ("path",), "file_path": ("path",), "filename": ("path",),
    "folder": ("path",), "directory": ("path",), "dir": ("path",),
    "link": ("url",), "href": ("url",), "q": ("query",), "search": ("query",), "keywords": ("query",),
    "cmd": ("command",), "app": ("name",), "application": ("name",),
}


def fix_synonyms(model: type[BaseModel], raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Rename unknown keys that are sure synonyms of the tool's fields: (args, ["text->content", ...])."""
    if not isinstance(raw, dict) or not model.model_fields:
        return raw, []
    accepted, fixed, out = accepted_names(model), [], dict(raw)
    for key in list(raw):
        if key in accepted:
            continue
        targets = [t for t in SYNONYMS.get(key.lower(), ()) if t in model.model_fields and t not in out]
        if len(targets) >= 1:
            out[targets[0]] = out.pop(key)
            fixed.append(f"{key}->{targets[0]}")
    return out, fixed


def unknown_arguments(model: type[BaseModel], raw: dict[str, Any]) -> str:
    """'' when every key is known, else a fix-it message ('attendees' -> did you mean 'guests'?).
    Models open to arbitrary keys (extra='allow' or no declared fields) are not checked."""
    if not isinstance(raw, dict) or model.model_config.get("extra") == "allow" or not model.model_fields:
        return ""
    accepted = accepted_names(model)
    unknown = [k for k in raw if k not in accepted]
    if not unknown:
        return ""
    hints = []
    for key in unknown:
        close = difflib.get_close_matches(key, sorted(accepted), n=1, cutoff=0.5)
        hints.append(f"'{key}' (did you mean '{close[0]}'?)" if close else f"'{key}'")
    return (f"unknown argument {', '.join(hints)}. Valid arguments: {', '.join(sorted(model.model_fields))}. "
            "Call the tool again with the right names.")


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.register(t)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        """Remove a tool (a connected app was disconnected or its tool list changed)."""
        return self._tools.pop(name, None) is not None

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> list[dict[str, Any]]:
        """Specs of the tools usable right now; unusable ones are not offered to the model."""
        out = []
        for t in self._tools.values():
            try:
                if t.available():
                    out.append(t.spec())
            except Exception:  # a broken availability check must not hide every other tool
                continue
        return out

    def names(self) -> list[str]:
        return list(self._tools)

    def planner_names(self) -> list[str]:
        """Names the planner is shown: hidden tools (each connected app's own tools, reached through
        find_app_tool / use_app_tool) are left out, so a big app does not cost planner tokens."""
        return [n for n, t in self._tools.items() if getattr(t, "hidden", False) is not True]


# Prompt-injection containment, enforced in code rather than by asking the model nicely. Once a task
# has read content that someone else wrote (a web page, search results, a file, a chat message, the
# screen), that content may be steering the model. From then on, actions that send, delete, run code
# or change Delta's memory need a human yes for the rest of the task, whatever the autonomy level
# or learned trust says. In-page browser steps (click, fill, press) stay free: they only act on the
# page already open, which is how Deep Research and form filling work.
UNTRUSTED_SOURCES = {"browser_read", "fetch_page", "web_search", "read_messages", "read_file",
                     "read_window", "look_at_screen", "look_with_marks", "read_screen_text", "helper_read_page",
                     "parallel_research"}
GUARDED_RISKS = {Risk.EXTERNAL, Risk.DESTRUCTIVE}
ALWAYS_GUARDED = {"run_shell", "run_python", "remember", "send_file", "send_message", "chat_for_me"}
IN_PAGE = {"browser_click", "browser_fill", "browser_press"}


def needs_taint_check(tool_name: str, risk: str, autonomy: str = "") -> bool:
    if tool_name in IN_PAGE:
        return False
    if tool_name in DESKTOP_INPUT and risk == Risk.WRITE:
        # Enter or Alt+F4 in a desktop app can submit or close things. Autopilot lets that through in
        # ordinary apps; terminals, the Run box and Delta's own window still ask (ExfilGuard), and an Enter
        # that sends in a chat app is EXTERNAL, which stays guarded below.
        return autonomy != AUTOPILOT
    return tool_name in ALWAYS_GUARDED or risk in GUARDED_RISKS


# "Allow for this task" never covers these: they run arbitrary code or change Delta's memory.
NEVER_FOR_TASK = {"run_shell", "run_python", "remember"}
# ...nor anything that cannot be undone or is hard-blocked anyway.
NEVER_FOR_TASK_RISKS = {Risk.DESTRUCTIVE, Risk.SYSTEM}
# Alias tools (click_mark, click_text) name an element the agent saw; the executor resolves them into the
# ordinary call that acts on it, which then passes every check below as if the model had asked for it.
_RESOLVING: ContextVar[bool] = ContextVar("delta_tool_resolving", default=False)
# The step number of the call being executed, so its end event pairs with its start (task engine).
_STEP: ContextVar[dict[str, Any] | None] = ContextVar("delta_tool_step", default=None)


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, policy: PolicyEngine, approvals: ApprovalBroker,
                 audit: AuditLog, learning: LearningStore | None = None,
                 on_learned: Callable[[Learned], Awaitable[None]] | None = None) -> None:
        self.registry = registry
        self.policy = policy
        self.approvals = approvals
        self.audit = audit
        self.learning = learning
        self.on_learned = on_learned
        # task_id -> things the user refused to have changed during that task (normalised paths/targets)
        self._refused: dict[str, set[str]] = {}
        # task_id -> the first tool that brought outside content into the task (see UNTRUSTED_SOURCES)
        self._tainted: dict[str, str] = {}
        self.guard: Any = None  # the Container's RemoteGuard, set at start-up (see _remote_refusal)
        self.exfil = ExfilGuard()  # read-then-exfiltrate via URLs, input into terminals / Delta itself
        # task_id -> "tool|risk|target class" the user allowed for the rest of that task. Memory only:
        # never saved, dropped in forget_task, so a new task asks again.
        self._task_grants: dict[str, set[str]] = {}
        # Durable task engine (delta.engine), set at start-up; None keeps the executor exactly as before.
        self.recorder: Any = None  # TaskEngine: each step's start and end go to the task's event log
        self.watchdog: Any = None  # Watchdog: time limit per call, ends the processes a stopped call started
        self.undo: Any = None  # UndoStore (delta.selfheal.undo): a copy of each file about to be changed
        # FaultInjector (delta.selfheal.inject): TESTS AND SCRIPTED EVALS ONLY, never set from settings.
        self.faults: Any = None
        # Full results of every call that ran, per task (memory only): answer grounding and provenance.
        self.observations = ObservationLog()

    @staticmethod
    def action_key(tool: Tool, args: BaseModel, risk: str) -> str:
        """Groups similar actions for learning: tool + risk + kind of target (folder/app/site/command)."""
        return f"{tool.name}|{risk}|{tool.scope(args)}"

    @staticmethod
    def _targets(tool: Tool, args: BaseModel) -> set[str]:
        """What a call would change: declared paths, apps and domains, normalised for comparison."""
        found = {str(norm_path(p)) for p in tool.paths(args) if p}
        found |= {d.lower() for d in tool.domains(args)}
        if app := tool.app(args):
            found.add(app.lower())
        return found

    def _already_refused(self, task_id: str, tool: Tool, args: BaseModel) -> str | None:
        """A 'no' covers every way of doing it: a delete refused via delete_path must not be retried
        through run_shell, a GUI click or an MCP tool. Matches declared targets and any argument text."""
        refused = self._refused.get(task_id)
        if not refused:
            return None
        text = json.dumps(args.model_dump(), default=str).lower().replace("\\\\", "\\").replace("/", "\\")
        for target in refused:
            t = target.lower().replace("/", "\\")
            if t in self._targets(tool, args) or (len(t) > 3 and t in text):
                return target
        return None

    def _learned_match(self, kind: str, key: str, tool: Tool, args: BaseModel, risk: str) -> Learned | None:
        """Find an active learned rule for this call.

        Exact key first. Folder-scoped rules then apply across tools, by kind of change:
          avoid: same risk, and the folder appears in the call's targets or arguments, so a refused
                 delete cannot come back as `Remove-Item`, a move, or an MCP tool.
          trust: same risk, every declared path inside the folder, and never for the shell (a cwd
                 inside a trusted folder says nothing about what a command does).
        """
        assert self.learning
        if exact := self.learning.is_active(kind, key):
            return exact
        paths = [str(norm_path(p)) for p in tool.paths(args) if p]
        text = json.dumps(args.model_dump(), default=str).lower().replace("\\\\", "\\").replace("/", "\\")
        for item in self.learning.list(status="active", kind=kind):
            _, item_risk, scope = split_key(item.key)
            if item_risk != risk or not is_folder_scope(scope):
                continue
            folder = scope.lower().rstrip("\\")
            if kind == "avoid" and (any(p.startswith(folder) for p in paths) or folder in text):
                return item
            if (kind == "trust" and tool.name != "run_shell" and paths
                    and all(p == folder or p.startswith(folder + "\\") for p in paths)):
                return item
        return None

    def taint(self, task_id: str, source: str) -> None:
        """Mark a task as having read outside content (e.g. results merged back from read-only helpers)."""
        if task_id and task_id not in self._tainted:
            self._tainted[task_id] = source
            self.audit.record("taint", task=task_id, source=source)

    def tainted_by(self, task_id: str) -> str | None:
        return self._tainted.get(task_id) if task_id else None

    def forget_task(self, task_id: str) -> None:
        self._refused.pop(task_id, None)
        self._tainted.pop(task_id, None)
        self._task_grants.pop(task_id, None)
        self.observations.forget(task_id)
        self.exfil.forget(task_id)
        if self.watchdog is not None:
            self.watchdog.forget(task_id)

    @staticmethod
    def target_class(tool: Tool, args: BaseModel) -> str:
        """The kind of target "Allow for this task" covers: the app a desktop input lands on, the site an
        address is on, else the tool's scope (folder, site or app). '' when it cannot be told."""
        name = tool.name
        if name in DESKTOP_INPUT and getattr(tool, "input_targets", None) is not None:
            apps = input_classes(tool, args)
            return "app:" + ",".join(apps) if apps else ""
        if name in URL_TOOLS:
            raw = str(getattr(args, "target" if name == "open_target" else "url", "") or "")
            if name != "open_target" or "://" in raw:
                host = host_of(raw)
                return "site:" + site_of(host) if host else ""
        if name in SEARCH_TOOLS:
            return ""
        scope = tool.scope(args)
        return "scope:" + scope if scope else ""

    def task_grant_key(self, tool: Tool, args: BaseModel, decision: Decision, task_id: str,
                       guard: GuardAsk | None) -> str:
        """The key a "Allow for this task" answer would cover for this call, or '' when it may not be
        offered: permanent deletes, code and memory tools, terminal / Delta-window / unchecked input,
        addresses or searches carrying data, always-confirmed actions, and phone requests whose approval
        needs the PIN."""
        if not task_id or decision.risk in NEVER_FOR_TASK_RISKS or tool.name in NEVER_FOR_TASK:
            return ""
        if guard is not None and guard.strict:
            return ""
        if (getattr(tool, "always_ask", False) or getattr(tool, "approve_each", False)
                or (tool.name in MESSAGING_TOOLS and _help_request())):
            return ""
        req = CURRENT_REQUEST.get()
        if req is not None and is_remote(req.channel) and (
                str(decision.risk) in PIN_TO_APPROVE_RISKS or needs_remote_pin(tool.name)):
            return ""
        cls = self.target_class(tool, args)
        return f"{tool.name}|{decision.risk}|{cls}" if cls else ""

    async def execute(self, name: str, raw_args: dict[str, Any], task_id: str = "") -> ToolResult:
        """Run one tool call through every check (below). With the task engine on, the call is also
        recorded: a start event right before the tool really runs, an end event with its result."""
        recorder = self.recorder if task_id else None
        if recorder is None:
            return await self._execute(name, raw_args, task_id)
        mark: dict[str, Any] = {}
        token = _STEP.set(mark)
        started = time.monotonic()
        result: ToolResult | None = None
        try:
            result = await self._execute(name, raw_args, task_id)
            return result
        finally:
            _STEP.reset(token)
            recorder.step_finished(task_id, mark.get("step"), name, raw_args, result, time.monotonic() - started,
                                   self.registry.get(name))

    async def _execute(self, name: str, raw_args: dict[str, Any], task_id: str = "") -> ToolResult:
        tool = self.registry.get(name)
        if tool is None:
            return ToolResult(False, f"unknown tool '{name}'. Available: {', '.join(self.registry.names())}")
        raw_args, fixed = fix_synonyms(tool.Args, raw_args)
        if fixed:
            self.audit.record("tool.args_fixed", task=task_id, tool=name, renamed=fixed)
        if problem := unknown_arguments(tool.Args, raw_args):
            # Pydantic ignores unknown keys: a guest list sent as "attendees" would simply vanish.
            self.audit.record("tool.bad_args", task=task_id, tool=name, problem=problem)
            return ToolResult(False, f"invalid arguments for {name}: {problem}")
        try:
            args: BaseModel = tool.Args.model_validate(raw_args)
        except ValidationError as e:
            return ToolResult(False, f"invalid arguments for {name}: {e.errors(include_url=False)}")
        if getattr(tool, "is_alias", False) is True:
            return await self._resolve_alias(tool, args, name, task_id)

        decision = self.policy.evaluate(tool, args)
        autonomy = str(self.policy.b.autonomy)
        source = self._tainted.get(task_id) if task_id else None
        tainted = (bool(source) and decision.verdict != Verdict.DENY
                   and needs_taint_check(name, decision.risk, autonomy))
        if tainted:
            decision = replace(decision, verdict=Verdict.ASK,
                               reason=f"this task read outside content ({source}), which could be steering me; "
                                      "please confirm")
        guard: GuardAsk | None = None
        if decision.verdict != Verdict.DENY and (guard := self.exfil.check(
                tool, args, task_id, source, self.policy.b.allowed_domains, autonomy)):
            decision, tainted = replace(decision, verdict=Verdict.ASK, reason=guard.reason), True  # never learned trust
        if (decision.verdict != Verdict.DENY and task_id and self.recorder is not None
                and (repeat := self.recorder.repeat_reason(task_id, name, args.model_dump()))):
            # A resumed task about to repeat a side effect that already happened before the interruption:
            # a human yes every time (no learned trust, no "Allow for this task", whatever the autonomy level).
            decision, tainted = replace(decision, verdict=Verdict.ASK, reason=repeat), True
            guard = guard or GuardAsk(repeat, "unknown_target")  # a strict kind: never "Allow for this task"
        summary = tool.summary(args)
        self.audit.record("policy", task=task_id, tool=name, summary=summary,
                          verdict=decision.verdict, risk=decision.risk, reason=decision.reason)

        if decision.verdict != Verdict.DENY and (refusal := self._remote_refusal(tool, args)):
            return ToolResult(False, f"BLOCKED: {refusal} Do not try another way; tell the user exactly this.",
                              {"needs_pin": True})
        if decision.verdict == Verdict.DENY:
            return ToolResult(False, f"BLOCKED by policy: {decision.reason}. Do not retry this action; "
                                     "find another way or tell the user.")
        if decision.verdict == Verdict.ASK:
            if task_id and (target := self._already_refused(task_id, tool, args)):
                self.audit.record("approval", task=task_id, tool=name, approved=False, by="earlier refusal",
                                  target=target)
                return ToolResult(False, f"BLOCKED: the user already refused changes to {target} in this task. "
                                         "Do not try another way. Stop and tell the user what you could not do.")
            key = self.action_key(tool, args, decision.risk)
            if self.learning and (avoid := self._learned_match("avoid", key, tool, args, decision.risk)):
                self.audit.record("approval", task=task_id, tool=name, approved=False, by="learned avoid",
                                  learned=avoid.id)
                # Treated exactly like a fresh "no": the task stops, no workarounds.
                return ToolResult(False, "BLOCKED: the user has refused this kind of action before.",
                                  {"user_denied": True,
                                   "denied_action": f"{summary} (you said no to this before; undo it in "
                                                    f"Delta's learned list if you changed your mind)"})
            grant = self.task_grant_key(tool, args, decision, task_id, guard)
            if grant and grant in self._task_grants.get(task_id, set()):
                self.audit.record("approval", task=task_id, tool=name, approved=True, by="allowed for this task",
                                  target=grant.split("|", 2)[2])
            elif (self.learning and not tainted and decision.risk in TRUSTABLE_RISKS
                    and not getattr(tool, "approve_each", False)  # never learned: each email gets its own yes
                    and (trust := self._learned_match("trust", key, tool, args, decision.risk))):
                self.audit.record("approval", task=task_id, tool=name, approved=True, by="learned trust",
                                  learned=trust.id)
            else:
                outcome = await self.approvals.request(ApprovalRequest(
                    tool=name, summary=summary, risk=decision.risk, reason=decision.reason, task_id=task_id,
                    for_task=bool(grant), can_edit=bool(getattr(tool, "can_edit", False))))
                self.audit.record("approval", task=task_id, tool=name, approved=outcome.approved, by=outcome.by,
                                  for_task=bool(outcome.for_task and grant), edit=outcome.edit is not None)
                if outcome.edit is not None:  # "Edit": nothing ran; the agent redrafts and asks again (no learning)
                    changes = outcome.edit or "(no details: ask the user what to change)"
                    return ToolResult(False, f"The user chose EDIT, so nothing was sent. Their changes: {changes}. "
                                             "Redraft with these changes, then ask for approval again. If the "
                                             "changes are unclear, ask the user.", {"edit": changes})
                if outcome.approved and outcome.for_task and grant:
                    self._task_grants.setdefault(task_id, set()).add(grant)
                if self.learning and outcome.by != "timeout" and outcome.human:  # only real human answers teach
                    learned = self.learning.record_decision(key, decision.risk, summary, outcome.approved)
                    if learned:
                        self.audit.record("learned", task=task_id, kind=learned.kind, item=learned.id,
                                          content=learned.content)
                        if self.on_learned:
                            await self.on_learned(learned)
                if outcome.approved:
                    return await self._run(tool, args, name, task_id)
                if task_id:
                    self._refused.setdefault(task_id, set()).update(self._targets(tool, args) or {summary})
                return ToolResult(False, f"The user DENIED this action ({outcome.by}). Do not retry it or try "
                                         "another way to do the same thing; tell the user.",
                                  {"user_denied": outcome.by != "timeout", "denied_action": summary})

        return await self._run(tool, args, name, task_id)

    async def _resolve_alias(self, tool: Tool, args: BaseModel, name: str, task_id: str) -> ToolResult:
        """click_mark / click_text: find the element, then run the ordinary call that acts on it through every
        check (policy, approvals, Remote Guard, ExfilGuard, taint, repeat check). The alias itself can only be
        refused here (denied tool, remote lock); it can never make the real call less checked."""
        decision = self.policy.evaluate(tool, args)
        if decision.verdict == Verdict.DENY:
            return ToolResult(False, f"BLOCKED by policy: {decision.reason}. Do not retry this action; "
                                     "find another way or tell the user.")
        if refusal := self._remote_refusal(tool, args):
            return ToolResult(False, f"BLOCKED: {refusal} Do not try another way; tell the user exactly this.",
                              {"needs_pin": True})
        if _RESOLVING.get():
            return ToolResult(False, f"{name} cannot resolve to another alias tool")
        token = _RESOLVING.set(True)
        try:
            try:
                target = await tool.resolve_target(args)  # type: ignore[attr-defined]
            except Exception as e:
                log.exception("tool.resolve_failed", tool=name)
                return ToolResult(False, f"{name}: could not find the element ({type(e).__name__}: {e})")
            if isinstance(target, str):
                return ToolResult(False, target)
            real, real_args = target
            real_tool = self.registry.get(real)
            if real_tool is None or getattr(real_tool, "is_alias", False) is True:
                return ToolResult(False, f"{name}: '{real}' is not available on this PC")
            self.audit.record("tool.resolved", task=task_id, tool=name, to=real, args=real_args)
            result = await self._execute(real, real_args, task_id)
        finally:
            _RESOLVING.reset(token)
        return ToolResult(result.ok, result.output, {**result.evidence, "resolved_to": real, "via": name})

    def _remote_refusal(self, tool: Tool, args: BaseModel) -> str | None:
        """Remote Guard, for every tool: a request from a phone channel needs the PIN for sensitive reads
        and nothing runs while remote control is locked (delta.safety.remote_guard.tool_refusal)."""
        req = CURRENT_REQUEST.get()
        if self.guard is None or req is None:
            return None
        outside = any(not _under(norm_path(p), self.policy.allowed) for p in tool.paths(args) if p)
        probe = getattr(tool, "remote_pin", None)  # a tool can ask for the PIN on some calls (set_browser)
        extra = probe(args) if callable(probe) else None
        return tool_refusal(self.guard, req.channel, req.user, tool.name, outside, extra)

    async def _run(self, tool: Tool, args: BaseModel, name: str, task_id: str) -> ToolResult:
        step: int | None = None
        if self.recorder is not None and task_id and (mark := _STEP.get()) is not None:
            step = mark["step"] = self.recorder.step_started(task_id, name, args.model_dump(), tool)
        # Undo: copy what is about to change (files inside the allowed folders only). Never fails the step.
        kept: list[str] = []
        if self.undo is not None and task_id:
            kept = await asyncio.to_thread(self.undo.before, task_id, step, name, args.model_dump())
        injected = self.faults.take(name) if self.faults is not None else None
        try:
            if injected is not None:
                result = injected
            else:
                result = await (self.watchdog.run(tool, args, task_id) if self.watchdog is not None
                                else tool.run(args))
        except Exception as e:
            log.exception("tool.crashed", tool=name)
            result = ToolResult(False, f"{type(e).__name__}: {e}")
        if kept:
            await asyncio.to_thread(self.undo.after, task_id, kept, result.ok)
        self.audit.record("tool", task=task_id, tool=name, ok=result.ok, evidence=result.evidence)
        self.exfil.observe(task_id, name, args, result)
        untrusted = name in UNTRUSTED_SOURCES or getattr(tool, "untrusted_output", False) is True  # e.g. MCP
        if task_id:
            self.observations.add(task_id, name, result.ok, args.model_dump(), result.output, result.evidence,
                                  untrusted, str(getattr(tool, "risk", "")) == Risk.READ)
        if task_id and result.ok and untrusted and task_id not in self._tainted:
            self._tainted[task_id] = name
            self.audit.record("taint", task=task_id, source=name)
        return result
