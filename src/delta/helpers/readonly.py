"""The read-only executor for helpers: a tool allowlist enforced in code, never by the prompt.

A helper may only call the tools in HELPER_TOOLS, and only calls whose risk is READ. File reads must stay
inside the user's allowed folders (checked on the real path, so a link cannot point out of them). The
user's own browser is never reachable: no browser tool is on the list, and a tool that drives it is refused
by its module whatever its name. Helpers never ask the user anything: a call that would need an approval
(Remote Guard, ExfilGuard, quiet hours, a policy ASK) is refused instead, with no "Allow for this task" and
no learned trust.

Every allowed call still runs through an ordinary ToolExecutor that shares the main one's registry, policy,
audit log, Remote Guard, ExfilGuard and watchdog, so a helper is never less checked than the agent itself.
"""

from __future__ import annotations

import json
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from delta.safety.approvals import ApprovalOutcome, ApprovalRequest
from delta.safety.policy import norm_path
from delta.tools.base import Risk, ToolResult
from delta.tools.registry import ToolExecutor

# True while helpers run: helper-only tools (helper_read_page) are offered only then.
IN_HELPER: ContextVar[bool] = ContextVar("delta_in_helper", default=False)
# The only tools a helper can call. Nothing here clicks, types, writes, sends or opens an app.
HELPER_TOOLS = frozenset({"web_search", "fetch_page", "helper_read_page", "read_file", "list_dir", "search_files",
                          "path_info"})
# Modules whose tools act on the user's own screen or signed-in browser: refused even if a name matched.
FORBIDDEN_MODULES = ("delta.tools.user_browser", "delta.tools.desktop", "delta.tools.browser",
                     "delta.tools.browser_switch", "delta.messaging", "delta.tools.pc", "delta.tools.screen")
REFUSED = ("REFUSED: helpers are read-only. They can search the web, read pages and read files in the allowed "
           "folders, nothing else.")


class _NeverAsk:
    """Approval broker for helpers: every question is answered no, by no person, at once."""

    async def request(self, req: ApprovalRequest) -> ApprovalOutcome:
        return ApprovalOutcome(False, "helper (read-only helpers never ask)", human=False)


def _real(p: str | Path) -> Path:
    return Path(os.path.normcase(os.path.realpath(os.path.expandvars(os.path.expanduser(str(p))))))


class ReadOnlyExecutor:
    def __init__(self, inner: ToolExecutor) -> None:
        self.inner = inner
        ex = ToolExecutor(inner.registry, inner.policy, _NeverAsk(), inner.audit)  # type: ignore[arg-type]
        ex.guard = inner.guard        # Remote Guard: a phone request still needs the PIN to read files
        ex.exfil = inner.exfil        # ExfilGuard: addresses carrying data after a read are refused
        ex.watchdog = inner.watchdog  # time limits per call
        # no recorder (helpers are recorded as child tasks by the supervisor), no undo (nothing is written),
        # no learning (a helper refusal must never teach anything), no fault injection
        self.exec = ex

    def specs(self) -> list[dict[str, Any]]:
        out = []
        for spec in self.inner.registry.specs():
            name = spec["function"]["name"]
            if name in HELPER_TOOLS and self._module_ok(name):
                out.append(spec)
        return out

    def _module_ok(self, name: str) -> bool:
        tool = self.inner.registry.get(name)
        module = str(getattr(type(tool), "__module__", ""))
        return tool is not None and not module.startswith(FORBIDDEN_MODULES)

    def _refuse(self, child_id: str, name: str, why: str) -> ToolResult:
        self.inner.audit.record("helper.refused", task=child_id, tool=name, reason=why)
        return ToolResult(False, f"{REFUSED} ({why})", {"helper_refused": True})

    def allowed_paths(self) -> list[Path]:
        return [_real(p) for p in self.inner.policy.allowed]

    async def execute(self, name: str, raw_args: dict[str, Any], child_id: str, parent_id: str = "") -> ToolResult:
        if name not in HELPER_TOOLS:
            return self._refuse(child_id, name, f"'{name}' is not a read-only helper tool")
        tool = self.inner.registry.get(name)
        if tool is None or not self._module_ok(name):
            return self._refuse(child_id, name, f"'{name}' is not available to helpers")
        try:
            args = tool.Args.model_validate(raw_args if isinstance(raw_args, dict) else {})
        except ValidationError as e:
            return ToolResult(False, f"invalid arguments for {name}: {e.errors(include_url=False)}")
        if tool.risk_for(args) != Risk.READ:
            return self._refuse(child_id, name, "that call is not read-only")
        paths = [p for p in tool.paths(args) if p]
        if paths:
            roots = [norm_path(p) for p in self.inner.policy.allowed]
            real_roots = self.allowed_paths()
            for p in paths:
                plain, real = norm_path(p), _real(p)
                if not any(plain == r or plain.is_relative_to(r) for r in roots) or not any(
                        real == r or real.is_relative_to(r) for r in real_roots):
                    return self._refuse(child_id, name, "helpers read files only inside the allowed folders")
        if parent_id and (source := self.inner.tainted_by(parent_id)):
            self.exec.taint(child_id, source)  # outside content steering the parent steers its helpers too
        result = await self.exec.execute(name, json.loads(json.dumps(raw_args, default=str)), task_id=child_id)
        if "DENIED" in result.output[:40] or result.evidence.get("denied_action"):
            # a question a person would have had to answer: helpers never ask, so it is a refusal
            return ToolResult(False, "REFUSED: this read needs your approval, and helpers never ask. The main "
                                     "agent can do it instead.", {"helper_refused": True})
        return result

    def forget(self, child_id: str) -> None:
        self.exec.forget_task(child_id)
