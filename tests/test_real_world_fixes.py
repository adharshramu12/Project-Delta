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
