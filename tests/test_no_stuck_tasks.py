"""A task always ends with a result: any exception (not just an AI error) becomes a failed task with a
plain message, the run record is closed, the UI is told and the next task can start. A Stop (cancel)
is cleaned up the same way and still finishes as cancelled."""

import asyncio
import errno
from pathlib import Path

import pytest
from fakes import FakeProvider, js

from delta.config import AgentConfig, Boundaries, MemoryConfig, Settings
from delta.core.events import EventBus
from delta.core.orchestrator import Orchestrator
from delta.core.proof import ProofRecorder
from delta.core.state import CURRENT_REQUEST, TASK_CRASHED, TaskRequest
from delta.llm.router import ModelRouter
from delta.memory.store import MemoryStore
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine
from delta.tools.filesystem import filesystem_tools
from delta.tools.registry import ToolExecutor, ToolRegistry


def make(tmp_path: Path) -> tuple[Orchestrator, list, list]:
    settings = Settings(agent=AgentConfig(max_attempts=1, max_steps=3),
                        memory=MemoryConfig(auto_extract=False), data_dir=tmp_path)
    router = ModelRouter({"fake": FakeProvider("fake", [js({"needs_tools": False, "answer": "hi"})])},
                         {"plan": ["fake"], "chat": ["fake"], "verify": ["fake"]})
    audit = AuditLog(tmp_path / "audit.jsonl")
    events_seen: list = []
    audited: list = []
    real_record = audit.record

    def record(event, **k):
        audited.append((event, k))
        real_record(event, **k)

    audit.record = record  # type: ignore[method-assign]
    executor = ToolExecutor(ToolRegistry(filesystem_tools()), PolicyEngine(Boundaries()),
                            ApprovalBroker(timeout_s=0.05), audit)
    bus = EventBus()

    async def on_event(event, data):
        events_seen.append((event, data.get("status")))

    bus.subscribe(on_event)
    orch = Orchestrator(settings, router, executor, MemoryStore(tmp_path / "m.db"), None, bus, audit,
                        proof=ProofRecorder(tmp_path / "runs"))
    return orch, events_seen, audited


@pytest.mark.parametrize("error", [RuntimeError("bug in verify"), KeyError("plan"), ValueError("bad json"),
                                   OSError(errno.ENOSPC, "No space left on device"),
                                   FileNotFoundError("runs/x/01.jpg"), AttributeError("NoneType")])
async def test_any_exception_becomes_a_failed_task_not_a_stuck_one(tmp_path, error):
    orch, events, audited = make(tmp_path)

    async def boom(req):
        raise error

    orch._run = boom  # type: ignore[method-assign]
    req = TaskRequest("do the thing")
    res = await orch.run(req)
    assert res.status == "failed" and res.answer == TASK_CRASHED
    assert str(error) not in res.answer and "Traceback" not in res.answer  # no internals to the user
    assert ("task.finished", "failed") in events
    assert ("task.error", {"task": req.id, "error": type(error).__name__}) in audited
    assert any(e == "task.end" and k["status"] == "failed" for e, k in audited)
    assert (tmp_path / "runs" / req.id / "run.json").exists()  # the run record is closed
    assert orch.proof.dir is None and CURRENT_REQUEST.get() is None and orch.executor._tainted == {}


async def test_a_broken_proof_folder_does_not_fail_the_answer(tmp_path):
    orch, events, _ = make(tmp_path)

    def broken_finish(result):
        raise OSError(errno.EACCES, "denied")

    orch.proof.finish = broken_finish  # type: ignore[method-assign]
    res = await orch.run(TaskRequest("hello"))
    assert res.status in ("done", "failed") and ("task.finished", res.status) in events
    assert orch.proof.dir is None  # the next task starts a fresh record


async def test_cancel_is_cleaned_up_and_re_raised(tmp_path):
    orch, events, audited = make(tmp_path)
    started = asyncio.Event()

    async def slow(req):
        started.set()
        await asyncio.sleep(30)

    orch._run = slow  # type: ignore[method-assign]
    req = TaskRequest("long job")
    t = asyncio.create_task(orch.run(req))
    await started.wait()
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert ("task.finished", "cancelled") in events
    assert any(e == "task.end" and k["status"] == "cancelled" for e, k in audited)
    assert not any(e == "task.error" for e, _ in audited)  # a Stop is not a crash
    assert (tmp_path / "runs" / req.id / "run.json").exists()


async def test_the_one_task_lock_is_released_after_a_crash(tmp_path):
    from types import SimpleNamespace

    from delta.bootstrap import Container

    orch, _, _ = make(tmp_path)

    async def boom(req):
        raise RuntimeError("bug")

    orch._run = boom  # type: ignore[method-assign]
    c = Container.__new__(Container)
    c.guard, c.orchestrator, c.audit = None, orch, SimpleNamespace(record=lambda *a, **k: None)
    c._task_lock, c.running = asyncio.Semaphore(1), None
    for _ in range(2):  # the second would hang forever if the first had kept the lock
        res = await asyncio.wait_for(c.submit(TaskRequest("x")), 5)
        assert res.status == "failed"
    assert c.running is None and not c._task_lock.locked()
