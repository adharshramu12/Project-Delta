"""ApprovalBroker.request must survive presenters added mid-request and slow presenters (audit M2, L8)."""

from __future__ import annotations

import asyncio

from delta.safety.approvals import ApprovalBroker, ApprovalRequest


def _req() -> ApprovalRequest:
    return ApprovalRequest(tool="run_shell", summary="run: dir", risk="write", reason="test")


async def test_presenter_added_while_presenting_does_not_crash_or_leak() -> None:
    """A channel started mid-request (daemon.start_channel) adds a presenter while we iterate."""
    broker = ApprovalBroker(timeout_s=1)
    seen: list[str] = []

    async def late(req: ApprovalRequest) -> None:
        seen.append("late")

    async def first(req: ApprovalRequest) -> None:
        seen.append("first")
        broker.add_presenter("late", late)
        await asyncio.sleep(0)
        asyncio.get_running_loop().call_soon(broker.resolve, req.id, True, "first")

    async def second(req: ApprovalRequest) -> None:
        seen.append("second")
        await asyncio.sleep(0)

    broker.add_presenter("first", first)
    broker.add_presenter("second", second)

    outcome = await asyncio.wait_for(broker.request(_req()), 2)

    assert outcome.approved and outcome.by == "first"
    assert sorted(seen) == ["first", "second"]  # the newcomer sees the next request, not this one
    assert "late" in broker._presenters
    assert broker.pending() == []


async def test_hung_presenter_does_not_block_the_others() -> None:
    broker = ApprovalBroker(timeout_s=2, present_timeout_s=0.2)

    async def hung(req: ApprovalRequest) -> None:
        await asyncio.sleep(3600)

    async def dashboard(req: ApprovalRequest) -> None:
        asyncio.get_running_loop().call_soon(broker.resolve, req.id, True, "dashboard")

    broker.add_presenter("telegram", hung)  # registered first: used to block everyone after it
    broker.add_presenter("dashboard", dashboard)

    outcome = await asyncio.wait_for(broker.request(_req()), 1)

    assert outcome.approved and outcome.by == "dashboard"
    assert broker.pending() == []


async def test_no_answer_still_means_deny_with_failing_and_hung_presenters() -> None:
    broker = ApprovalBroker(timeout_s=0.3, present_timeout_s=0.1)

    async def hung(req: ApprovalRequest) -> None:
        await asyncio.sleep(3600)

    async def broken(req: ApprovalRequest) -> None:
        raise RuntimeError("telegram down")

    broker.add_presenter("hung", hung)
    broker.add_presenter("broken", broken)

    outcome = await asyncio.wait_for(broker.request(_req()), 2)

    assert not outcome.approved and outcome.by == "timeout"
    assert broker.pending() == []


async def test_cancelled_request_cleans_up_pending() -> None:
    broker = ApprovalBroker(timeout_s=30, present_timeout_s=30)

    async def hung(req: ApprovalRequest) -> None:
        await asyncio.sleep(3600)

    broker.add_presenter("hung", hung)
    task = asyncio.ensure_future(broker.request(_req()))
    await asyncio.sleep(0.05)
    assert len(broker.pending()) == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert broker.pending() == []
