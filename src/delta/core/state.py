from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class TaskRequest:
    text: str
    channel: str = "cli"
    user: str = "owner"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])


@dataclass
class Plan:
    needs_tools: bool
    goal: str = ""
    steps: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    answer: str = ""  # filled when no tools are needed


@dataclass
class StepRecord:
    tool: str
    args: dict[str, Any]
    ok: bool
    output: str
    evidence: dict[str, Any]


@dataclass
class Verification:
    passed: bool
    reason: str
    missing: list[str] = field(default_factory=list)


Status = Literal["done", "failed", "cancelled"]


@dataclass
class TaskResult:
    task_id: str
    status: Status
    answer: str
    attempts: int = 0
    steps: list[StepRecord] = field(default_factory=list)
    verification: Verification | None = None
    provider: str = ""
