"""Regression tests for problems found in the first real-model run (Groq free tier)."""

import time

from fakes import FakeProvider, call, js, text
from test_orchestrator import make

from delta.core.state import TaskRequest
from delta.llm.providers import _retry_after, pick_model
from delta.llm.router import ModelRouter
from delta.llm.schemas import LLMError
from delta.memory import extractor


async def test_short_rate_limit_waits_and_retries_same_provider():
    groq = FakeProvider("groq", [LLMError("429", kind="rate_limit", retry_after_s=0.05), text("ok")])
    nim = FakeProvider("nim", [text("should not be used")])
    router = ModelRouter({"groq": groq, "nim": nim}, {"chat": ["groq", "nim"]})
    t = time.perf_counter()
    assert (await router.complete([])).content == "ok"
    assert time.perf_counter() - t >= 0.05
    assert nim.calls == [] and router.breakers["groq"].failures == 0


async def test_long_rate_limit_falls_back_without_hurting_health():
    groq = FakeProvider("groq", [LLMError("429", kind="rate_limit", retry_after_s=600)])
    nim = FakeProvider("nim", [text("from nim")])
    router = ModelRouter({"groq": groq, "nim": nim}, {"chat": ["groq", "nim"]})
    assert (await router.complete([])).content == "from nim"
    assert router.breakers["groq"].failures == 0


async def test_unreachable_local_model_is_skipped_for_a_while():
    ollama = FakeProvider("ollama", [LLMError("refused", kind="unreachable")] * 5, local=True)
    groq = FakeProvider("groq", [text("a"), text("b"), text("c")])
    router = ModelRouter({"ollama": ollama, "groq": groq}, {"chat": ["ollama", "groq"]})
    for _ in range(3):
        await router.complete([])
    assert len(ollama.calls) == 1  # tried once, then skipped instead of failing on every call


def test_pick_replacement_model_from_real_groq_list():
    groq_nov_2026 = ["allam-2-7b", "canopylabs/orpheus-v1-english", "meta-llama/llama-prompt-guard-2-22m",
                     "openai/gpt-oss-120b", "openai/gpt-oss-20b", "openai/gpt-oss-safeguard-20b",
                     "qwen/qwen3.8-27b", "whisper-large-v3"]
    assert pick_model(groq_nov_2026) == "openai/gpt-oss-120b"
    assert pick_model(groq_nov_2026, exclude="openai/gpt-oss-120b") == "qwen/qwen3.8-27b"
    assert pick_model(["whisper-large-v3", "orpheus"]) is None  # never a speech/guard model
    assert pick_model(groq_nov_2026, vision=True) is None


async def test_refusal_covers_every_other_way_of_doing_it(tmp_path):
    """Live finding: after the user denied delete_path, the model tried `Remove-Item -Force` via run_shell."""
    from delta.config import Boundaries
    from delta.safety.approvals import ApprovalBroker
    from delta.safety.audit import AuditLog
    from delta.safety.policy import PolicyEngine
    from delta.tools.filesystem import filesystem_tools
    from delta.tools.registry import ToolExecutor, ToolRegistry
    from delta.tools.shell import RunShell

    target = tmp_path / "keep.txt"
    target.write_text("x")
    asked = []
    broker = ApprovalBroker(timeout_s=1)

    async def deny(req):
        asked.append(req.tool)
        broker.resolve(req.id, False, "user")

    broker.add_presenter("t", deny)
    ex = ToolExecutor(ToolRegistry([*filesystem_tools(), RunShell()]), PolicyEngine(Boundaries()), broker,
                      AuditLog(tmp_path / "a.jsonl"))
    r1 = await ex.execute("delete_path", {"path": str(target)}, task_id="t1")
    r2 = await ex.execute("run_shell", {"command": f'Remove-Item -Path "{target}" -Force'}, task_id="t1")
    r3 = await ex.execute("move_path", {"source": str(target), "destination": str(tmp_path / "x.txt")},
                          task_id="t1")
    assert not r1.ok and "DENIED" in r1.output
    assert not r2.ok and "already refused" in r2.output
    assert not r3.ok and "already refused" in r3.output
    assert asked == ["delete_path"]  # the user was asked once, not nagged
    assert target.exists()
    ex.forget_task("t1")  # a new task may ask again
    await ex.execute("delete_path", {"path": str(target)}, task_id="t2")
    assert asked == ["delete_path", "delete_path"]


async def test_last_provider_waits_longer_instead_of_failing(monkeypatch):
    monkeypatch.setattr("delta.llm.router.MAX_RATE_LIMIT_WAIT_S", 0.01)
    monkeypatch.setattr("delta.llm.router.LAST_RESORT_WAIT_S", 0.2)
    only = FakeProvider("groq", [LLMError("429", kind="rate_limit", retry_after_s=0.1), text("ok")])
    router = ModelRouter({"groq": only}, {"chat": ["groq"]})
    assert (await router.complete([])).content == "ok"  # 0.1 s > normal limit, but it is the last resort


def test_friendly_messages_for_users():
    from delta.core.orchestrator import friendly_model_error
    from delta.llm.schemas import AllProvidersFailedError

    busy = friendly_model_error(AllProvidersFailedError("x", ["unreachable", "rate_limit"]))
    assert "busy" in busy and "429" not in busy
    assert "Groq key" in friendly_model_error(AllProvidersFailedError("x", []))


def test_retry_after_parsing():
    class E(Exception):
        response = None

    assert _retry_after(E("Please try again in 6.735s. Need more tokens?")) == 6.735
    assert _retry_after(E("try again in 250ms")) == 0.25
    assert _retry_after(E("no hint")) is None


async def test_actor_gets_only_planned_tools_first(tmp_path):
    target = str(tmp_path / "a.txt")
    orch, _, _ = make(tmp_path, {
        "plan": [js({"needs_tools": True, "tools": ["write_file"], "success_criteria": [f"{target} exists"]})],
        "chat": [call("write_file", path=target, content="x"), text("done")],
        "verify": [js({"passed": True, "reason": "ok"})],
    })
    seen: list[list[str]] = []
    original = orch.router.complete

    async def spy(messages, tools=None, task="chat", json_mode=False):
        if task == "chat":
            seen.append(sorted(t["function"]["name"] for t in tools or []))
        return await original(messages, tools=tools, task=task, json_mode=json_mode)

    orch.router.complete = spy  # type: ignore[method-assign]
    r = await orch.run(TaskRequest("write a"))
    await orch.drain()
    assert r.status == "done"
    assert "write_file" in seen[0] and "delete_path" not in seen[0]  # subset, not all tools
    assert len(seen[0]) < len(orch.executor.registry.names())


async def test_memory_ignores_one_off_requests(monkeypatch):
    class R:
        async def complete(self, *a, **k):
            return text('{"facts": ["User wants to create a file named x.txt", "User uses Delta tool", '
                        '"User prefers short answers in Telugu"]}')

    facts = await extractor.extract_facts(R(), "make x.txt", "done")  # type: ignore[arg-type]
    assert facts == ["User prefers short answers in Telugu"]


async def test_memory_does_not_guess_name_from_windows_account(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "ravi")

    class R:
        async def complete(self, *a, **k):
            return text('{"facts": ["User\'s name is ravi", "User\'s username is ravi"]}')

    assert await extractor.extract_facts(R(), "save my notes", "saved C:/Users/ravi/x") == []  # type: ignore[arg-type]
    told = await extractor.extract_facts(R(), "hi, I am Ravi", "hello Ravi")  # type: ignore[arg-type]
    assert told == ["User's name is ravi"]  # the user said it, so it is kept
