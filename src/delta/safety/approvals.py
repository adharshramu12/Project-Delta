"""Human-in-the-loop approvals.

A request is broadcast to every registered presenter (CLI, Telegram, Slack, desktop app,
remote dashboard), so you can approve from whichever device you have. Presenters run side by side,
each with its own time limit, so one slow or broken channel cannot hold up the others. The first answer
wins; no answer before the timeout means DENY.
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
    def __init__(self, timeout_s: float = 300, present_timeout_s: float = 10) -> None:
        self.timeout_s = timeout_s
        self.present_timeout_s = present_timeout_s  # per presenter, to show the request
        self._presenters: dict[str, Presenter] = {}
        self._pending: dict[str, tuple[ApprovalRequest, asyncio.Future[ApprovalOutcome]]] = {}

    def add_presenter(self, name: str, presenter: Presenter) -> None:
        self._presenters[name] = presenter

    def pending(self) -> list[ApprovalRequest]:
        return [req for req, _ in self._pending.values()]

    async def _present(self, name: str, present: Presenter, req: ApprovalRequest) -> None:
        try:
            await asyncio.wait_for(present(req), self.present_timeout_s)
        except TimeoutError:
            log.error("approval.present_timeout", presenter=name, id=req.id, timeout_s=self.present_timeout_s)
        except Exception as e:
            log.error("approval.present_failed", presenter=name, error=str(e))

    async def request(self, req: ApprovalRequest) -> ApprovalOutcome:
        fut: asyncio.Future[ApprovalOutcome] = asyncio.get_running_loop().create_future()
        self._pending[req.id] = (req, fut)
        showing: asyncio.Future[list[object]] | None = None
        try:
            # A snapshot: a channel started meanwhile (daemon.start_channel) adds a presenter, and changing
            # the dict while iterating it raised RuntimeError (same bug class as issue #8).
            presenters = list(self._presenters.items())
            if not presenters:
                log.warning("approval.no_presenter", id=req.id)
            showing = asyncio.gather(*(self._present(name, present, req) for name, present in presenters),
                                     return_exceptions=True)
            return await asyncio.wait_for(asyncio.shield(fut), self.timeout_s)
        except TimeoutError:
            return ApprovalOutcome(False, "timeout")
        finally:
            self._pending.pop(req.id, None)
            if showing is not None and not showing.done():
                showing.cancel()  # answered already: stop waiting on a slow channel

    def resolve(self, request_id: str, approved: bool, by: str) -> bool:
        entry = self._pending.get(request_id)
        if not entry or entry[1].done():
            return False
        entry[1].set_result(ApprovalOutcome(approved, by))
        return True

    def deny_all(self, by: str = "shutdown") -> None:
        for rid in list(self._pending):
            self.resolve(rid, False, by)
