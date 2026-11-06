"""Self-heal (delta.selfheal): failure taxonomy, playbooks, caps, never retrying side effects, the strategy
ladder, benign-popup rules, the opt-in model ladder and failover (off by default), and undo."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from engine_helpers import Rig, no_registry
from fakes import FakeProvider, call, text
from pydantic import BaseModel

from delta.config import Boundaries, RouterConfig, SelfHealConfig, Settings, UndoConfig
from delta.core.events import EventBus
from delta.core.state import StepRecord, TaskRequest
from delta.evals.fakebrowser import FakeBrowserManager, fake_browser_tools
from delta.llm.ladder import ModelLadder
from delta.llm.router import ModelRouter
from delta.llm.schemas import AllProvidersFailedError, LLMError
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine
from delta.selfheal import strategy
from delta.selfheal.healer import Healer, retry_safe
from delta.selfheal.inject import FaultInjector
from delta.selfheal.playbooks import PLAYBOOKS, PRE_ACTION, playbook_for
from delta.selfheal.taxonomy import Failure, classify, classify_llm_error, is_outage, is_refusal
from delta.selfheal.undo import UndoStore
from delta.tools.base import Risk, Tool, ToolResult
from delta.tools.filesystem import filesystem_tools
from delta.tools.registry import ToolExecutor, ToolRegistry

F = Failure


# --- taxonomy ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(("output", "evidence", "want"), [
    ("element [3] is no longer on the page (it changed or navigated); call browser_read", {}, F.STALE_ELEMENT),
    ("nothing visible called 'Save'; use browser_read to see the page", {}, F.ELEMENT_NOT_FOUND),
    ("no visible control named 'OK'; call read_window to see what exists", {}, F.ELEMENT_NOT_FOUND),
    ("TimeoutError: Timeout 8000ms exceeded.", {}, F.TIMEOUT),
    ("STOPPED by the watchdog: web_search took longer than 90 seconds.", {"watchdog": "timeout"}, F.TIMEOUT),
    ('Error: <div id="consent"> intercepts pointer events', {}, F.POPUP_OR_DIALOG),
    ("Please sign in to continue", {}, F.SIGN_IN_WALL),
    ("This page is protected by reCAPTCHA (g-recaptcha)", {}, F.CAPTCHA),
    ("Error: net::ERR_NAME_NOT_RESOLVED at https://nope.invalid", {}, F.NAVIGATION_ERROR),
    ("Notepad is not responding", {}, F.APP_NOT_RESPONDING),
    ("The Delta browser stopped responding.", {"watchdog": "browser_dead"}, F.APP_CRASHED),
    ("Target page, context or browser has been closed", {}, F.APP_CRASHED),
    ("PermissionError: [Errno 13] Permission denied: 'x'", {}, F.PERMISSION_DENIED),
    ("[WinError 5] Access is denied", {}, F.PERMISSION_DENIED),
    ("[WinError 32] The process cannot access the file because it is being used by another process", {},
     F.FILE_LOCKED),
    ("429 Too Many Requests", {}, F.RATE_LIMIT),
    ("file not found: C:\\x.txt", {}, F.UNKNOWN),
    ("BLOCKED by policy: tool 'x' is disabled", {}, F.PERMISSION_DENIED),
])
def test_classify_tool_failures(output: str, evidence: dict[str, Any], want: Failure) -> None:
    assert classify("some_tool", output, evidence) == want


def test_classify_exceptions_and_model_errors() -> None:
    locked = PermissionError(13, "The process cannot access the file because it is being used by another process")
    assert classify("write_file", exc=locked) == F.FILE_LOCKED
    assert classify("write_file", exc=PermissionError(13, "Permission denied")) == F.PERMISSION_DENIED
    assert classify("x", exc=TimeoutError()) == F.TIMEOUT
    assert classify_llm_error("rate_limit") == F.RATE_LIMIT
    assert classify_llm_error("error", kinds=["rate_limit", "unreachable"]) == F.RATE_LIMIT
    assert classify_llm_error("too_large") == F.MODEL_ERROR
    assert classify_llm_error("error", kinds=["quota"]) == F.BUDGET
    assert is_outage("rate_limit") and is_outage("quota") and is_outage("error", "groq: HTTP 503: overloaded")
    assert not is_outage("error", "claude_code: not signed in") and not is_outage("too_large")


def test_refusals_are_recognised() -> None:
    assert is_refusal("BLOCKED by policy: no")
    assert is_refusal("REFUSED: password/payment field. Ask the user to fill it in.")
    assert is_refusal("The user DENIED this action (test).")
    assert is_refusal("STOPPED: you moved the mouse")
    assert is_refusal("anything", {"user_denied": True})
    assert not is_refusal("STOPPED by the watchdog: took too long")


def test_every_failure_has_a_playbook_and_only_pre_action_failures_unlock_side_effects() -> None:
    assert set(PLAYBOOKS) == set(Failure)
    assert playbook_for(F.SIGN_IN_WALL).stop and playbook_for(F.PERMISSION_DENIED).stop
    assert not playbook_for(F.CAPTCHA).retry  # captchas only go to the captcha rules
    assert {F.STALE_ELEMENT, F.ELEMENT_NOT_FOUND} == PRE_ACTION
    # side-effecting tools are never retried, whatever the failure
    for name in ("run_shell", "run_python", "send_message", "send_file", "write_file", "delete_path", "edit_file",
                 "move_path"):
        for failure in Failure:
            assert not retry_safe(name, "read", failure)
    assert retry_safe("browser_click", "act", F.TIMEOUT)
    assert not retry_safe("browser_click", "external", F.TIMEOUT)  # a Send click that timed out may have sent
    assert retry_safe("browser_click", "external", F.STALE_ELEMENT)  # the click provably never happened
    assert not retry_safe("browser_click", "destructive", F.POPUP_OR_DIALOG)


# --- a small rig: real executor and policy, the fake browser, a healer ------------------------------
class FlakyArgs(BaseModel):
    path: str = ""


class FlakyRead(Tool[FlakyArgs]):
    """A read-only tool that fails the first `fails` times with `message`."""
    name = "flaky_read"
    description = "test"
    risk = Risk.READ
    Args = FlakyArgs

    def __init__(self, message: str, fails: int = 1) -> None:
        self.message, self.fails, self.calls = message, fails, 0

    async def run(self, args: FlakyArgs) -> ToolResult:
        self.calls += 1
        if self.calls <= self.fails:
            return ToolResult(False, self.message)
        return ToolResult(True, "the content")


def make(tmp: Path, tools: list[Tool], autonomy: str = "ask-risky", approve: bool = True,
         cfg: SelfHealConfig | None = None) -> tuple[ToolExecutor, Healer, list[Any]]:
    approvals = ApprovalBroker(timeout_s=2)
    asked: list[Any] = []

    async def answer(req: Any) -> None:
        asked.append(req)
        asyncio.get_running_loop().call_soon(approvals.resolve, req.id, approve, "test")

    approvals.add_presenter("test", answer)
    policy = PolicyEngine(Boundaries(autonomy=autonomy, allowed_paths=[str(tmp)]))  # type: ignore[arg-type]
    executor = ToolExecutor(ToolRegistry(tools), policy, approvals, AuditLog(tmp / "audit.jsonl"))
    return executor, Healer(cfg or SelfHealConfig(), executor, EventBus(), settle_s=0), asked


async def test_read_only_step_is_retried_once_and_recorded(tmp_path: Path) -> None:
    tool = FlakyRead("PermissionError: [WinError 32] being used by another process")
    executor, healer, _ = make(tmp_path, [tool])
    steps: list[StepRecord] = []
    first = await executor.execute("flaky_read", {}, task_id="t1")
    rec = await healer.recover("t1", "flaky_read", {}, first, steps)
    assert rec.recovered and rec.result.ok and rec.failure == F.FILE_LOCKED
    assert tool.calls == 2 and "waited a moment" in rec.note()


async def test_caps_per_step_and_per_task(tmp_path: Path) -> None:
    tool = FlakyRead("TimeoutError: Timeout 8000ms exceeded.", fails=99)
    executor, healer, _ = make(tmp_path, [tool], cfg=SelfHealConfig(max_per_step=1, max_per_task=2))
    steps: list[StepRecord] = []
    r = await executor.execute("flaky_read", {"path": "a"}, task_id="t")
    await healer.recover("t", "flaky_read", {"path": "a"}, r, steps)
    calls = tool.calls
    again = await healer.recover("t", "flaky_read", {"path": "a"}, r, steps)  # same step: cap reached
    assert tool.calls == calls and "used up" in again.hint
    await healer.recover("t", "flaky_read", {"path": "b"}, r, steps)  # another step: the task's last attempt
    calls = tool.calls
    await healer.recover("t", "flaky_read", {"path": "c"}, r, steps)  # task cap reached
    assert tool.calls == calls and healer.attempts("t") == 2
    healer.forget("t")
    assert healer.attempts("t") == 0


async def test_refusals_and_disabled_selfheal_never_recover(tmp_path: Path) -> None:
    tool = FlakyRead("x")
    executor, healer, _ = make(tmp_path, [tool])
    for output, ev in (("BLOCKED by policy: no", {}), ("The user DENIED this action", {"user_denied": True})):
        rec = await healer.recover("t", "flaky_read", {}, ToolResult(False, output, ev), [])
        assert not rec.recovered and not rec.lines and tool.calls == 0
    off = Healer(SelfHealConfig(enabled=False), executor, settle_s=0)
    rec = await off.recover("t", "flaky_read", {}, ToolResult(False, "TimeoutError: Timeout"), [])
    assert not rec.lines and tool.calls == 0


async def test_side_effect_is_not_retried_even_when_the_failure_looks_temporary(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")
    executor, healer, _ = make(tmp_path, filesystem_tools())
    executor.faults = FaultInjector([{"tool": "write_file", "times": 1, "output": "TimeoutError: timed out"}])
    args = {"path": str(target), "content": "new", "append": True}
    steps: list[StepRecord] = []
    r = await executor.execute("write_file", args, task_id="t")
    rec = await healer.recover("t", "write_file", args, r, steps)
    assert not rec.result.ok and not steps and "could happen twice" in rec.note()
    assert target.read_text(encoding="utf-8") == "old"


async def test_permission_denied_stops_and_sign_in_wall_asks(tmp_path: Path) -> None:
    _executor, healer, _ = make(tmp_path, [FlakyRead("x")])
    rec = await healer.recover("t", "flaky_read", {}, ToolResult(False, "PermissionError: [Errno 13] Permission "
                                                                        "denied"), [])
    assert rec.stop.startswith("NEED_USER:") and "Windows did not allow" in rec.stop
    rec = await healer.recover("t", "flaky_read", {}, ToolResult(False, "Please sign in to continue",
                                                                 {"url": "https://mail.example.com/login"}), [])
    assert rec.stop.startswith("NEED_USER:") and "never type passwords" in rec.stop


async def test_captcha_goes_to_the_captcha_rules_only(tmp_path: Path) -> None:
    tool = FlakyRead("x")
    _executor, healer, _ = make(tmp_path, [tool])
    rec = await healer.recover("t", "flaky_read", {}, ToolResult(False, "hCaptcha challenge: verify you are human"), [])
    assert rec.failure == F.CAPTCHA and not rec.recovered and tool.calls == 0
    assert "solve_captcha" in rec.hint and "always go to the user" in rec.hint


# --- benign popups ------------------------------------------------------------------------------------
def browser(tmp: Path, **opts: Any) -> FakeBrowserManager:
    return FakeBrowserManager(tmp / "page_state.json", opts)


def state(tmp: Path) -> dict[str, str]:
    return dict(json.loads((tmp / "page_state.json").read_text(encoding="utf-8")))


async def test_cookie_banner_is_rejected_never_accepted(tmp_path: Path) -> None:
    mgr = browser(tmp_path, popup="cookie")
    executor, healer, _ = make(tmp_path, fake_browser_tools(mgr))  # type: ignore[arg-type]
    await executor.execute("browser_open", {"url": "http://127.0.0.1/button.html"}, task_id="t")
    r = await executor.execute("browser_click", {"text": "Mark done"}, task_id="t")
    rec = await healer.recover("t", "browser_click", {"text": "Mark done"}, r, [])
    assert rec.recovered and state(tmp_path)["cookies"] == "rejected" and state(tmp_path)["clicked"] == "yes"


async def test_no_dismiss_when_the_policy_would_ask(tmp_path: Path) -> None:
    mgr = browser(tmp_path, popup="cookie")
    executor, healer, asked = make(tmp_path, fake_browser_tools(mgr), autonomy="ask-everything")  # type: ignore[arg-type]
    await executor.execute("browser_open", {"url": "http://127.0.0.1/button.html"}, task_id="t")
    r = await executor.execute("browser_click", {"text": "Mark done"}, task_id="t")
    asked.clear()
    rec = await healer.recover("t", "browser_click", {"text": "Mark done"}, r, [])
    assert not rec.recovered and "cookies" not in state(tmp_path)
    assert not any("Reject" in a.summary for a in asked)


async def test_payment_dialog_and_switched_off_dismiss_are_left_alone(tmp_path: Path) -> None:
    mgr = browser(tmp_path, popup="payment")
    executor, healer, _ = make(tmp_path, fake_browser_tools(mgr))  # type: ignore[arg-type]
    await executor.execute("browser_open", {"url": "http://127.0.0.1/button.html"}, task_id="t")
    r = await executor.execute("browser_click", {"text": "Mark done"}, task_id="t")
    rec = await healer.recover("t", "browser_click", {"text": "Mark done"}, r, [])
    assert not rec.recovered and state(tmp_path)["popup"] == "payment"
    mgr2 = browser(tmp_path / "b", popup="cookie")
    executor2, _, _ = make(tmp_path, fake_browser_tools(mgr2))  # type: ignore[arg-type]
    off = Healer(SelfHealConfig(dismiss_benign_popups=False), executor2, settle_s=0)
    await executor2.execute("browser_open", {"url": "http://127.0.0.1/button.html"}, task_id="t")
    r = await executor2.execute("browser_click", {"text": "Mark done"}, task_id="t")
    assert not (await off.recover("t", "browser_click", {"text": "Mark done"}, r, [])).recovered
    assert "cookies" not in state(tmp_path / "b")


async def test_stale_ref_is_relocated_by_name(tmp_path: Path) -> None:
    mgr = browser(tmp_path, moved=True)
    executor, healer, _ = make(tmp_path, fake_browser_tools(mgr))  # type: ignore[arg-type]
    await executor.execute("browser_open", {"url": "http://127.0.0.1/button.html"}, task_id="t")
    steps: list[StepRecord] = []
    r = await executor.execute("browser_click", {"ref": 1}, task_id="t")
    assert not r.ok
    rec = await healer.recover("t", "browser_click", {"ref": 1}, r, steps)
    assert rec.recovered and rec.lines == ["Button moved, found it again."]
    assert [s.tool for s in steps] == ["browser_read", "browser_click"] and steps[-1].args == {"text": "Mark done"}


# --- strategy ladder ----------------------------------------------------------------------------------
class ClickElementArgs(BaseModel):
    name: str
    window: str | None = None


class FakeClickElement(Tool[ClickElementArgs]):
    name = "click_element"
    description = "test"
    risk = Risk.ACT
    Args = ClickElementArgs

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, args: ClickElementArgs) -> ToolResult:
        self.calls.append(args.model_dump())
        return ToolResult(True, f"invoked [Button] {args.name}")


def test_rungs_are_data_driven() -> None:
    assert strategy.rung_of("browser_click") == ("web", strategy.DOM)
    assert strategy.rung_of("click_element") == ("desktop", strategy.UIA)
    assert strategy.rung_of("write_file") is None

    class UB:
        pass
    UB.__module__ = "delta.tools.user_browser"
    assert strategy.rung_of("browser_click", UB()) == ("web", strategy.UIA)
    assert strategy.next_rung("web", strategy.DOM, can_see=True) == strategy.UIA
    assert strategy.next_rung("web", strategy.UIA, can_see=False) == strategy.ASK  # no vision model: skipped
    assert strategy.next_rung("web", strategy.UIA, can_see=True) == strategy.VISION
    assert strategy.STEP_DOWN[("browser_fill", strategy.UIA)]({"field": "Name", "value": "x", "submit": True},
                                                              {}) is None  # never folds a submit into a step-down


async def test_step_down_to_ui_automation_after_repeated_failures(tmp_path: Path) -> None:
    mgr = browser(tmp_path, missing=True)
    uia = FakeClickElement()
    executor, healer, _ = make(tmp_path, [*fake_browser_tools(mgr), uia])  # type: ignore[list-item]
    await executor.execute("browser_open", {"url": "http://127.0.0.1/button.html"}, task_id="t")
    steps = [StepRecord("browser_open", {}, True, "", {"title": "Task board", "url": "http://127.0.0.1/button.html"})]
    args = {"text": "Mark done"}
    r = await executor.execute("browser_click", args, task_id="t")
    first = await healer.recover("t", "browser_click", args, r, steps)
    assert not first.recovered and not uia.calls
    second = await healer.recover("t", "browser_click", args, first.result, steps)
    assert second.recovered and uia.calls == [{"name": "Mark done", "window": "Task board"}]
    assert any("Windows UI Automation" in line for line in second.lines)


async def test_vision_rung_only_with_a_vision_model_then_ask(tmp_path: Path) -> None:
    mgr = browser(tmp_path, missing=True)
    executor, healer, _ = make(tmp_path, fake_browser_tools(mgr), cfg=SelfHealConfig(strategy_after=1))  # type: ignore[arg-type]
    healer.can_see = lambda: True
    await executor.execute("browser_open", {"url": "http://127.0.0.1/button.html"}, task_id="t")
    r = await executor.execute("browser_click", {"text": "Mark done"}, task_id="t")
    rec = await healer.recover("t", "browser_click", {"text": "Mark done"}, r, [])
    assert "look_at_screen" in rec.hint and not rec.stop
    rec = await healer.recover("t", "browser_click", {"text": "Mark done"}, r, [])
    assert rec.stop.startswith("NEED_USER:")


# --- the agent loop with self-heal on -------------------------------------------------------------------
async def test_orchestrator_heals_and_logs_one_line_per_recovery(tmp_path: Path, monkeypatch: Any) -> None:
    no_registry(monkeypatch, tmp_path)
    clue = tmp_path / "clue.txt"
    clue.write_text("The word is lantern", encoding="utf-8")
    rig = Rig(tmp_path, {"plan": [text('{"needs_tools": true, "goal": "read", "tools": ["read_file"], '
                                       '"success_criteria": ["word"]}')],
                         "chat": [call("read_file", path=str(clue)), text("DONE: lantern")]})
    rig.executor.faults = FaultInjector([{"tool": "read_file", "output": "[WinError 32] being used by another "
                                                                          "process"}])
    rig.orch.healer = Healer(SelfHealConfig(), rig.executor, EventBus(), settle_s=0)
    rig.orch.healer.engine = rig.engine
    res = await rig.orch.run(TaskRequest("read clue.txt", channel="api"))
    assert res.status == "done"
    tl = rig.engine.timeline(res.task_id)
    assert tl is not None
    assert [r["summary"] for r in tl["recoveries"]] == ["The file was in use; waited a moment and tried again."]
    assert tl["recoveries"][0]["failure"] == "file_locked"
    failed = [s for s in tl["steps"] if s["ok"] is False]
    assert failed and failed[0]["failure"] == "file_locked"


# --- model ladder and failover: opt-in, off by default --------------------------------------------------
def test_router_config_defaults_are_off_and_old_settings_load() -> None:
    s = Settings.model_validate({"llm": {"routes": {"chat": ["groq"]}}})
    assert s.router.ladder == [] and s.router.fallbacks == [] and s.selfheal.enabled and s.undo.enabled
    assert ModelLadder(s.router, {"groq": object()}).enabled is False


async def test_default_router_order_is_unchanged() -> None:
    a, b = FakeProvider("a", [text("from a")]), FakeProvider("b", [text("from b")])
    r = ModelRouter({"a": a, "b": b}, {"chat": ["a", "b"]})
    assert (await r.complete([{"role": "user", "content": "hi"}])).content == "from a"
    assert r.model_calls == {"a": 1} and not b.calls


async def test_ladder_prefers_the_cheap_model_for_observe_steps_and_escalates() -> None:
    providers = {"small": FakeProvider("small", [text("small")]), "main": FakeProvider("main", [text("main")])}
    ladder = ModelLadder(RouterConfig(ladder=["small", "nope"], escalate_after=2), providers)
    assert ladder.rungs == ["small"]
    read_ok = [StepRecord("read_file", {}, True, "", {})]
    assert ladder.step_kind(read_ok, 0) == "observe"
    assert ladder.step_kind([StepRecord("write_file", {}, True, "", {})], 0) == "act"
    assert ladder.pick("t", "observe") == "small" and ladder.pick("t", "act") is None
    ladder.record("t", False)
    ladder.record("t", False)
    assert ladder.pick("t", "observe") is None  # escalated to the normal route, and stays there
    r = ModelRouter(providers, {"chat": ["main"]})
    assert (await r.complete([{"role": "user", "content": "x"}], prefer="small")).content == "small"
    assert r.model_calls == {"small": 1}


async def test_fallbacks_only_on_outage_and_only_listed_providers() -> None:
    main = FakeProvider("main", [LLMError("main: rate limited", kind="rate_limit", retry_after_s=999)])
    backup = FakeProvider("backup", [text("from backup")])
    paid = FakeProvider("paid", [text("never")])
    r = ModelRouter({"main": main, "backup": backup, "paid": paid}, {"chat": ["main"]})
    with pytest.raises(AllProvidersFailedError):  # no fallbacks configured: nothing else is tried
        await r.complete([{"role": "user", "content": "x"}])
    main.replies = [LLMError("main: rate limited", kind="rate_limit", retry_after_s=999)]
    r.fallbacks = ["backup"]
    resp = await r.complete([{"role": "user", "content": "x"}])
    assert resp.content == "from backup" and not paid.calls and "fallback provider backup" in r.fallback_note()
    main.replies = [LLMError("main: not signed in")]  # not an outage: no failover
    r.reset_meter()
    with pytest.raises(AllProvidersFailedError):
        await r.complete([{"role": "user", "content": "x"}])


async def test_bootstrap_leaves_routing_alone_by_default(tmp_path: Path, monkeypatch: Any) -> None:
    no_registry(monkeypatch, tmp_path)
    from delta.bootstrap import build

    c = build(Settings(data_dir=tmp_path))
    assert c.orchestrator.ladder is None and c.router.fallbacks == []
    assert c.orchestrator.healer is not None and c.orchestrator.executor.undo is not None
    assert c.orchestrator.executor.faults is None  # failure injection is never on in a real Delta


# --- undo ---------------------------------------------------------------------------------------------
def undo_store(tmp: Path, **cfg: Any) -> UndoStore:
    return UndoStore(tmp / "data" / "undo", UndoConfig(**cfg), [tmp / "work"])


def test_undo_modified_created_moved_and_recycled(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    store = undo_store(tmp_path)
    a, b, c = work / "a.txt", work / "b.txt", work / "c.txt"
    a.write_text("old a", encoding="utf-8")
    c.write_text("old c", encoding="utf-8")
    store.before("t1", 1, "write_file", {"path": str(a)})
    a.write_text("new a", encoding="utf-8")
    store.before("t1", 2, "write_file", {"path": str(b)})  # created
    b.write_text("new b", encoding="utf-8")
    store.before("t1", 3, "move_path", {"source": str(c), "destination": str(work / "sub" / "c.txt")})
    (work / "sub").mkdir()
    c.rename(work / "sub" / "c.txt")
    summary = store.summary("t1")
    assert summary["available"] and summary["files"] == 3 and summary["steps"] == [1, 2, 3]
    result = store.restore("t1")
    assert result["ok"] and not result["skipped"]
    assert a.read_text(encoding="utf-8") == "old a" and not b.exists() and c.read_text(encoding="utf-8") == "old c"
    assert not (work / "sub" / "c.txt").exists()
    displaced = list((store.folder("t1") / "displaced").iterdir())
    assert {p.name.split("-", 1)[1] for p in displaced} == {"a.txt", "b.txt"}  # nothing deleted for good
    assert store.summary("t1")["restored"] and not store.restore("t1")["ok"]


def test_undo_skips_big_files_outside_folders_and_failed_steps(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    store = undo_store(tmp_path, max_file_mb=0.0001)
    big = work / "big.bin"
    big.write_bytes(b"x" * 1000)
    store.before("t", 1, "write_file", {"path": str(big)})
    info = store.summary("t")
    assert not info["available"] and "bigger than" in info["skipped"][0]["why"]
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x", encoding="utf-8")
    assert store.before("t2", 1, "write_file", {"path": str(outside)}) == []
    small = work / "s.txt"
    small.write_text("s", encoding="utf-8")
    store2 = undo_store(tmp_path / "x")
    (tmp_path / "x" / "work").mkdir(parents=True)
    s2 = tmp_path / "x" / "work" / "s.txt"
    s2.write_text("s", encoding="utf-8")
    ids = store2.before("t3", 1, "edit_file", {"path": str(s2)})
    store2.after("t3", ids, ok=False)
    assert store2.entries("t3") == []


def test_undo_restore_follows_the_current_policy_and_prunes(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    store = undo_store(tmp_path)
    f = work / "f.txt"
    f.write_text("old", encoding="utf-8")
    store.before("t", 1, "edit_file", {"path": str(f)})
    f.write_text("new", encoding="utf-8")
    narrower = PolicyEngine(Boundaries(allowed_paths=[str(tmp_path / "other")]))
    result = store.restore("t", narrower)
    assert not result["ok"] and "allowed folders" in result["skipped"][0]["why"]
    assert f.read_text(encoding="utf-8") == "new"
    assert store.restore("t", PolicyEngine(Boundaries(allowed_paths=[str(work)])))["ok"]
    assert f.read_text(encoding="utf-8") == "old"
    assert store.prune(now=10**12) == 1 and not store.folder("t").exists()


async def test_executor_keeps_an_undo_copy_and_the_timeline_shows_it(tmp_path: Path, monkeypatch: Any) -> None:
    no_registry(monkeypatch, tmp_path)
    f = tmp_path / "notes.txt"
    f.write_text("v1\n", encoding="utf-8")
    rig = Rig(tmp_path, {"plan": [text('{"needs_tools": true, "goal": "append", "tools": ["write_file"], '
                                       '"success_criteria": ["done"]}')],
                         "chat": [call("write_file", path=str(f), content="v2\n", append=True), text("DONE: ok")]})
    undo = UndoStore(tmp_path / "undo", UndoConfig(), [tmp_path])
    rig.executor.undo = rig.engine.undo = undo
    res = await rig.orch.run(TaskRequest("append v2", channel="api"))
    assert res.status == "done" and f.read_text(encoding="utf-8") == "v1\nv2\n"
    tl = rig.engine.timeline(res.task_id)
    assert tl is not None and tl["undo"]["available"] and any(s["undo"] for s in tl["steps"])
    assert undo.restore(res.task_id, rig.executor.policy)["ok"] and f.read_text(encoding="utf-8") == "v1\n"


def test_fault_injector_is_explicit_and_counted() -> None:
    inj = FaultInjector([{"tool": "read_file", "times": 1, "skip": 1, "output": "boom"}])
    assert inj.take("read_file") is None  # skipped once
    hit = inj.take("read_file")
    assert hit is not None and not hit.ok and hit.output == "boom"
    assert inj.take("read_file") is None and inj.fired == ["read_file"]


# --- undo from the dashboard, the CLI and a phone -------------------------------------------------------
class Guard:
    def __init__(self, locked: bool = False, has_pin: bool = True, unlocked: bool = False) -> None:
        self.locked, self.has_pin, self._unlocked = locked, has_pin, unlocked

    def locked_message(self) -> str:
        return "Remote control is locked."

    def is_unlocked(self, user: str) -> bool:
        return self._unlocked


def test_undo_from_a_phone_needs_the_pin(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from delta.selfheal.actions import undo_task

    (tmp_path / "work").mkdir()
    f = tmp_path / "work" / "f.txt"
    f.write_text("old", encoding="utf-8")
    store = undo_store(tmp_path)
    store.before("t9", 1, "edit_file", {"path": str(f)})
    f.write_text("new", encoding="utf-8")
    executor = SimpleNamespace(undo=store, policy=PolicyEngine(Boundaries(allowed_paths=[str(tmp_path / "work")])))
    c = SimpleNamespace(orchestrator=SimpleNamespace(executor=executor), guard=Guard(locked=True), audit=None)
    assert not undo_task(c, "t9", "telegram", "42")["ok"]
    c.guard = Guard(has_pin=False)
    assert "PIN" in undo_task(c, "t9", "telegram", "42")["message"]
    c.guard = Guard()
    assert "/unlock" in undo_task(c, "t9", "telegram", "42")["message"] and f.read_text(encoding="utf-8") == "new"
    c.guard = Guard(unlocked=True)
    assert undo_task(c, "t9", "telegram", "42")["ok"] and f.read_text(encoding="utf-8") == "old"
    assert not undo_task(c, "../etc", "api")["ok"]


def test_undo_api_route_and_cli(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    import argparse

    from fastapi.testclient import TestClient

    from delta.api.server import create_app, load_token
    from delta.bootstrap import build
    from delta.selfheal.cli import run_cli

    no_registry(monkeypatch, tmp_path)
    allowed = tmp_path / "work"
    allowed.mkdir()
    settings = Settings(data_dir=tmp_path / "data", boundaries=Boundaries(allowed_paths=[str(allowed)]))
    c = build(settings)
    f = allowed / "f.txt"
    f.write_text("old", encoding="utf-8")
    c.orchestrator.executor.undo.before("abc1", 1, "edit_file", {"path": str(f)})
    f.write_text("new", encoding="utf-8")
    token = load_token(settings.data_dir)
    with TestClient(create_app(c, token)) as tc:
        tc.headers["Authorization"] = f"Bearer {token}"
        assert tc.post("/api/tasks/abc1/undo").json()["ok"]
        assert f.read_text(encoding="utf-8") == "old"
        assert not tc.post("/api/tasks/abc1/undo").json()["ok"]  # already undone
    import delta.engine.cli

    monkeypatch.setattr(delta.engine.cli, "_service_running", lambda s: False)
    c.orchestrator.executor.undo.before("abc2", 1, "edit_file", {"path": str(f)})
    f.write_text("newer", encoding="utf-8")
    assert run_cli(settings, argparse.Namespace(id=None, json=False)) == 0
    assert "abc2" in capsys.readouterr().out
    assert run_cli(settings, argparse.Namespace(id="abc2", json=False)) == 0
    assert f.read_text(encoding="utf-8") == "old"
