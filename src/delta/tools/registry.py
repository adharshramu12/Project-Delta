"""ToolRegistry + ToolExecutor: the single gate every tool call passes through.

validate args -> policy decision -> (approval) -> run -> audit. Nothing runs any other way.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from delta.safety.approvals import ApprovalBroker, ApprovalRequest
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine, Verdict
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
        return [t.spec() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, policy: PolicyEngine, approvals: ApprovalBroker,
                 audit: AuditLog) -> None:
        self.registry = registry
        self.policy = policy
        self.approvals = approvals
        self.audit = audit

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
            outcome = await self.approvals.request(ApprovalRequest(
                tool=name, summary=summary, risk=decision.risk, reason=decision.reason, task_id=task_id))
            self.audit.record("approval", task=task_id, tool=name, approved=outcome.approved, by=outcome.by)
            if not outcome.approved:
                return ToolResult(False, f"The user DENIED this action ({outcome.by}). Do not retry it.")

        try:
            result = await tool.run(args)
        except Exception as e:
            log.exception("tool.crashed", tool=name)
            result = ToolResult(False, f"{type(e).__name__}: {e}")
        self.audit.record("tool", task=task_id, tool=name, ok=result.ok, evidence=result.evidence)
        return result
