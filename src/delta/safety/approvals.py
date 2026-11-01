"""Human-in-the-loop approvals.

A request is broadcast to every registered presenter (CLI, Telegram, Slack, desktop app,
remote dashboard), so you can approve from whichever device you have. The first answer wins;
no answer before the timeout means DENY.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from delta.system.logging import log


@dataclass
class ApprovalRequest:
    tool: str
    summary: str
    risk: str
    reason: str
    task_id: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class ApprovalOutcome:
    approved: bool
    by: str  # who answered, or "timeout"


Presenter = Callable[[ApprovalRequest], Awaitable[None]]


class ApprovalBroker:
    def __init__(self, timeout_s: float = 300) -> None:
        self.timeout_s = timeout_s
        self._presenters: dict[str, Presenter] = {}
        self._pending: dict[str, tuple[ApprovalRequest, asyncio.Future[ApprovalOutcome]]] = {}

    def add_presenter(self, name: str, presenter: Presenter) -> None:
        self._presenters[name] = presenter

    def pending(self) -> list[ApprovalRequest]:
        return [req for req, _ in self._pending.values()]

    async def request(self, req: ApprovalRequest) -> ApprovalOutcome:
        fut: asyncio.Future[ApprovalOutcome] = asyncio.get_running_loop().create_future()
        self._pending[req.id] = (req, fut)
        if not self._presenters:
            log.warning("approval.no_presenter", id=req.id)
        for name, present in self._presenters.items():
            try:
                await present(req)
            except Exception as e:
                log.error("approval.present_failed", presenter=name, error=str(e))
        try:
            return await asyncio.wait_for(asyncio.shield(fut), self.timeout_s)
        except TimeoutError:
            return ApprovalOutcome(False, "timeout")
        finally:
            self._pending.pop(req.id, None)

    def resolve(self, request_id: str, approved: bool, by: str) -> bool:
        entry = self._pending.get(request_id)
        if not entry or entry[1].done():
            return False
        entry[1].set_result(ApprovalOutcome(approved, by))
        return True

    def deny_all(self, by: str = "shutdown") -> None:
        for rid in list(self._pending):
            self.resolve(rid, False, by)
