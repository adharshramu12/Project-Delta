from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class TaskRequest:
    text: str
    channel: str = "cli"
    user: str = "owner"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    chat_id: str = ""
    history: str = ""  # short recap of this chat's recent turns (ChatStore.context_for)

    @property
    def with_context(self) -> str:
        """The request as the AI should read it: earlier turns of this chat first, so follow-ups
        like "send it to him" resolve. Instant commands and skill matching use `text` alone."""
        if not self.history:
            return self.text
        return (f"Earlier in this chat (for context; do not redo finished work):\n{self.history}\n\n"
                f"Current request: {self.text}")


@dataclass
class Plan:
    needs_tools: bool
    goal: str = ""
    steps: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    answer: str = ""  # filled when no tools are needed
    skill_id: int | None = None  # learned skill the planner chose to follow
    tools: list[str] = field(default_factory=list)  # tools the planner expects to need
    direct: dict[str, Any] | None = None  # {"tool", "args"}: one call does the whole job (no agent loop)
    remember: list[str] = field(default_factory=list)  # lasting facts about the user, noted in the same call


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
    checks: list[dict[str, Any]] = field(default_factory=list)  # per checkpoint: met + what showed it
    screenshot: bool = False  # judged against a fresh screenshot, not only the text log
    raw: str = ""  # the checker's own reply, kept for tracing odd verdicts


Status = Literal["done", "failed", "cancelled", "needs_user"]


@dataclass
class TaskResult:
    task_id: str
    status: Status
    answer: str
    attempts: int = 0
    steps: list[StepRecord] = field(default_factory=list)
    verification: Verification | None = None
    provider: str = ""
    skill_id: int | None = None
    tokens: dict[str, int] = field(default_factory=dict)  # input / cached / output / calls for this task
    fallback: str = ""  # why the user's chosen main AI did not answer, e.g. "cline skipped: credits used up"
    fast: bool = False  # finished by the planner's one direct tool call
    options: list[str] = field(default_factory=list)  # needs_user: quick answers the user can tap
    # needs_user: a form of up to 4 questions [{"q", "options", "multi"}] (delta.core.questions)
    questions: list[dict[str, Any]] = field(default_factory=list)
    asked_about: str = ""  # needs_user: the original request, so an answer can continue it


# The request being worked on, for tools that answer to whoever asked (send_file -> their chat).
CURRENT_REQUEST: ContextVar[TaskRequest | None] = ContextVar("current_request", default=None)
