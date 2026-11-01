"""Shared helpers for chat channels (Telegram, Slack, …)."""

from __future__ import annotations

from typing import Protocol

from delta.core.state import TaskResult
from delta.safety.approvals import ApprovalRequest

RISK_ICON = {"write": "✏️", "destructive": "🗑️", "external": "📤", "act": "🖱️", "read": "👀"}


class Channel(Protocol):
    name: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


def is_allowed(user_id: str | int, allowed: list[str]) -> bool:
    """Empty allow-list means nobody: a bot token alone must never be enough to control a PC."""
    return str(user_id) in {str(a) for a in allowed}


def approval_text(req: ApprovalRequest) -> str:
    return f"{RISK_ICON.get(req.risk, '⚠️')} Approval needed\n{req.summary}\n({req.risk}: {req.reason})"


def result_text(res: TaskResult) -> str:
    tail = {"done": "", "failed": "\n\n❌ not verified", "cancelled": "\n\n⏸ paused"}[res.status]
    return (res.answer or "(no answer)") + tail


def chunks(text: str, size: int = 3800) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]
