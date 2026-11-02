import pytest
from fakes import FakeProvider, text

from delta.llm.providers import extract_json
from delta.llm.router import ModelRouter
from delta.llm.schemas import AllProvidersFailedError, LLMError


async def test_falls_back_when_local_fails():
    ollama = FakeProvider("ollama", [LLMError("connection refused")], local=True)
    groq = FakeProvider("groq", [text("hi from groq")])
    router = ModelRouter({"ollama": ollama, "groq": groq}, {"chat": ["ollama", "groq"]})
    resp = await router.complete([{"role": "user", "content": "hi"}])
    assert resp.content == "hi from groq"
    assert router.last_provider == "groq"


async def test_skips_local_when_governor_says_no():
    ollama = FakeProvider("ollama", [text("local")], local=True)
    nim = FakeProvider("nim", [text("cloud")])
    router = ModelRouter({"ollama": ollama, "nim": nim}, {"chat": ["ollama", "nim"]}, allow_local=lambda: False)
    assert (await router.complete([])).content == "cloud"
    assert ollama.calls == []


async def test_circuit_breaker_opens_after_repeated_failures():
    bad = FakeProvider("bad", [LLMError("x")] * 3)
    good = FakeProvider("good", [text("ok")] * 4)
    router = ModelRouter({"bad": bad, "good": good}, {"chat": ["bad", "good"]})
    for _ in range(4):
        await router.complete([])
    assert len(bad.calls) == 3  # 4th request skipped the open breaker


async def test_all_failed_raises_helpful_error():
    router = ModelRouter({"a": FakeProvider("a", [LLMError("down")])}, {"chat": ["a"]})
    with pytest.raises(AllProvidersFailedError, match="down"):
        await router.complete([])


async def test_unload_local_models():
    p = FakeProvider("ollama", local=True)
    await ModelRouter({"ollama": p}, {}).unload_local()
    assert p.unloaded


def test_extract_json_from_fenced_prose():
    assert extract_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('blah {"b": {"c": 2}} trailing') == {"b": {"c": 2}}


async def test_main_ai_is_tried_first_for_every_task():
    local = FakeProvider("lmstudio", [text("local")], local=True)
    claude = FakeProvider("claude_code", [text("claude")] * 2)
    router = ModelRouter({"lmstudio": local, "claude_code": claude},
                         {"chat": ["lmstudio", "claude_code"], "plan": ["lmstudio"]}, preferred="claude_code")
    assert (await router.complete([], task="chat")).content == "claude"
    assert (await router.complete([], task="plan")).content == "claude"
    assert local.calls == [] and router.usable()[0] == "claude_code"
