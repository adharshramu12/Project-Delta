"""Codex live session over `codex app-server` JSON-RPC, against a fake server."""

import asyncio
import json

import pytest

from delta.config import ProviderConfig
from delta.llm import cli_providers
from delta.llm.cli_providers import CodexProvider, _CodexSession


class FakeServer:
    """Speaks just enough of the app-server protocol: initialize, thread/start, turn/start."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.out: asyncio.Queue[bytes] = asyncio.Queue()
        self.turns: list[dict] = []
        self.refused: list[dict] = []
        self.returncode = None
        server = self

        class Stdin:
            def write(self, raw):
                for line in raw.decode().splitlines():
                    server.handle(json.loads(line))

            def close(self):
                pass

        class Stdout:
            async def readline(self):
                return await server.out.get()

        self.stdin, self.stdout = Stdin(), Stdout()

    def emit(self, msg):
        self.out.put_nowait((json.dumps(msg) + "\n").encode())

    def handle(self, m):
        method, rid = m.get("method"), m.get("id")
        if method is None:  # our answer to a request from the server
            self.refused.append(m)
            return
        if method == "initialize":
            self.emit({"id": rid, "result": {"userAgent": "fake"}})
        elif method == "thread/start":
            self.emit({"id": rid, "result": {"thread": {"id": "th1"}}})
        elif method == "turn/start":
            self.turns.append(m["params"])
            self.emit({"id": 999, "method": "item/commandExecution/requestApproval", "params": {}})  # must be refused
            self.emit({"id": rid, "result": {"turn": {"id": "t"}}})
            self.emit({"method": "item/completed", "params": {"item": {"type": "agentMessage",
                                                                        "text": self.replies.pop(0)}}})
            self.emit({"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"last": {
                "inputTokens": 1000, "cachedInputTokens": 900, "outputTokens": 7}}}})
            self.emit({"method": "turn/completed", "params": {"turn": {}}})

    def kill(self):
        self.returncode = -9


@pytest.fixture
def server(monkeypatch):
    holder = {}

    async def spawn(*args, **kwargs):
        holder["server"] = FakeServer(holder.pop("replies"))
        holder["args"] = args
        return holder["server"]

    monkeypatch.setattr(cli_providers.asyncio, "create_subprocess_exec", spawn)
    return holder


async def test_a_turn_returns_the_reply_and_real_token_usage(server):
    server["replies"] = ["ok"]
    s = await _CodexSession.start("codex", "gpt-5.6-terra", "k")
    assert server["args"][1] == "app-server" and s.thread_id == "th1"
    text, usage = await s.ask("hi", "gpt-5.6-terra", "low", 5)
    assert text == "ok" and usage == {"input": 100, "cached": 900, "output": 7}
    assert server["server"].turns[0]["effort"] == "low"
    assert server["server"].refused and "error" in server["server"].refused[0]  # Codex may not act on the PC


async def test_one_conversation_per_task_sends_only_new_messages(server):
    server["replies"] = ['{"tool_calls": [{"name": "open_app", "arguments": {}}]}', '{"final": "DONE: opened"}']
    p = CodexProvider("codex", ProviderConfig(kind="codex", models={"chat": "gpt-5.6-terra"}))
    p._path = "codex"
    tools = [{"type": "function", "function": {"name": "open_app", "parameters": {"type": "object"}}}]
    convo = [{"role": "system", "content": "You are Delta."}, {"role": "user", "content": "open notepad"}]
    first = await p.complete("chat", convo, tools)
    assert first.tool_calls[0].name == "open_app"
    convo += [{"role": "assistant", "content": "", "tool_calls": [
                  {"id": "c1", "type": "function", "function": {"name": "open_app", "arguments": "{}"}}]},
              {"role": "tool", "tool_call_id": "c1", "name": "open_app", "content": "OK: Notepad is open"}]
    second = await p.complete("chat", convo, tools)
    turns = server["server"].turns
    assert len(turns) == 2 and second.final
    later = turns[1]["input"][0]["text"]
    assert "Notepad is open" in later and "You are Delta" not in later
    await p.unload()
    assert server["server"].returncode == -9


async def test_without_an_app_server_it_falls_back_to_exec(monkeypatch):
    async def broken(*a, **k):
        raise OSError("no app-server here")

    monkeypatch.setattr(cli_providers.asyncio, "create_subprocess_exec", broken)
    p = CodexProvider("codex", ProviderConfig(kind="codex", models={"chat": ""}))
    p._path = "codex"
    used = []

    async def one_shot(self, *a, **k):
        used.append("exec")
        return cli_providers.LLMResponse(content="hi")

    monkeypatch.setattr(cli_providers.CLIProvider, "complete", one_shot)
    assert (await p.complete("chat", [{"role": "user", "content": "hi"}], None)).content == "hi"
    assert used == ["exec"] and p._no_server


async def test_screenshot_turns_stay_in_the_live_session(server):
    """A one-shot `codex exec` per screenshot re-sent the whole task uncached and lost the framing."""
    server["replies"] = ['{"tool_calls": [{"name": "click_at", "arguments": {}}]}', '{"final": "DONE: clicked"}']
    p = CodexProvider("codex", ProviderConfig(kind="codex", models={"chat": "gpt-5.6-terra"}))
    p._path = "codex"
    tools = [{"type": "function", "function": {"name": "click_at", "parameters": {"type": "object"}}}]
    convo = [{"role": "system", "content": "You are Delta."}, {"role": "user", "content": "click new notebook"}]
    await p.complete("chat", convo, tools)
    shot = "data:image/jpeg;base64,AAAA"
    convo += [{"role": "assistant", "content": "", "tool_calls": [
                  {"id": "c1", "type": "function", "function": {"name": "click_at", "arguments": "{}"}}]},
              {"role": "tool", "tool_call_id": "c1", "name": "click_at", "content": "clicked"},
              {"role": "user", "content": [{"type": "text", "text": "AFTER screenshot:"},
                                           {"type": "image_url", "image_url": {"url": shot}}]}]
    second = await p.complete("chat", convo, tools)
    turns = server["server"].turns
    assert second.final and len(turns) == 2  # same server, no exec process
    assert turns[1]["input"][1] == {"type": "image", "url": shot}
    assert "they all work" in turns[1]["input"][0]["text"]
    await p.unload()


async def test_a_codex_that_refuses_the_safety_overrides_is_not_run_as_configured(monkeypatch):
    """M9: fail closed. If Codex will not start with Delta's lean overrides (plugins, MCP servers and shell off),
    it is reported unusable, never run with the user's own setup, and the plain probe is closed at once."""
    started = []

    class Probe:
        returncode = None

        def close(self):
            started.append("closed")

    async def start(cls, exe, model, key, limit_s=30, extra=None):
        started.append("lean" if extra else "plain")
        if extra:
            raise cli_providers.LLMError("unknown config key")
        return Probe()

    monkeypatch.setattr(_CodexSession, "start", classmethod(start))
    p = CodexProvider("codex", ProviderConfig(kind="codex", models={"chat": ""}))
    p._path = "codex"
    with pytest.raises(cli_providers.LLMError, match="safety settings"):
        await p.complete("chat", [{"role": "user", "content": "hi"}], None)
    assert started == ["lean", "plain", "closed"]
    await p.prewarm("chat")  # quiet, and never starts another plain session
    with pytest.raises(cli_providers.LLMError):
        await p.complete("chat", [{"role": "user", "content": "hi"}], None)
    assert started == ["lean", "plain", "closed"]
    assert "features.shell_tool=false" in p.command("codex", "", cli_providers.Path("o.txt"))  # exec: lean too
