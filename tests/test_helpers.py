"""Parallel read-only helpers (delta.helpers): the allowlist is enforced by the executor (never by the prompt),
helpers never ask the user, never leave the allowed folders, inherit the task's taint, run concurrently with
their own token budget and time limit, skip CLI providers, merge answers with provenance, and are recorded as
child tasks in the task engine. Off by default: the planner prompt is unchanged unless they are turned on."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from engine_helpers import Rig, no_registry
from fakes import FakeProvider, call, js, text
from pydantic import BaseModel

from delta.config import Boundaries, HelpersConfig, Settings
from delta.core.planner import HELPERS_NOTE, make_plan
from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.engine.recorder import TaskEngine
from delta.engine.store import TaskStore
from delta.helpers.readonly import HELPER_TOOLS, ReadOnlyExecutor
from delta.helpers.supervisor import HelperResult, HelperSupervisor, merge
from delta.helpers.tools import HelperReadPage, ParallelResearch, helper_tools
from delta.llm.router import ModelRouter
from delta.llm.schemas import AllProvidersFailedError, LLMResponse
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.exfil_guard import URL_TOOLS
from delta.safety.policy import PolicyEngine
from delta.tools.base import Risk, Tool, ToolResult
from delta.tools.filesystem import filesystem_tools
from delta.tools.registry import ToolExecutor, ToolRegistry


def executor(tmp: Path, tools: list[Tool] | None = None, asked: list[Any] | None = None) -> ToolExecutor:
    approvals = ApprovalBroker(timeout_s=2)

    async def answer(req: Any) -> None:  # a person who says yes to everything: helpers must never reach them
        if asked is not None:
            asked.append(req)
        asyncio.get_running_loop().call_soon(approvals.resolve, req.id, True, "test")

    approvals.add_presenter("test", answer)
    (tmp / "work").mkdir(exist_ok=True)
    policy = PolicyEngine(Boundaries(allowed_paths=[str(tmp / "work")]))
    return ToolExecutor(ToolRegistry(tools or filesystem_tools()), policy, approvals, AuditLog(tmp / "audit.jsonl"))


# --- the read-only executor ---------------------------------------------------------------------------
async def test_only_allowlisted_read_tools_run(tmp_path: Path) -> None:
    asked: list[Any] = []
    ex = executor(tmp_path, asked=asked)
    ro = ReadOnlyExecutor(ex)
    (tmp_path / "work" / "a.txt").write_text("hello", encoding="utf-8")
    ok = await ro.execute("read_file", {"path": str(tmp_path / "work" / "a.txt")}, "p.h1", "p")
    assert ok.ok and "hello" in ok.output
    for name, args in [("write_file", {"path": str(tmp_path / "work" / "x.txt"), "content": "x"}),
                       ("delete_path", {"path": str(tmp_path / "work" / "a.txt")}),
                       ("move_path", {"src": str(tmp_path / "work" / "a.txt"), "dst": str(tmp_path / "work" / "b")}),
                       ("make_dir", {"path": str(tmp_path / "work" / "d")}),
                       ("browser_click", {"text": "Send"}), ("run_shell", {"command": "dir"})]:
        r = await ro.execute(name, args, "p.h1", "p")
        assert not r.ok and r.output.startswith("REFUSED") and r.evidence["helper_refused"], name
    assert not (tmp_path / "work" / "x.txt").exists() and (tmp_path / "work" / "a.txt").exists()
    assert not asked  # nothing ever reached a person
    names = {s["function"]["name"] for s in ro.specs()}
    assert names <= HELPER_TOOLS and "write_file" not in names and "read_file" in names


async def test_reads_outside_the_allowed_folders_are_refused_even_through_a_link(tmp_path: Path) -> None:
    ex = executor(tmp_path)
    ro = ReadOnlyExecutor(ex)
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    r = await ro.execute("read_file", {"path": str(secret)}, "p.h1", "p")
    assert not r.ok and "allowed folders" in r.output
    if sys.platform != "win32":
        os.symlink(secret, tmp_path / "work" / "link.txt")
        r = await ro.execute("read_file", {"path": str(tmp_path / "work" / "link.txt")}, "p.h1", "p")
        assert not r.ok and "allowed folders" in r.output and "top secret" not in r.output


class _Args(BaseModel):
    url: str = ""


class _NotReallyRead(Tool[_Args]):
    """A tool on the allowlist by name whose call is not read-only, from a forbidden module."""
    name = "fetch_page"
    description = "test"
    risk = Risk.READ
    Args = _Args

    def __init__(self, risk: Risk = Risk.READ) -> None:
        self.r = risk
        self.ran = False

    def risk_for(self, args: _Args) -> Risk:
        return self.r

    async def run(self, args: _Args) -> ToolResult:
        self.ran = True
        return ToolResult(True, "ran")


async def test_risk_and_module_are_checked_not_only_the_name(tmp_path: Path) -> None:
    writes = _NotReallyRead(Risk.EXTERNAL)
    ro = ReadOnlyExecutor(executor(tmp_path, [writes]))
    r = await ro.execute("fetch_page", {"url": "https://example.com"}, "p.h1", "p")
    assert not r.ok and "not read-only" in r.output and not writes.ran

    class UserBrowserRead(_NotReallyRead):
        pass
    UserBrowserRead.__module__ = "delta.tools.user_browser"
    ub = UserBrowserRead()
    ro = ReadOnlyExecutor(executor(tmp_path, [ub]))
    r = await ro.execute("fetch_page", {"url": "https://example.com"}, "p.h1", "p")
    assert not r.ok and r.evidence["helper_refused"] and not ub.ran
    assert ro.specs() == []


async def test_a_read_that_would_need_approval_is_refused_not_asked(tmp_path: Path) -> None:
    asked: list[Any] = []
    page = _NotReallyRead()
    ex = executor(tmp_path, [page], asked=asked)
    ex.taint("p", "browser_read")  # the parent task read a web page
    ro = ReadOnlyExecutor(ex)
    blob = "A" * 80  # encoded data in the address after an outside read: ExfilGuard asks
    r = await ro.execute("fetch_page", {"url": f"https://collect.example/?d={blob}"}, "p.h1", "p")
    assert not r.ok and r.evidence.get("helper_refused") and not page.ran and not asked
    assert ex.tainted_by("p") == "browser_read"


# --- the supervisor -------------------------------------------------------------------------------------
class Barrier:
    """Proves helpers run at the same time: each first model call waits until all have arrived."""

    def __init__(self, n: int) -> None:
        self.n, self.arrived, self.event = n, 0, asyncio.Event()
        self.max_inflight, self.inflight = 0, 0


def router_with(handler: Any, name: str = "api") -> ModelRouter:
    return ModelRouter({name: FakeProvider(name, handler=handler)}, {"chat": [name]})


async def test_helpers_run_in_parallel_and_merge_with_provenance(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir(exist_ok=True)
    (tmp_path / "work" / "a.txt").write_text("alpha", encoding="utf-8")
    gate = Barrier(2)
    seen: dict[str, int] = {}

    async def wait_all() -> None:
        gate.arrived += 1
        if gate.arrived >= gate.n:
            gate.event.set()
        await asyncio.wait_for(gate.event.wait(), 2)

    class Slow(FakeProvider):
        async def complete(self, task: str, messages: list[Any], tools: Any, json_mode: bool = False) -> Any:
            assert task == "helper"
            q = str(messages[1]["content"])
            seen[q] = seen.get(q, 0) + 1
            if seen[q] == 1:
                await wait_all()
                return call("read_file", path=str(tmp_path / "work" / "a.txt")) if "file" in q else text("web answer")
            return text("file says alpha")

    router = ModelRouter({"api": Slow("api")}, {"chat": ["api"]})
    ex = executor(tmp_path)
    sup = HelperSupervisor(HelpersConfig(enabled=True, max_helpers=2), router, ex)
    results = await sup.run("p", ["what is in the file?", "what does the web say?", "a third is dropped"])
    assert len(results) == 2 and all(r.ok for r in results)
    merged = merge(results)
    assert "[Helper 1] what is in the file?" in merged and "file says alpha" in merged and "web answer" in merged
    assert f"Sources: read_file {tmp_path / 'work' / 'a.txt'}" in merged
    assert router.meter["helper_tokens"] == sum(r.tokens for r in results) > 0 and router.meter["helpers"] == 2


async def test_timeout_and_token_budget_end_a_helper_not_the_task(tmp_path: Path) -> None:
    class Hang(FakeProvider):
        async def complete(self, task: str, messages: list[Any], tools: Any, json_mode: bool = False) -> Any:
            await asyncio.sleep(10)

    sup = HelperSupervisor(HelpersConfig(enabled=True, timeout_s=5), ModelRouter({"a": Hang("a")}, {"chat": ["a"]}),
                           executor(tmp_path))
    sup.cfg.timeout_s = 0.05  # (the setting's floor is 5 s; tests go faster)
    [r] = await sup.run("p", ["slow question"])
    assert not r.ok and "longer than" in r.error

    calls = {"n": 0}

    def chatty(task: str, messages: list[Any]) -> LLMResponse:
        calls["n"] += 1
        return call("read_file", path=str(tmp_path / "work" / "nothing.txt"))

    sup = HelperSupervisor(HelpersConfig(enabled=True, token_budget=500, max_steps=10), router_with(chatty),
                           executor(tmp_path))
    [r] = await sup.run("p", ["x" * 3000])  # one call is already over the budget
    assert calls["n"] <= 2 and "budget" in r.error


async def test_cli_providers_are_never_used_by_helpers(tmp_path: Path) -> None:
    from delta.config import ProviderConfig
    from delta.llm.cli_providers import ClaudeCodeProvider

    cli = ClaudeCodeProvider("claude_code", ProviderConfig(kind="claude_code", models={"chat": "x"}))
    router = ModelRouter({"claude_code": cli}, {"chat": ["claude_code"]})
    with pytest.raises(AllProvidersFailedError):
        await router.complete_api_only([{"role": "user", "content": "hi"}], None, "helper")
    sup = HelperSupervisor(HelpersConfig(enabled=True), router, executor(tmp_path))
    [r] = await sup.run("p", ["q"])
    assert not r.ok and "CLI" in r.error


def test_merge_says_plainly_what_failed() -> None:
    assert merge([]) == ""
    out = merge([HelperResult(1, "q1", "p.h1", ok=False, error="took longer than 60 s")])
    assert "not answered: took longer than 60 s" in out and "outside content" in out


# --- task engine: child tasks and the timeline ---------------------------------------------------------
async def test_helpers_are_child_tasks_in_the_timeline(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    engine = TaskEngine(store)
    engine.begin(TaskRequest("compare two things", channel="api", id="p1"))
    (tmp_path / "work").mkdir(exist_ok=True)
    sup = HelperSupervisor(HelpersConfig(enabled=True), router_with(lambda t, m: text("answer")), executor(tmp_path),
                           engine)
    await sup.run("p1", ["first", "second"])
    tl = engine.timeline("p1")
    assert tl is not None and [h["child"] for h in tl["helpers"]] == ["p1.h1", "p1.h2"]
    child = store.get("p1.h1")
    assert child is not None and child["channel"] == "helper" and child["state"] == "done"
    assert "p1" in child["note"]
    assert not [t for t in store.running() if t["channel"] == "helper"]  # never left to resume after a crash
    assert [t["id"] for t in engine.recover()] == ["p1"]  # only the parent was running; helpers are final
    store.close()


# --- planner, tools and defaults ---------------------------------------------------------------------------
async def test_planner_prompt_is_unchanged_unless_helpers_are_on() -> None:
    seen: list[str] = []

    def plan(task: str, messages: list[Any]) -> LLMResponse:
        seen.append(str(messages[0]["content"]))
        return js({"needs_tools": True, "goal": "g", "steps": [], "tools": [], "success_criteria": ["c"],
                   "research": ["one", "two", "three"]})

    router = ModelRouter({"a": FakeProvider("a", handler=plan)}, {"plan": ["a"]})
    off = await make_plan(router, "x", [], "", "Delta")
    on = await make_plan(router, "x", [], "", "Delta", helpers=2)
    assert off.research == [] and HELPERS_NOTE.format(n=2) not in seen[0]
    assert on.research == ["one", "two"] and seen[1].endswith(HELPERS_NOTE.format(n=2))


async def test_parallel_research_tool_reads_the_current_task(tmp_path: Path) -> None:
    sup = HelperSupervisor(HelpersConfig(enabled=False), router_with(lambda t, m: text("a")), executor(tmp_path))
    tool = ParallelResearch(sup)
    assert not tool.available()
    assert not (await tool.run(tool.Args(questions=["a", "b"]))).ok
    sup.cfg.enabled = True
    token = CURRENT_REQUEST.set(TaskRequest("t", channel="api", id="pp"))
    try:
        r = await tool.run(tool.Args(questions=["a", "b"]))
    finally:
        CURRENT_REQUEST.reset(token)
    assert r.ok and r.evidence["helpers"][0]["child"] == "pp.h1"
    page = HelperReadPage()
    assert not page.available()  # offered to helpers only
    assert "helper_read_page" in URL_TOOLS  # ExfilGuard checks its addresses
    assert not (await page.run(page.Args(url="file:///C:/Windows/win.ini"))).ok


async def test_bootstrap_registers_no_helper_tools_by_default(tmp_path: Path, monkeypatch: Any) -> None:
    no_registry(monkeypatch, tmp_path)
    from delta.bootstrap import build

    c = build(Settings(data_dir=tmp_path))
    names = set(c.registry.names())
    assert "parallel_research" not in names and "helper_read_page" not in names
    assert c.orchestrator.helpers is None and c.orchestrator.perception is not None
    assert {"look_with_marks", "click_mark", "read_screen_text", "click_text"} <= names
    c2 = build(Settings(data_dir=tmp_path / "on", helpers=HelpersConfig(enabled=True)))
    assert {"parallel_research", "helper_read_page"} <= set(c2.registry.names())
    assert c2.orchestrator.helpers is not None and c2.orchestrator.helpers.engine is c2.orchestrator.engine
    assert [t.name for t in helper_tools(c2.orchestrator.helpers)] == ["parallel_research", "helper_read_page"]


async def test_agent_loop_runs_planned_helpers_once_and_taints_the_task(tmp_path: Path, monkeypatch: Any) -> None:
    no_registry(monkeypatch, tmp_path)
    rig = Rig(tmp_path, {
        "plan": [js({"needs_tools": True, "goal": "g", "steps": [], "tools": ["read_file"],
                     "success_criteria": ["c"], "research": ["q one", "q two"]})],
        "helper": [text("helper answer A"), text("helper answer B")],
        "chat": [text("DONE: both found")]})
    rig.router.routes["helper"] = ["fake"]
    sup = HelperSupervisor(HelpersConfig(enabled=True), rig.router, rig.executor, rig.engine)
    for t in helper_tools(sup, read_page=False):
        rig.registry.register(t)
    rig.orch.helpers = sup
    result = await rig.orch.run(TaskRequest("compare", channel="api", id="ag"))
    assert result.status == "done"
    assert [s.tool for s in result.steps][:1] == ["parallel_research"]
    assert "helper answer A" in result.steps[0].output and "helper answer B" in result.steps[0].output
    assert result.usage["perception"]["helpers"] == 2
    tl = rig.engine.timeline("ag")
    assert tl is not None and len(tl["helpers"]) == 2
    taints = [line for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
              if '"taint"' in line and "parallel_research" in line]
    assert taints
