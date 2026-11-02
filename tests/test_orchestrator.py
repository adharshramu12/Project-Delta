"""End-to-end agent loop with a scripted model and real file tools in a temp folder."""

import asyncio
from pathlib import Path

from fakes import FakeProvider, call, js, text

from delta.config import AgentConfig, Boundaries, MemoryConfig, Settings
from delta.core.events import EventBus
from delta.core.orchestrator import Orchestrator
from delta.core.state import TaskRequest
from delta.llm.router import ModelRouter
from delta.memory.store import MemoryStore
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine
from delta.tools.filesystem import filesystem_tools
from delta.tools.registry import ToolExecutor, ToolRegistry


def make(tmp_path: Path, script: dict[str, list], approve: bool | None = True):
    settings = Settings(agent=AgentConfig(max_attempts=2, max_steps=6),
                        memory=MemoryConfig(auto_extract=False), data_dir=tmp_path)
    queues = {k: list(v) for k, v in script.items()}

    def handler(task, messages):
        return queues[task].pop(0)

    router = ModelRouter({"fake": FakeProvider("fake", handler=handler)},
                         {"plan": ["fake"], "chat": ["fake"], "verify": ["fake"]})
    approvals = ApprovalBroker(timeout_s=1)
    if approve is not None:
        async def auto(req):
            asyncio.get_running_loop().call_soon(approvals.resolve, req.id, approve, "test")
        approvals.add_presenter("test", auto)
    audit = AuditLog(tmp_path / "audit.jsonl")
    executor = ToolExecutor(ToolRegistry(filesystem_tools()), PolicyEngine(Boundaries()), approvals, audit)
    memory = MemoryStore(tmp_path / "m.db")
    return Orchestrator(settings, router, executor, memory, None, EventBus(), audit), audit, memory


async def test_direct_answer_skips_tools(tmp_path):
    orch, _, _ = make(tmp_path, {"plan": [js({"needs_tools": False, "answer": "Hello!"})]})
    r = await orch.run(TaskRequest("what is 2+2"))
    assert r.status == "done" and r.answer == "Hello!"


async def test_write_file_with_approval_and_verification(tmp_path):
    target = str(tmp_path / "notes.txt")
    orch, audit, memory = make(tmp_path, {
        "plan": [js({"needs_tools": True, "goal": "make notes", "steps": ["write", "check"],
                     "success_criteria": [f"{target} exists"]})],
        "chat": [call("write_file", path=target, content="hello"), call("path_info", path=target),
                 text("DONE: Created notes.txt")],
        "verify": [js({"passed": True, "reason": "path_info shows it exists"})],
    })
    r = await orch.run(TaskRequest("create notes"))
    await orch.drain()  # memory/skill learning runs after the answer is returned
    assert r.status == "done", r.answer
    assert Path(target).read_text() == "hello"
    assert [s.tool for s in r.steps] == ["write_file", "path_info"]
    events = [e["event"] for e in audit.tail(50)]
    assert "approval" in events and audit.verify()
    assert any(m.kind == "episode" for m in memory.list())


async def test_user_denial_stops_the_task_at_once(tmp_path):
    """A human 'no' ends the task: no workaround attempts, no verify-retry asking again."""
    target = str(tmp_path / "x.txt")
    orch, _, _ = make(tmp_path, approve=False, script={
        "plan": [js({"needs_tools": True, "success_criteria": [f"{target} exists"]})],
        "chat": [call("write_file", path=target, content="x"), call("make_dir", path=str(tmp_path / "other"))],
        "verify": [],
    })
    r = await orch.run(TaskRequest("make x"))
    assert r.status == "cancelled" and "You declined" in r.answer
    assert [s.tool for s in r.steps] == ["write_file"]  # nothing ran after the "no"
    assert not Path(target).exists()


async def test_unverified_task_retries_then_fails(tmp_path):
    target = str(tmp_path / "x.txt")
    orch, _, _ = make(tmp_path, script={
        "plan": [js({"needs_tools": True, "success_criteria": [f"{target} contains 'y'"]})],
        "chat": [call("write_file", path=target, content="x"), text("DONE: done!"),
                 call("path_info", path=target), text("DONE: Done again.")],
        "verify": [js({"passed": False, "reason": "wrong content", "missing": ["contains y"]}),
                   js({"passed": False, "reason": "still wrong"})],
    })
    r = await orch.run(TaskRequest("make x"))
    assert r.status == "failed" and r.attempts == 2
    assert "could not verify" in r.answer


async def test_no_actions_taken_fails_verification(tmp_path):
    orch, _, _ = make(tmp_path, {
        "plan": [js({"needs_tools": True, "success_criteria": ["file exists"]})],
        "chat": [text("DONE: I did it!"), text("DONE: I did it again!")],
        "verify": [],
    })
    r = await orch.run(TaskRequest("make file"))
    assert r.status == "failed"
    assert r.verification and r.verification.reason == "no actions were taken"


async def test_a_false_no_tools_answer_is_not_accepted(tmp_path):
    """Codex once stopped mid-task with 'this environment doesn't provide the desktop-control tools'."""
    target = str(tmp_path / "notes.txt")
    orch, _, _ = make(tmp_path, {
        "plan": [js({"needs_tools": True, "goal": "make notes", "steps": ["write"], "tools": ["write_file"],
                     "success_criteria": [f"{target} exists"]})],
        "chat": [text("NEED_USER: I'm unable to continue because this environment doesn't provide the tools."),
                 call("write_file", path=target, content="hi"), text("DONE: Created notes.txt")],
        "verify": [js({"passed": True, "reason": "written"})],
    })
    r = await orch.run(TaskRequest("create notes"))
    assert r.status == "done", r.answer
    assert Path(target).read_text() == "hi"


def test_real_blockers_still_reach_the_user():
    from delta.core.orchestrator import claims_no_tools

    assert claims_no_tools("NEED_USER: I don’t have access to the browser here")
    assert not claims_no_tools("NEED_USER: You are not signed in to NotebookLM. Please sign in.")
    assert not claims_no_tools("NEED_USER: Which Ravi do you mean, Ravi K or Ravi S?")


def test_website_requests_get_the_whole_browser_kit(tmp_path, monkeypatch):
    orch, _, _ = make(tmp_path, {})
    every = ["run_shell", "write_file", "browser_open", "browser_click", "browser_fill", "browser_read",
             "click_at", "click_element", "type_text"]
    monkeypatch.setattr(orch.executor.registry, "specs", lambda: [{"function": {"name": n}} for n in every])

    def names(wanted, text):
        return {s["function"]["name"] for s in orch._tool_specs(wanted, text)}

    assert {"browser_open", "browser_click", "browser_fill"} <= names(["run_shell"], "Open Chrome, then NotebookLM")
    assert {"click_element", "type_text"} <= names(["click_at"], "click OK in Paint")  # the whole group
    assert not {"browser_click", "click_at"} & names(["write_file"], "create notes.txt")
