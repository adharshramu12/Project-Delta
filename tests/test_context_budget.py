"""Long multi-step tasks must stay inside free-tier request limits.

Live finding (hard test, wiki-multihop): two full Wikipedia pages in the history exceeded Groq's
7,000-token-per-request limit (HTTP 413) and the task failed after doing every step right.
"""

from fakes import FakeProvider, call, js, text
from test_orchestrator import make

from delta.core.orchestrator import CONTEXT_BUDGET_CHARS, OLD_RESULT_CHARS, compact_history
from delta.core.state import TaskRequest
from delta.llm.schemas import LLMError
from delta.tools.user_browser import select_lines


def history(n_pages: int, page_chars: int) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    for i in range(n_pages):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "browser_read", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "name": "browser_read", "content": "x" * page_chars})
    return msgs


def size(msgs: list[dict]) -> int:
    return sum(len(str(m.get("content") or "")) for m in msgs)


def test_old_results_trimmed_newest_kept():
    msgs = history(4, 6000)
    compact_history(msgs, CONTEXT_BUDGET_CHARS)
    tools = [m for m in msgs if m["role"] == "tool"]
    assert all(len(t["content"]) <= OLD_RESULT_CHARS + 60 for t in tools[:2])
    assert len(tools[-1]["content"]) == 6000  # the page the model is acting on stays whole
    assert len(msgs) == 10  # nothing dropped: tool-call ids stay paired


def test_budget_enforced_even_for_huge_latest_result():
    msgs = history(2, 40000)
    compact_history(msgs, CONTEXT_BUDGET_CHARS)
    assert size(msgs) <= CONTEXT_BUDGET_CHARS + 200


def test_focus_returns_only_relevant_lines():
    page = [f"filler line {i}" for i in range(500)]
    page[250] = "Charles Babbage was born on 26 December 1791 in London"
    out = select_lines(page, "born", 7000)
    assert "1791" in out and "filler line 10" not in out and len(out) < 500
    assert "nothing on this page mentions" in select_lines(page, "zebra", 7000)


def test_loop_detection_ignores_reads_between_tries():
    """Live finding (hard test, youtube): the model clicked 'Search' again and again."""
    from delta.core.orchestrator import stuck_in_loop
    from delta.core.state import StepRecord

    click = StepRecord("browser_click", {"text": "Search"}, True, "", {})
    read = StepRecord("browser_read", {}, True, "", {})
    assert not stuck_in_loop([click, read, click])
    assert stuck_in_loop([click, read, click, read, click])
    other = StepRecord("browser_fill", {"field": "Search", "value": "x"}, True, "", {})
    assert not stuck_in_loop([click, click, other])


async def test_malformed_tool_call_is_retried(tmp_path):
    """Live finding (hard test, notebooklm): Groq returned 400 'Failed to parse tool call arguments'."""
    orch, _, _ = make(tmp_path, {"plan": [], "chat": [], "verify": []})
    chats = []

    def handler(task, messages):
        if task == "plan":
            return js({"needs_tools": True, "tools": ["path_info"], "success_criteria": ["answered"]})
        if task == "verify":
            return js({"passed": True, "reason": "ok"})
        chats.append(messages[-1]["content"])
        if len(chats) == 1:
            raise LLMError("400 Failed to parse tool call arguments as JSON", kind="bad_tool_call")
        if len(chats) == 2:
            return call("path_info", path=str(tmp_path))
        return text("done")

    orch.router.providers["fake"] = FakeProvider("fake", handler=handler)
    r = await orch.run(TaskRequest("check folder"))
    await orch.drain()
    assert r.status == "done" and "not valid JSON" in chats[1]


def test_screenshot_tool_hidden_without_a_vision_model():
    from delta.llm.router import ModelRouter
    from delta.tools.registry import ToolRegistry
    from delta.tools.screen import LookAtScreen

    class TextOnly(FakeProvider):
        def supports(self, task):
            return task != "vision"

    no_vision = ToolRegistry([LookAtScreen(ModelRouter({"groq": TextOnly("groq")}, {"vision": ["groq"]}))])
    assert no_vision.specs() == []
    with_vision = ToolRegistry([LookAtScreen(ModelRouter({"nim": FakeProvider("nim")}, {"vision": ["nim"]}))])
    assert [s["function"]["name"] for s in with_vision.specs()] == ["look_at_screen"]


async def test_too_large_error_triggers_harder_trim_and_retry(tmp_path):
    orch, _, _ = make(tmp_path, {
        "plan": [js({"needs_tools": False, "answer": "unused"})], "chat": [], "verify": [],
    })
    attempts = []

    def handler(task, messages):
        if task == "plan":
            return js({"needs_tools": True, "tools": ["read_file"], "success_criteria": ["answered"]})
        if task == "verify":
            return js({"passed": True, "reason": "ok"})
        attempts.append(size(messages))
        if len(attempts) == 1:
            return call("read_file", path=str(tmp_path / "big.txt"))
        if len(attempts) == 2:
            raise LLMError("413", kind="too_large")
        return text("done")

    (tmp_path / "big.txt").write_text("y" * 30000)
    orch.router.providers["fake"] = FakeProvider("fake", handler=handler)
    r = await orch.run(TaskRequest("read big file"))
    await orch.drain()
    assert r.status == "done", r.answer
    assert attempts[2] < attempts[1]  # retried with a smaller request


def test_only_the_newest_screenshot_is_kept_in_the_history():
    from delta.core.orchestrator import compact_history

    shot = lambda: {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:x"}}]}  # noqa: E731
    msgs = [{"role": "system", "content": "s"}, shot(), {"role": "tool", "content": "ok"}, shot()]
    compact_history(msgs, 18000)
    assert msgs[1]["content"] == "[earlier screenshot removed to save tokens]"
    assert isinstance(msgs[3]["content"], list)
