"""Batch 1: browser speed and token savings.

Snapshot diffs, event-driven settle, leaner history, screenshots only as a fallback, and the
per-task token/time meter with an optional budget. Fakes only: runs anywhere without a browser."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from fakes import FakeProvider, call, js, text
from test_browser_refs import FakeElement, page_elements, setup
from test_orchestrator import make

from delta.config import ProviderConfig, Settings
from delta.core import orchestrator as orch_mod
from delta.core.orchestrator import (
    OLD_RESULT_CHARS,
    UNCHANGED_TEXT,
    compact_history,
    drop_held_lines,
    held_element_lines,
    same_text_as_before,
)
from delta.core.state import TaskRequest, TaskResult
from delta.llm import usage
from delta.llm.cli_providers import CLIProvider
from delta.llm.router import ModelRouter
from delta.llm.schemas import LLMResponse
from delta.tools import browser, snapshot
from delta.tools.base import Risk, Tool, ToolResult
from delta.tools.browser import (
    BrowserClick,
    BrowserScroll,
    ReadArgs,
    RefClickArgs,
    ScrollArgs,
    settle,
)

# --- 1. snapshot diffs -----------------------------------------------------------------------------


def big_page() -> list[FakeElement]:
    """A page worth diffing: the usual elements plus a row of links."""
    return [*page_elements(), *(FakeElement(20 + i, "link", f"Article number {i}") for i in range(15))]


async def test_same_page_action_sends_only_the_changes(tmp_path):
    elements = big_page()
    mgr, page = setup(tmp_path, elements)
    first = await BrowserScroll(mgr).run(ScrollArgs())
    assert "Page: Example — https://example.com/" in first.output  # nothing held yet: full listing
    page.elements = [*elements, FakeElement(8, "button", "Open report"), FakeElement(9, "button", "Star message")]
    second = await BrowserClick(mgr).run(RefClickArgs(ref=1))
    assert second.ok and second.evidence["snapshot"] == "diff"
    assert "Page changes: Example — https://example.com/" in second.output
    assert '+ [8] button "Open report"' in second.output and '+ [9] button "Star message"' in second.output
    assert '[2] button "Sign in"' not in second.output  # unchanged: not sent again
    assert len(second.output) < len(first.output)
    assert {1, 2, 8, 9} <= set(mgr.refs)  # every ref on the page stays valid, shown or not


async def test_removed_and_changed_elements_and_stale_refs(tmp_path):
    elements = big_page()
    mgr, page = setup(tmp_path, elements)
    await BrowserScroll(mgr).run(ScrollArgs())
    elements[1].name = "Send"  # [2] relabelled after it was listed
    page.elements = [e for e in elements if e.ref != 6]  # [6] gone
    out = (await BrowserScroll(mgr).run(ScrollArgs())).output
    assert '~ [2] button "Send"' in out and '- [6] textbox "Message"' in out
    # The risk check reads the element as it is now, not as first listed.
    assert BrowserClick(mgr).risk_for(RefClickArgs(ref=2)) == Risk.EXTERNAL
    stale = await BrowserClick(mgr).run(RefClickArgs(ref=6))
    assert not stale.ok and "no longer on the page" in stale.output


async def test_navigation_and_every_n_steps_send_the_full_listing(tmp_path):
    mgr, page = setup(tmp_path, big_page())
    mgr.full_every = 3
    kinds = []
    for _ in range(5):
        kinds.append((await BrowserScroll(mgr).run(ScrollArgs())).evidence["snapshot"])
    assert kinds == ["full", "diff", "diff", "full", "diff"]
    page.url = "https://example.com/next"  # navigated
    assert (await BrowserScroll(mgr).run(ScrollArgs())).evidence["snapshot"] == "full"
    page.url = "https://example.com/next#section"  # same document, only the fragment moved
    assert (await BrowserScroll(mgr).run(ScrollArgs())).evidence["snapshot"] == "diff"
    # open/search/back always send in full, even to the same address
    mgr.snap.has_base = True
    out = await browser._observed(page, mgr, "opened", force_full=True)
    assert out.evidence["snapshot"] == "full"


async def test_diff_bigger_than_full_falls_back_and_switch_off_works(tmp_path):
    mgr, page = setup(tmp_path, page_elements())
    await BrowserScroll(mgr).run(ScrollArgs())
    page.elements = [FakeElement(100 + i, "link", f"Brand new link {i}") for i in range(40)]  # whole page swapped
    out = await BrowserScroll(mgr).run(ScrollArgs())
    assert out.evidence["snapshot"] == "full" and out.output.count("\n- [") == 0
    mgr.diff_snapshots = False
    assert (await BrowserScroll(mgr).run(ScrollArgs())).evidence["snapshot"] == "full"


async def test_new_password_field_in_a_diff_never_shows_a_value(tmp_path):
    elements = big_page()
    mgr, page = setup(tmp_path, elements)
    await BrowserScroll(mgr).run(ScrollArgs())
    pw = FakeElement(9, "textbox", "Password", type="password", editable=True)
    pw.value = "hunter2"
    page.elements = [*elements, pw]
    out = (await BrowserScroll(mgr).run(ScrollArgs())).output
    assert '+ [9] textbox "Password" (password)' in out and "hunter2" not in out


async def test_failed_listing_resets_so_the_next_one_is_full(tmp_path):
    mgr, page = setup(tmp_path, page_elements())
    await BrowserScroll(mgr).run(ScrollArgs())
    real = page.evaluate

    async def broken(script: str) -> Any:
        if script == browser.ELEMENTS_JS:
            raise RuntimeError("context destroyed")
        return await real(script)

    page.evaluate = broken  # type: ignore[method-assign]
    assert (await BrowserScroll(mgr).run(ScrollArgs())).ok
    page.evaluate = real  # type: ignore[method-assign]
    assert (await BrowserScroll(mgr).run(ScrollArgs())).evidence["snapshot"] == "full"


def test_listing_is_found_only_where_an_action_puts_it():
    action = 'OK: clicked \'x\'\n\nPage changes: T — u\n+ [3] button "B"\n\nAFTER (browser_read):\nPage: 2 of 9'
    start, end, kind = snapshot.find_listing(action) or (0, 0, "")
    assert kind == "diff" and action[start:end].endswith('"B"')
    assert snapshot.find_listing("OK: Title — url\n\nPage: 2 of 9\nmore text") is not None  # shape matches
    assert snapshot.find_listing("OK: some page text\nPage: 2 of 9") is None


# --- 2. event-driven settle ------------------------------------------------------------------------


class _StillPage:
    def __init__(self, dom_ms: float = 0.0) -> None:
        self.dom_ms = dom_ms
        self.scripts: list[str] = []
        self.waits: list[str] = []

    async def wait_for_load_state(self, state: str, **kw: Any) -> None:
        self.waits.append(state)

    async def evaluate(self, js: str) -> bool:
        self.scripts.append(js)
        if "MutationObserver" in js:
            await asyncio.sleep(self.dom_ms / 1000)
        return True


async def test_settle_on_a_still_page_is_quick_and_uses_no_networkidle():
    page = _StillPage()
    start = time.monotonic()
    await settle(page)
    assert time.monotonic() - start < 0.5
    assert "networkidle" not in page.waits
    assert any("MutationObserver" in s and str(browser.DOM_QUIET_MS) in s for s in page.scripts)


async def test_settle_waits_for_the_dom_to_go_quiet_but_never_past_the_cap():
    page = _StillPage(dom_ms=300)  # a page still rendering for 300 ms
    start = time.monotonic()
    await settle(page)
    assert 0.28 <= time.monotonic() - start < browser.SETTLE_IDLE_MS / 1000
    busy = _StillPage(dom_ms=10_000)  # never quiet: the cap (idle_ms) ends the wait
    start = time.monotonic()
    await settle(busy, idle_ms=300)
    assert time.monotonic() - start < 1.5


async def test_back_waits_for_commit_not_load_events(tmp_path):
    """A page restored from the back-forward cache fires no load events: waiting for
    domcontentloaded made browser_back hang for its full 15 s."""
    from delta.tools.browser import BrowserBack

    mgr, page = setup(tmp_path, big_page())
    seen: list[str] = []

    async def go_back(wait_until: str = "load", timeout: int = 0) -> None:  # noqa: ASYNC109
        seen.append(wait_until)

    page.go_back = go_back  # type: ignore[attr-defined]
    out = await BrowserBack(mgr).run(BrowserBack.Args())
    assert out.ok and seen == ["commit"] and out.evidence["snapshot"] == "full"


async def test_settle_asks_the_document_before_waiting_for_load_events():
    """Back-forward cache restores fire no load events: a parsed document needs no load-state wait."""

    class Restored(_StillPage):
        async def evaluate(self, js: str) -> Any:
            if js == "document.readyState":
                return "complete"
            return await super().evaluate(js)

        async def wait_for_load_state(self, state: str, **kw: Any) -> None:
            await asyncio.sleep(10)  # would never fire

    page = Restored()
    start = time.monotonic()
    await settle(page)
    assert time.monotonic() - start < 1.0


# --- 3. leaner history -----------------------------------------------------------------------------


def _browser_history(results: list[str]) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    for i, content in enumerate(results):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "browser_click", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "name": "browser_click", "content": content})
    return msgs


def _full(n: int, url: str = "https://a.example/") -> str:
    lines = "\n".join(f'[{i}] link "Result {i} ' + "x" * 30 + '"' for i in range(n, n + 25))
    return f"OK: clicked 'r', now at {url}\n\nPage: A — {url}\n{lines}\n\nAFTER (browser_read):\n" + "born 1815 " * 80


def _diff(ref: int) -> str:
    return (f"OK: clicked 'more'\n\nPage changes: A — https://a.example/\n+ [{ref}] button \"More {ref}\"\n"
            "\nAFTER (browser_read):\n"
            + "text " * 200)


def test_superseded_full_listing_becomes_a_stub_and_the_current_one_stays_whole():
    msgs = _browser_history([_full(1, "https://old.example/"), _full(100), _diff(200), _diff(201), _diff(202)])
    compact_history(msgs, 100_000)
    tools = [m for m in msgs if m["role"] == "tool"]
    assert "superseded" in tools[0]["content"] and "[1] link" not in tools[0]["content"]
    assert "old.example" in tools[0]["content"]  # the stub still says which page it was
    assert '[100] link "Result 100' in tools[1]["content"] and '[124] link' in tools[1]["content"]  # base whole
    assert '+ [200] button' in tools[2]["content"]  # diffs after the base are kept although old
    assert len(tools[1]["content"]) < len(_full(100))  # its long page text was still cut
    assert len(msgs) == 12  # nothing dropped: tool-call ids stay paired


def test_over_budget_old_listings_go_to_stubs_before_the_newest_results():
    msgs = _browser_history([_full(1), _diff(200), _diff(201), _diff(202)])
    compact_history(msgs, 1500)
    tools = [m for m in msgs if m["role"] == "tool"]
    assert "superseded" in tools[0]["content"]
    assert len(msgs) == 10


def test_plain_results_are_trimmed_exactly_as_before():
    msgs = _browser_history(["OK: some page text\n\nPage: 2 of 9\n" + "y" * 5000] * 3)
    for m in msgs:
        if m["role"] == "tool":
            m["name"] = "browser_read"  # not an action: never treated as a listing
    compact_history(msgs, 100_000)
    tools = [m for m in msgs if m["role"] == "tool"]
    assert len(tools[0]["content"]) <= OLD_RESULT_CHARS + 60 and "superseded" not in tools[0]["content"]
    assert len(tools[-1]["content"]) > 5000


def test_after_read_drops_element_lines_already_held():
    msgs = _browser_history([_full(1), _diff(200)])
    held = held_element_lines(msgs)
    assert '[1] link "Result 1 ' + "x" * 30 + '"' in held and '[200] button "More 200"' in held
    obs = 'A — https://a.example/\nborn 1815\n[200] button "More 200"\n[999] link "Only here"'
    assert drop_held_lines(obs, held) == 'A — https://a.example/\nborn 1815\n[999] link "Only here"'


def test_unchanged_page_text_is_sent_once_per_pair_of_steps():
    text, prev = same_text_as_before("Born 1815", "https://a.example/p", None)
    assert text == "Born 1815"
    text, prev = same_text_as_before("Born 1815", "https://a.example/p#bio", prev)
    assert text == UNCHANGED_TEXT and prev is None  # the previous result still holds it in full
    text, prev = same_text_as_before("Born 1815", "https://a.example/p", prev)
    assert text == "Born 1815"  # compared only with a result that carried the text itself
    text, _ = same_text_as_before("Born 1815", "https://b.example/", prev)
    assert text == "Born 1815"  # another page: always in full


async def test_agent_loop_sends_repeated_page_text_as_a_note(tmp_path, monkeypatch):
    import delta.config

    monkeypatch.setattr(orch_mod, "known_folder", lambda name: tmp_path)
    monkeypatch.setattr(delta.config, "known_folder", lambda name: tmp_path)
    orch, _, _ = make(tmp_path, {
        "plan": [js({"needs_tools": True, "goal": "g", "steps": ["s"], "success_criteria": ["c"],
                     "tools": ["browser_scroll"]})],
        "chat": [call("browser_scroll"), call("browser_scroll"), text("DONE: read it")],
        "verify": [js({"passed": True, "reason": "ok"})],
    })
    for name in ("browser_scroll", "browser_read"):
        orch.executor.registry.register(_FakeBrowserTool(name))
    r = await orch.run(TaskRequest("read the page"))
    assert r.status == "done", r.answer
    sent = orch.router.providers["fake"].calls  # type: ignore[attr-defined]
    last_chat = [m for task, m in sent if task == "chat"][-1]
    tool_msgs = [m["content"] for m in last_chat if m.get("role") == "tool"]
    assert "born 1815" in tool_msgs[0] and UNCHANGED_TEXT in tool_msgs[1]
    assert [s.output for s in r.steps if s.tool == "browser_read"][1] == "Page text\nborn 1815"  # proof keeps it


# --- 4. screenshots only when needed ---------------------------------------------------------------


class _FakeBrowserTool(Tool[ReadArgs]):
    risk = Risk.READ
    Args = ReadArgs
    description = "fake"

    def __init__(self, name: str) -> None:
        self.name = name  # type: ignore[misc]

    async def run(self, args: ReadArgs) -> ToolResult:
        return ToolResult(True, "Page text\nborn 1815", {"url": "https://a.example/", "title": "A"})


async def test_browser_actions_are_observed_from_the_page_text_not_a_screenshot(tmp_path):
    orch, _, _ = make(tmp_path, {})
    for name in ("browser_click", "browser_read"):
        orch.executor.registry.register(_FakeBrowserTool(name))
    shots: list[str] = []

    async def no_shot(*a: Any, **k: Any) -> tuple[str | None, str]:
        shots.append("taken")
        return "b64", "window"

    orch._screenshot = no_shot  # type: ignore[method-assign]
    orch._brain_sees = lambda: True  # type: ignore[method-assign]
    seen = await orch._observe("browser_click", "t1", [], "when was ada born", {"title": "A"})
    assert seen is not None and seen[0] == "browser_read" and seen[3] is None and not shots


# --- 5. token and time meter -----------------------------------------------------------------------


class _NoUsageCLI(CLIProvider):
    def supports(self, task: str) -> bool:
        return True

    async def complete(self, task: str, messages: list, tools: Any, json_mode: bool = False) -> LLMResponse:
        return LLMResponse(content="hi", provider=self.name)


async def test_meter_reported_estimated_and_not_reported():
    reported = FakeProvider("api", replies=[LLMResponse(content="a", usage={"input": 100, "output": 7})])
    r = ModelRouter({"api": reported}, {"chat": ["api"]})
    await r.complete([{"role": "user", "content": "hello"}])
    assert usage.summary(r.meter)["source"] == "reported" and usage.summary(r.meter)["input"] == 100

    r = ModelRouter({"api": FakeProvider("api", replies=[text("x" * 40)])}, {"chat": ["api"]})
    await r.complete([{"role": "user", "content": "y" * 400}])
    s = usage.summary(r.meter)
    assert s["source"] == "estimate" and s["input"] == 100 and s["output"] == 10 and "(estimate)" in s["label"]

    cli = _NoUsageCLI("claude", ProviderConfig())
    r = ModelRouter({"claude": cli}, {"chat": ["claude"]})
    await r.complete([{"role": "user", "content": "y" * 400}])
    s = usage.summary(r.meter)
    assert s["source"] == "not reported" and s["label"] == "tokens not reported by this AI"
    assert usage.used_for_budget(r.meter) > 0  # the budget still sees a rough size


def test_summary_mixed_and_none():
    assert usage.summary({})["source"] == "none"
    s = usage.summary({"calls": 2, "input": 10, "output": 2, "estimated_calls": 1, "est_input": 5, "est_output": 1})
    assert s["source"] == "mixed" and s["input"] == 15 and "1 call(s) estimated" in s["label"]
    assert usage.estimate_tokens([{"role": "user", "content": [
        {"type": "text", "text": "abcd"}, {"type": "image_url", "image_url": {"url": "data:..."}}]}]) == 1 + 1000


@pytest.fixture
def any_os(monkeypatch, tmp_path):
    """Windows' known folders come from the registry; elsewhere use a temp folder (Linux dev runs)."""
    import delta.config

    monkeypatch.setattr(orch_mod, "known_folder", lambda name: tmp_path)
    monkeypatch.setattr(delta.config, "known_folder", lambda name: tmp_path)


