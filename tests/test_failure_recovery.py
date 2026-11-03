"""Failure drills: an AI dying in the middle of a task, and a whole task when every AI is down."""

import asyncio
from pathlib import Path

from fakes import FakeProvider, call, js, text

from delta.config import AgentConfig, Boundaries, MemoryConfig, Settings
from delta.core.events import EventBus
from delta.core.orchestrator import Orchestrator
from delta.core.state import TaskRequest
from delta.llm.router import ModelRouter
from delta.llm.schemas import LLMError
from delta.memory.store import MemoryStore
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine
from delta.tools.filesystem import filesystem_tools
from delta.tools.registry import ToolExecutor, ToolRegistry


def orchestrator(tmp_path: Path, providers: dict, routes: dict) -> Orchestrator:
    settings = Settings(agent=AgentConfig(max_attempts=2, max_steps=6),
                        memory=MemoryConfig(auto_extract=False), data_dir=tmp_path)
    approvals = ApprovalBroker(timeout_s=1)

    async def yes(req):
        asyncio.get_running_loop().call_soon(approvals.resolve, req.id, True, "test")

    approvals.add_presenter("test", yes)
    audit = AuditLog(tmp_path / "audit.jsonl")
    executor = ToolExecutor(ToolRegistry(filesystem_tools()), PolicyEngine(Boundaries()), approvals, audit)
    return Orchestrator(settings, ModelRouter(providers, routes), executor, MemoryStore(tmp_path / "m.db"),
                        None, EventBus(), audit)


async def test_main_ai_dies_mid_task_and_the_backup_finishes_it(tmp_path):
    target = tmp_path / "notes.txt"
    plan = js({"needs_tools": True, "goal": "notes", "success_criteria": [f"{target} exists"]})
    # The main AI plans and takes the first step, then hits a rate limit for the rest of the task.
    main = FakeProvider("main", [plan, call("write_file", path=str(target), content="hi")]
                        + [LLMError("429 rate limited")] * 10)
    backup_seen: list[list] = []

    def backup_handler(task, messages):
        backup_seen.append(messages)
        if task == "verify":
            return js({"passed": True, "reason": "path_info shows the file"})
        return call("path_info", path=str(target)) if len(backup_seen) == 1 else text("DONE: notes.txt created")

    backup = FakeProvider("backup", handler=backup_handler)
    routes = {t: ["main", "backup"] for t in ("plan", "chat", "verify")}
    r = await orchestrator(tmp_path, {"main": main, "backup": backup}, routes).run(TaskRequest("make notes"))

    assert r.status == "done", r.answer
    assert target.read_text() == "hi"  # the step done before the failure was not repeated
    assert [s.tool for s in r.steps] == ["write_file", "path_info"]
    assert "write_file" in str(backup_seen[0])  # the backup AI got the history so far


async def test_every_ai_down_ends_with_an_honest_error_not_a_crash(tmp_path):
    down = FakeProvider("only", [LLMError("connection refused")] * 10)
    routes = {t: ["only"] for t in ("plan", "chat", "verify")}
    r = await orchestrator(tmp_path, {"only": down}, routes).run(TaskRequest("make notes"))
    assert r.status in ("failed", "error") and "connection refused" in r.answer.lower()
