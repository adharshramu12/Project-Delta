"""Using the user's existing plans (Claude Code, Codex) and local servers (LM Studio)."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from delta.bootstrap import build_providers
from delta.config import REPO_ROOT, ProviderConfig, Settings, _read_yaml
from delta.llm.cli_providers import ClaudeCodeProvider, CLIProvider, CodexProvider, find_cli, render_transcript
from delta.llm.providers import OpenAICompatProvider, lmstudio_base_url
from delta.llm.schemas import LLMError

TOOLS = [{"type": "function", "function": {"name": "open_app", "parameters": {"type": "object"}}}]


def cli(cls, **cfg):
    p = cls("x", ProviderConfig(kind="claude_code", models={"chat": "sonnet"}, **cfg))
    p._path = "fake-exe"
    return p


def fake_run(stdout="", stderr="", code=0, writes=None):
    seen = {}

    async def run(cmd, prompt, cwd):
        seen.update(cmd=cmd, prompt=prompt, cwd=cwd)
        if writes is not None:
            Path(cmd[cmd.index("--output-last-message") + 1]).write_text(writes, encoding="utf-8")
        return stdout, stderr, code

    return run, seen


class FakeSession:
    """Stands in for a running `claude -p --input-format stream-json` process."""

    def __init__(self, events):
        self.events = list(events)
        self.lines = []
        self.sent = 0
        self.system = None
        self.last_used = 0.0
        self.closed = False

    def alive(self):
        return not self.closed

    def continues(self, messages):
        from delta.llm.cli_providers import _Session

        return _Session.continues(self, messages)

    async def ask(self, line, limit_s):
        self.lines.append(json.loads(line))
        return self.events.pop(0)

    def close(self):
        self.closed = True


def claude(*events, **cfg):
    """A Claude Code provider whose sessions are fakes; returns (provider, spawned sessions)."""
    p = cli(ClaudeCodeProvider, **cfg)
    spawned = []

    async def spawn(model, effort):
        s = FakeSession(events)
        s.args = (model, effort)
        spawned.append(s)
        return s

    p._spawn = spawn
    return p, spawned


def result(text, **usage):
    return {"type": "result", "is_error": False, "result": text, "usage": usage}


def sent_text(session, i=-1):
    return session.lines[i]["message"]["content"][0]["text"]


def test_transcript_keeps_roles_and_drops_images():
    text = render_transcript([
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": [{"type": "text", "text": "what is this"}, {"type": "image_url"}]},
    ])
    assert "### INSTRUCTIONS\nbe brief" in text
    assert "### USER\nwhat is this" in text
    assert text.endswith("### ASSISTANT (your reply)")


async def test_claude_code_turns_off_its_own_tools_and_parses_tool_calls():
    p, spawned = claude(result('{"tool_calls": [{"name": "open_app", "arguments": {"name": "notepad"}}]}'))
    resp = await p.complete("chat", [{"role": "user", "content": "open notepad"}], TOOLS)
    assert resp.tool_calls[0].name == "open_app" and resp.tool_calls[0].arguments == {"name": "notepad"}
    cmd = p.session_args("claude", "sonnet", "low")
    assert cmd[cmd.index("--tools") + 1] == ""  # Delta's policy, not Claude Code, runs actions
    assert "--strict-mcp-config" in cmd and cmd[cmd.index("--model") + 1] == "sonnet"
    assert "open notepad" in sent_text(spawned[0]) and "open_app" in sent_text(spawned[0])  # via stdin
    assert spawned[0].args == ("sonnet", "")


async def test_claude_code_plain_text_reply():
    p, _ = claude(result("Hello!"))
    assert (await p.complete("chat", [{"role": "user", "content": "hi"}], None)).content == "Hello!"


async def test_one_session_per_task_gets_only_the_new_messages():
    """Speed + tokens: no 6 s process start per step, and the history is not re-sent."""
    p, spawned = claude(result('{"tool_calls": [{"name": "open_app", "arguments": {}}]}'),
                        result("DONE: opened", input_tokens=40, cache_read_input_tokens=1200, output_tokens=5))
    convo = [{"role": "system", "content": "You are Delta."}, {"role": "user", "content": "open notepad"}]
    await p.complete("chat", convo, TOOLS)
    convo += [{"role": "assistant", "content": "", "tool_calls": [
                  {"id": "c1", "type": "function", "function": {"name": "open_app", "arguments": "{}"}}]},
              {"role": "tool", "tool_call_id": "c1", "name": "open_app", "content": "OK: Notepad is open"}]
    resp = await p.complete("chat", convo, TOOLS)
    assert len(spawned) == 1  # same process both times
    second = sent_text(spawned[0])
    assert "Notepad is open" in second and "You are Delta" not in second and "open notepad" not in second
    assert resp.usage == {"input": 40, "cached": 1200, "output": 5}


async def test_a_new_conversation_uses_the_prewarmed_process():
    p, spawned = claude(result("plan"))
    await p.prewarm("chat")
    assert len(spawned) == 1
    await p.complete("chat", [{"role": "user", "content": "hi"}], None)
    assert len(spawned) == 1 and spawned[0].lines  # no second start: the warm one answered
    await p.unload()
    assert spawned[0].closed  # freed at the end of the task


async def test_codex_reads_last_message_file_and_is_read_only():
    p = cli(CodexProvider)
    p.cfg.models = {"chat": ""}
    p._run, seen = fake_run(stdout="lots of progress noise", writes='{"final": "done"}')
    resp = await p.complete("chat", [{"role": "user", "content": "hi"}], TOOLS)
    assert resp.content == "done" and resp.model == "default"
    assert seen["cmd"][-2:] == ["--", "-"] and "--model" not in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--sandbox") + 1] == "read-only"


@pytest.mark.parametrize(("stderr", "kind", "words"), [
    ("Error: Not logged in. Please run /login", "error", "not signed in"),
    ("Claude usage limit reached. Resets at 5pm", "rate_limit", "usage limit"),
])
async def test_cli_errors_are_explained(stderr, kind, words):
    p, spawned = claude({"type": "result", "is_error": True, "result": stderr})
    with pytest.raises(LLMError, match=words) as e:
        await p.complete("chat", [{"role": "user", "content": "hi"}], None)
    assert e.value.kind == kind and spawned[0].closed


async def test_missing_cli_is_unavailable_and_unreachable(monkeypatch):
    monkeypatch.setattr("delta.llm.cli_providers.find_cli", lambda names, configured=None: None)
    p = ClaudeCodeProvider("claude_code", ProviderConfig(kind="claude_code", models={"chat": "sonnet"}))
    assert not p.supports("chat")
    with pytest.raises(LLMError) as e:
        await p.complete("chat", [], None)
    assert e.value.kind == "unreachable"


async def test_real_subprocess_gets_prompt_on_stdin(tmp_path):
    class Echo(CLIProvider):
        names = ("echo",)
        label = "Echo"

        def command(self, exe, model, out_file, effort="", images=None):
            return [exe, "-c", "import sys; print(sys.stdin.read().count('ping'))"]

        def reply(self, stdout, out_file):
            return stdout.strip()

    p = Echo("echo", ProviderConfig(kind="codex", command=sys.executable, models={"chat": ""}, timeout_s=30))
    assert (await p.complete("chat", [{"role": "user", "content": "ping ping"}], None)).content == "2"


def test_find_cli_uses_configured_path(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    assert find_cli(("claude",), str(exe)) == str(exe)
    assert find_cli(("claude",), str(tmp_path / "missing.exe")) is None


async def test_lmstudio_auto_picks_a_downloaded_chat_model():
    p = OpenAICompatProvider("lmstudio", ProviderConfig(base_url="http://127.0.0.1:1234/v1", local=True,
                                                        native_tools=False, models={"chat": "auto"}), None)
    used = {}

    async def create(**kw):
        used.update(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"final": "hi"}',
                                                                               tool_calls=None))])

    async def models_list():
        return SimpleNamespace(data=[SimpleNamespace(id="text-embedding-nomic-embed-text-v1.5"),
                                     SimpleNamespace(id="google/gemma-4-e4b")])

    p._client_obj = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                                    models=SimpleNamespace(list=models_list))
    resp = await p.complete("chat", [{"role": "user", "content": "hi"}], TOOLS)
    assert used["model"] == "google/gemma-4-e4b" and resp.model == "google/gemma-4-e4b"
    assert p.model_for("chat") == "google/gemma-4-e4b"  # remembered


def test_shipped_config_builds_every_provider_kind():
    raw = _read_yaml(REPO_ROOT / "config" / "delta.yaml")
    settings = Settings.model_validate({"llm": raw["llm"]})
    providers = build_providers(settings)
    assert isinstance(providers["claude_code"], ClaudeCodeProvider)
    assert isinstance(providers["codex"], CodexProvider)
    assert isinstance(providers["lmstudio"], OpenAICompatProvider)
    for route in settings.llm.routes.values():
        assert set(route) <= set(providers)


def test_lmstudio_port_is_read_from_its_own_settings(tmp_path):
    cfg = tmp_path / ".lmstudio" / "apps" / "bionic" / ".internal" / "http-server-config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"port": 1235, "networkInterface": "127.0.0.1"}), encoding="utf-8")
    assert lmstudio_base_url("http://127.0.0.1:1234/v1", home=tmp_path) == "http://127.0.0.1:1235/v1"
    assert lmstudio_base_url("http://x/v1", home=tmp_path / "nobody") == "http://x/v1"


async def test_server_without_json_mode_is_asked_again_without_it():
    import httpx
    import openai

    p = OpenAICompatProvider("lmstudio", ProviderConfig(base_url="http://127.0.0.1:1235/v1", local=True,
                                                        native_tools=False, models={"chat": "gemma"}), None)
    sent = []

    async def create(**kw):
        sent.append("response_format" in kw)
        if "response_format" in kw:
            req = httpx.Request("POST", "http://127.0.0.1:1235/v1/chat/completions")
            raise openai.BadRequestError("Error code: 400", response=httpx.Response(400, request=req),
                                         body="'response_format.type' must be 'json_schema' or 'text'")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"final": "ok"}',
                                                                               tool_calls=None))])

    p._client_obj = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert (await p.complete("chat", [{"role": "user", "content": "hi"}], TOOLS)).content == "ok"
    await p.complete("chat", [{"role": "user", "content": "hi"}], TOOLS)
    assert sent == [True, False, False]  # remembered: no failing first try the second time


async def test_json_mode_without_tools_returns_the_raw_json():
    """The planner asks for JSON and parses it itself; the tool-call parser must not eat it."""
    plan = '{"needs_tools": false, "answer": "Hi there!"}'
    p, _ = claude(result(plan))
    assert (await p.complete("plan", [{"role": "user", "content": "hi"}], None, json_mode=True)).content == plan


async def test_lmstudio_is_skipped_when_loading_would_exhaust_ram(monkeypatch):
    import psutil

    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=1e9))
    p = OpenAICompatProvider("lmstudio", ProviderConfig(base_url="http://127.0.0.1:1/v1", local=True,
                                                        min_free_ram_gb=6, models={"chat": "gemma"}), None)
    with pytest.raises(LLMError, match="RAM free") as e:
        await p.complete("chat", [{"role": "user", "content": "hi"}], None)
    assert e.value.kind == "unreachable"


async def test_each_job_gets_its_own_model_and_effort():
    p, spawned = claude(result("{}"), result("ok"))
    p.cfg.models = {"plan": "opus", "chat": "haiku"}
    p.cfg.efforts = {"plan": "high", "chat": "low"}
    await p.complete("plan", [{"role": "user", "content": "x"}], None, json_mode=True)
    await p.complete("verify", [{"role": "user", "content": "x"}], None)  # falls back to chat's choice
    assert [s.args for s in spawned] == [("opus", "high"), ("haiku", "low")]
    cmd = p.session_args("claude", "opus", "high")
    assert cmd[cmd.index("--effort") + 1] == "high"


def test_codex_effort_and_model_list(tmp_path, monkeypatch):
    p = cli(CodexProvider)
    cmd = p.command("codex", "gpt-6-luna", tmp_path / "o.txt", "high")
    assert cmd[cmd.index("--model") + 1] == "gpt-6-luna" and "model_reasoning_effort=high" in cmd
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-6-luna", "display_name": "GPT-6 Luna", "visibility": "list",
         "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
        {"slug": "internal", "visibility": "hide"}]}), encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert p.model_options() == [{"id": "gpt-6-luna", "label": "GPT-6 Luna", "efforts": ["low", "high"]}]


def test_codex_token_count_is_read_from_its_output():
    p = cli(CodexProvider)
    assert p.usage("", "...\ntokens used\n2,658\n") == {"input": 2658}
    assert p.usage("tokens used: 912", "") == {"input": 912}
    assert p.usage("no count here", "") == {}


async def test_codex_reply_carries_its_token_count(tmp_path):
    p = cli(CodexProvider)
    p.cfg.models = {"chat": ""}

    async def run(cmd, prompt, cwd):
        Path(cmd[cmd.index("--output-last-message") + 1]).write_text("hello", encoding="utf-8")
        return "", "tokens used\n1,234\n", 0

    p._run = run
    assert (await p.complete("chat", [{"role": "user", "content": "hi"}], None)).usage == {"input": 1234}


def test_codex_runs_lean_for_delta(monkeypatch, tmp_path):
    """Codex's own agent features and the user's desktop plugins/MCP servers cost ~7k tokens a call."""
    from delta.llm.cli_providers import lean_codex_args

    (tmp_path / "config.toml").write_text('[plugins."browser@openai-bundled"]\nenabled = true\n'
                                          "[mcp_servers.node_repl]\ncommand = 'x'\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    args = lean_codex_args()
    sets = args[1::2]
    assert set(args[0::2]) == {"-c"}
    assert "features.shell_tool=false" in sets and "features.multi_agent=false" in sets
    assert "plugins.browser@openai-bundled.enabled=false" in sets
    assert "mcp_servers.node_repl.enabled=false" in sets


CLINE_OK = "\n".join([
    '{"type":"agent_event","event":{"type":"text","text":"O"}}',
    '{"type":"run_result","finishReason":"stop","text":"OK","usage":{"inputTokens":900,"outputTokens":3,'
    '"cacheReadTokens":4000,"cacheWriteTokens":100}}'])
CLINE_BROKE = ('{"type":"run_result","finishReason":"error","text":"Insufficient balance. Your Cline Credits '
               'balance is $-0.02"}')


class FakeClineProc:
    """Stands in for cline-cli: yields its JSON event lines; records whether it was stopped early."""

    def __init__(self, stdout: str, code: int = 0):
        self._lines = [(line + "\n").encode() for line in stdout.splitlines()]
        self.returncode, self.code, self.killed = None, code, False

        class Out:
            def __init__(self, lines):
                self.lines = lines

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.lines:
                    raise StopAsyncIteration
                return self.lines.pop(0)

        class Err:
            async def read(self):
                return b""

        self.stdout, self.stderr = Out(self._lines), Err()

    def kill(self):
        self.killed = True

    async def wait(self):
        self.returncode = -9 if self.killed else self.code
        return self.returncode


def fake_cline(monkeypatch, respond):
    """Patch process creation; respond(cmd) -> (stdout, exit code). Returns the list of launched procs."""
    from delta.llm import cli_providers

    procs = []

    async def spawn(*cmd, **kwargs):
        out, code = respond(list(cmd))
        procs.append(FakeClineProc(out, code))
        procs[-1].cmd = list(cmd)
        return procs[-1]

    monkeypatch.setattr(cli_providers.asyncio, "create_subprocess_exec", spawn)
    return procs


def _cline(tmp_path):
    from delta.config import ProviderConfig
    from delta.llm.cli_providers import ClineProvider

    p = ClineProvider("cline", ProviderConfig(kind="cline", models={"chat": ""}))
    p._path = "cline"
    return p


def test_cline_reads_answer_and_usage_from_its_json_events(tmp_path):
    p = _cline(tmp_path)
    assert p.reply(CLINE_OK, tmp_path / "x") == "OK"
    assert p.usage(CLINE_OK, "") == {"input": 1000, "cached": 4000, "output": 3}
    cmd = p.command("cline", "", tmp_path / "reply.txt")
    assert "--json" in cmd and cmd[cmd.index("-s") + 1].startswith("You are the reasoning engine of")
    assert cmd[cmd.index("--auto-approve") + 1] == "false"


def test_cline_out_of_credits_moves_on_instead_of_waiting(tmp_path):
    from delta.llm.schemas import LLMError

    p = _cline(tmp_path)
    with pytest.raises(LLMError) as e:
        p.reply(CLINE_BROKE, tmp_path / "x")
    assert e.value.kind == "quota" and "credits are used up" in str(e.value)


async def test_cline_gets_the_prompt_as_an_argument_and_refuses_oversize(tmp_path, monkeypatch):
    from delta.llm.schemas import LLMError

    p = _cline(tmp_path)
    files: list[str] = []

    def respond(cmd):  # read the prompt file while Cline's working folder still exists
        f = Path(cmd[cmd.index("-c") + 1]) / p.PROMPT_FILE
        files.append(f.read_text(encoding="utf-8") if f.is_file() else "")
        return CLINE_OK, 0

    procs = fake_cline(monkeypatch, respond)
    r = await p.complete("chat", [{"role": "user", "content": "say OK"}], None)
    assert r.content == "OK" and "say OK" in procs[0].cmd[-1] and files == [""]  # short: on the command line
    # A long prompt (a task that read web pages) used to fail with "prompt too long for Cline's command line".
    # Now it goes into a file in Cline's folder and the command line only mentions the file.
    r = await p.complete("chat", [{"role": "user", "content": "x" * 40000 + " END-MARK"}], None)
    assert r.content == "OK" and f"@{p.PROMPT_FILE}" in procs[1].cmd[-1] and len(procs[1].cmd[-1]) < 200
    assert "END-MARK" in files[1] and len(files[1]) > 40000
    with pytest.raises(LLMError) as e:
        await p.complete("chat", [{"role": "user", "content": "x" * (p.MAX_PROMPT_CHARS + 1)}], None)
    assert e.value.kind == "too_large" and len(procs) == 2
    assert not p.supports("vision")


CATALOG = {"free": [{"id": "reflection/beam-free", "name": "Beam"},
                    {"id": "cline-free/solar-mini4", "name": "Solar Mini 4"}],
           "recommended": [{"id": "anthropic/claude-sonnet-5.5", "name": "claude-sonnet-5.5"}]}


def test_cline_lists_free_models_first(tmp_path, monkeypatch):
    p = _cline(tmp_path)
    monkeypatch.setattr(p, "catalog", lambda: CATALOG)
    labels = [o["label"] for o in p.model_options()]
    assert labels == ["Beam (free)", "Solar Mini 4 (free)", "claude sonnet 5.5 (uses credits)"]


async def test_cline_out_of_credits_on_a_paid_model_answers_with_a_free_one(tmp_path, monkeypatch):
    from delta.config import ProviderConfig
    from delta.llm.cli_providers import ClineProvider

    p = ClineProvider("cline", ProviderConfig(kind="cline", models={"chat": "anthropic/claude-sonnet-5.5"}))
    p._path = "cline"
    monkeypatch.setattr(p, "catalog", lambda: CATALOG)
    models = []

    def respond(cmd):
        model = cmd[cmd.index("-m") + 1]
        models.append(model)
        return (CLINE_BROKE, 1) if model == "anthropic/claude-sonnet-5.5" else (CLINE_OK, 0)

    fake_cline(monkeypatch, respond)
    r = await p.complete("chat", [{"role": "user", "content": "say OK"}], None)
    assert r.content == "OK" and models == ["anthropic/claude-sonnet-5.5", "reflection/beam-free"]
    assert p.model_for("chat") == "anthropic/claude-sonnet-5.5"  # the user's choice is kept for next time


CLINE_NATIVE_CALL = "\n".join([
    '{"type":"agent_event","event":{"type":"usage","totalInputTokens":12361,"totalOutputTokens":59,'
    '"totalCacheReadTokens":6016}}',
    '{"type":"agent_event","event":{"type":"content_start","contentType":"tool","toolName":"browser_open",'
    '"input":{"url":"https://example.com"}}}',
    '{"type":"agent_event","event":{"type":"content_start","contentType":"tool","toolName":"run_commands",'
    '"input":{"commands":["start https://example.com"]}}}'])


async def test_cline_native_tool_call_becomes_a_delta_tool_call_and_stops_cline(tmp_path, monkeypatch):
    """Cline's model calls Delta's tools natively; Delta used to see an empty reply and nudge 3 times."""
    p = _cline(tmp_path)
    procs = fake_cline(monkeypatch, lambda cmd: (CLINE_NATIVE_CALL, 0))
    tools = [{"type": "function", "function": {"name": "browser_open", "parameters": {"type": "object"}}}]
    r = await p.complete("chat", [{"role": "user", "content": "open example.com"}], tools)
    assert [(c.name, c.arguments) for c in r.tool_calls] == [("browser_open", {"url": "https://example.com"})]
    assert procs[0].killed  # stopped before Cline's own run_commands attempt
    assert r.usage == {"input": 12361, "cached": 6016, "output": 59}
