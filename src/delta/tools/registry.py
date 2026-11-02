"""ToolRegistry + ToolExecutor: the single gate every tool call passes through.

validate args -> policy decision -> (approval) -> run -> audit. Nothing runs any other way.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from delta.learning.store import TRUSTABLE_RISKS, Learned, LearningStore, is_folder_scope, split_key
from delta.safety.approvals import ApprovalBroker, ApprovalRequest
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine, Verdict, norm_path
from delta.system.logging import log
from delta.tools.base import Tool, ToolResult


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.register(t)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self._tools[tool.name] = tool

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

    def forget_task(self, task_id: str) -> None:
        self._refused.pop(task_id, None)

    async def execute(self, name: str, raw_args: dict[str, Any], task_id: str = "") -> ToolResult:
        tool = self.registry.get(name)
        if tool is None:
            return ToolResult(False, f"unknown tool '{name}'. Available: {', '.join(self.registry.names())}")
        try:
            args: BaseModel = tool.Args.model_validate(raw_args)
        except ValidationError as e:
            return ToolResult(False, f"invalid arguments for {name}: {e.errors(include_url=False)}")

        decision = self.policy.evaluate(tool, args)
        summary = tool.summary(args)
        self.audit.record("policy", task=task_id, tool=name, summary=summary,
                          verdict=decision.verdict, risk=decision.risk, reason=decision.reason)

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
            if (self.learning and decision.risk in TRUSTABLE_RISKS
                    and (trust := self._learned_match("trust", key, tool, args, decision.risk))):
                self.audit.record("approval", task=task_id, tool=name, approved=True, by="learned trust",
                                  learned=trust.id)
            else:
                outcome = await self.approvals.request(ApprovalRequest(
                    tool=name, summary=summary, risk=decision.risk, reason=decision.reason, task_id=task_id))
                self.audit.record("approval", task=task_id, tool=name, approved=outcome.approved, by=outcome.by)
                if self.learning and outcome.by != "timeout":  # only real human answers teach anything
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

    async def _run(self, tool: Tool, args: BaseModel, name: str, task_id: str) -> ToolResult:
        try:
            result = await tool.run(args)
        except Exception as e:
            log.exception("tool.crashed", tool=name)
            result = ToolResult(False, f"{type(e).__name__}: {e}")
        self.audit.record("tool", task=task_id, tool=name, ok=result.ok, evidence=result.evidence)
        return result