@pytest.mark.usefixtures("any_os")
async def test_task_result_carries_tokens_time_and_the_api_shows_them(tmp_path):
    from delta.api.server import _result_json

    orch, _, _ = make(tmp_path, {"plan": [js({"needs_tools": False, "answer": "Hello!"})]})
    r = await orch.run(TaskRequest("what is 2+2"))
    assert r.status == "done" and r.wall_s >= 0 and r.usage["calls"] == 1 and r.usage["source"] == "estimate"
    out = _result_json(r)
    assert out["usage"] == r.usage and out["wall_s"] == r.wall_s and "tokens" in out
    json.dumps(out)  # stored with the chat message as JSON


@pytest.mark.usefixtures("any_os")
async def test_token_budget_pauses_and_asks(tmp_path):
    orch, _, _ = make(tmp_path, {
        "plan": [js({"needs_tools": True, "goal": "g", "steps": ["s"], "success_criteria": ["c"]})],
        "chat": [call("list_dir", path=str(tmp_path)), text("DONE: ok")],
        "verify": [js({"passed": True, "reason": "ok"})],
    })
    orch.s.agent.token_budget = 1
    r = await orch.run(TaskRequest("list my folder"))
    assert r.status == "needs_user" and r.options == ["Keep going", "Stop here"]
    assert "limit of 1 tokens per task" in r.answer and not r.steps


@pytest.mark.usefixtures("any_os")
async def test_no_budget_by_default(tmp_path):
    assert Settings().agent.token_budget == 0
    orch, _, _ = make(tmp_path, {
        "plan": [js({"needs_tools": True, "goal": "g", "steps": ["s"], "success_criteria": ["c"]})],
        "chat": [call("list_dir", path=str(tmp_path)), text("DONE: ok")],
        "verify": [js({"passed": True, "reason": "ok"})],
    })
    r = await orch.run(TaskRequest("list my folder"))
    assert r.status == "done", r.answer


def test_old_settings_files_still_load():
    s = Settings.model_validate({"agent": {"name": "Delta", "max_steps": 15}, "browser": {"mode": "delta-profile"}})
    assert s.agent.token_budget == 0 and s.browser.snapshot_diffs is True and s.browser.full_snapshot_every == 8
    assert TaskResult("t", "done", "a").usage == {} and TaskResult("t", "done", "a").wall_s == 0.0


def test_orchestrator_module_exports_still_there():
    assert orch_mod.CONTEXT_BUDGET_CHARS and orch_mod.compact_history
