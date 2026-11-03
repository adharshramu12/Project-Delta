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
    tail = {"done": "", "failed": "\n\n❌ not verified", "cancelled": "\n\n⏸ paused",
            "needs_user": "\n\n❓ waiting for your answer"}[res.status]
    return (res.answer or "(no answer)") + tail


def to_html(text: str) -> str:
    """Markdown from the model (**bold**, *italic*, `code`, [link](url)) as Telegram HTML. Sent as-is,
    Telegram shows the raw asterisks. Everything else is escaped, so no text can break the markup."""
    import html
    import re

    out = html.escape(text, quote=False)
    out = re.sub(r"```(?:\w+\n)?(.+?)```", r"<pre>\1</pre>", out, flags=re.S)
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.S)
    out = re.sub(r"__(.+?)__", r"<b>\1</b>", out, flags=re.S)
    out = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", out)
    out = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', out)
    return re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", out)


def chunks(text: str, size: int = 3800) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]
