"""Human-in-the-loop approvals.

A request is broadcast to every registered presenter (CLI, Telegram, Slack, desktop app,
remote dashboard), so you can approve from whichever device you have. Presenters run side by side,
each with its own time limit, so one slow or broken channel cannot hold up the others. The first answer
wins; no answer before the timeout means DENY.

Remote Guard is enforced here for every channel at once (delta.safety.remote_guard.approval_refusal):
while remote control is locked, an answer from a phone counts as a "no" (logged), and approving a
destructive, sending or sensitive action from a phone needs the owner's PIN.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from delta.safety.remote_guard import approval_refusal
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
    human: bool = True  # False when no person chose this answer (a lock turned it into a "no")


Presenter = Callable[[ApprovalRequest], Awaitable[None]]


class ApprovalBroker:
    def __init__(self, timeout_s: float = 300, present_timeout_s: float = 10) -> None:
        self.timeout_s = timeout_s
        self.present_timeout_s = present_timeout_s  # per presenter, to show the request
        self._presenters: dict[str, Presenter] = {}
        self._pending: dict[str, tuple[ApprovalRequest, asyncio.Future[ApprovalOutcome]]] = {}
        self.guard: Any = None  # the Container's RemoteGuard, set at start-up

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
        """True when this answer settled the request (see answer() for why it may not)."""
        return self.answer(request_id, approved, by)[0]

    def answer(self, request_id: str, approved: bool, by: str) -> tuple[bool, str]:
        """Settle a request: (settled, what to show the person who answered).
        `by` is "<channel>:<user>" for a chat channel, or a local name like "dashboard" or "cli"."""
        entry = self._pending.get(request_id)
        if not entry or entry[1].done():
            return False, "already answered or expired"
        req, fut = entry
        refusal = approval_refusal(self.guard, by, approved, req.tool, str(req.risk))
        if refusal is not None:
            deny_instead, message = refusal
            if not deny_instead:
                log.warning("approval.needs_pin", id=request_id, by=by)
                return False, message
            log.warning("approval.locked_deny", id=request_id, by=by)
            fut.set_result(ApprovalOutcome(False, f"{by} (remote control locked)", human=False))
            return True, "denied: " + message
        fut.set_result(ApprovalOutcome(approved, by))
        return True, "approved" if approved else "denied"

    def deny_all(self, by: str = "shutdown") -> None:
        for rid in list(self._pending):
            self.resolve(rid, False, by)
