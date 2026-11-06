"""Failure injection for tests and scripted evals ONLY.

A FaultInjector makes chosen tool calls fail in a chosen way without running the tool, so the recovery
code can be tested on any PC (no real flaky page needed). It is never read from settings: only the eval
runner (`inject:` in an eval spec) and unit tests set `ToolExecutor.faults`. With it unset (always, in a
real Delta) the executor behaves exactly as before.

    rules: [{tool: browser_click, times: 1, output: "element [3] is no longer on the page ...",
             evidence: {...}}, {tool: read_file, times: 1, raise: "PermissionError: ..."}]

The injected failure happens where the tool would have run (after policy and approvals), so the step is
recorded in the event log like any real failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from delta.tools.base import ToolResult


@dataclass
class Fault:
    tool: str
    times: int = 1
    output: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    skip: int = 0  # let this many matching calls run normally first


class FaultInjector:
    def __init__(self, rules: list[dict[str, Any]] | None = None) -> None:
        self.faults = [Fault(tool=str(r["tool"]), times=int(r.get("times", 1)),
                             output=str(r.get("output") or r.get("raise") or "injected failure"),
                             evidence=dict(r.get("evidence") or {}), skip=int(r.get("skip", 0)))
                       for r in rules or []]
        self.fired: list[str] = []

    def take(self, tool: str) -> ToolResult | None:
        """The injected result for this call, or None to run the tool for real."""
        for f in self.faults:
            if f.tool != tool or f.times <= 0:
                continue
            if f.skip > 0:
                f.skip -= 1
                return None
            f.times -= 1
            self.fired.append(tool)
            return ToolResult(False, f.output, dict(f.evidence))
        return None
