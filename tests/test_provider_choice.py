"""The AI the user picks is the AI that answers (user report: "Cline, Groq, NVIDIA are not taking the credits, it is
only taking codex").

Root causes reproduced here, each with a fake provider (no keys, no network):
- With a main AI chosen, any failure of it (a Groq key that expired: HTTP 401) silently moved the task to the next
  AI on the route, Codex, so Codex answered and was billed while the user believed Groq was in use.
- A refused key was an anonymous "HTTP 401" error: it was retried on every message and never named.
- Cline installed with npm is `cline.cmd`: the prompt went through cmd.exe, which cuts it at the first line break.
- The task's "via" line named the router's last provider anywhere, not the model that did this task's work.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from delta.config import ProviderConfig, Settings
from delta.llm.router import ModelRouter
from delta.llm.schemas import AllProvidersFailedError, LLMError, LLMResponse

FAKE_KEY = "test-key-not-real"


class Fake:
    """A provider that answers, or fails with a given LLMError, and counts its calls."""

    def __init__(self, name: str, jobs: tuple[str, ...] = ("plan", "chat", "verify"), fail: LLMError | None = None,
                 ready: bool = True, model: str = "m1") -> None:
        self.name, self.jobs, self.fail, self.ready, self.model = name, jobs, fail, ready, model
        self.local = False
        self.calls: list[str] = []

    def handles(self, task: str) -> bool:
        return task in self.jobs

    def supports(self, task: str) -> bool:
        return self.handles(task) and self.ready

    async def complete(self, task: str, messages: Any, tools: Any, json_mode: bool = False) -> LLMResponse:
        self.calls.append(task)
        if self.fail is not None:
            raise self.fail
        return LLMResponse(content="ok", provider=self.name, model=self.model)

    async def unload(self) -> None:
        return None


ROUTES = {"chat": ["codex", "cline", "groq", "nim"], "plan": ["codex", "cline", "groq", "nim"],
          "verify": ["groq", "nim", "codex", "cline"], "vision": ["codex", "nim"]}


def router(preferred: str, fallback: bool, **providers: Fake) -> ModelRouter:
    return ModelRouter(dict(providers), ROUTES, preferred=preferred, fallback=fallback)


def ask(r: ModelRouter, task: str = "chat") -> LLMResponse:
    return asyncio.run(r.complete([{"role": "user", "content": "hi"}], task=task))


@pytest.mark.parametrize("main", ["groq", "nim", "cline"])
@pytest.mark.parametrize("job", ["plan", "chat", "verify"])
def test_the_chosen_main_ai_answers_every_job(main: str, job: str) -> None:
    fakes = {n: Fake(n) for n in ("codex", "cline", "groq", "nim")}
    r = router(main, False, **fakes)
    resp = ask(r, job)
    assert resp.provider == main
    assert fakes["codex"].calls == []


def test_expired_groq_key_does_not_hand_the_task_to_codex() -> None:
    """The reported bug: Groq answers 401 and Codex (next on the route) silently did the work."""
    from delta.llm.providers import key_refused

    groq = Fake("groq", fail=LLMError(f"groq: {key_refused('groq')}", kind="auth"))
    codex = Fake("codex")
    r = router("groq", False, groq=groq, codex=codex)
    with pytest.raises(AllProvidersFailedError) as e:
        ask(r)
    assert codex.calls == []
    assert "Groq key expired or invalid. Add a new one in Settings > Keys" in e.value.user_message
    assert "fallback is off" in e.value.user_message
    from delta.core.orchestrator import friendly_model_error

    assert friendly_model_error(e.value) == e.value.user_message
    # The refused key rests (no paid retry on every message) until a new one is saved.
    assert not r.breakers["groq"].available()
    r.refresh()
    assert r.breakers["groq"].available()


def test_fallback_on_lets_the_next_ai_answer_and_says_so() -> None:
    from delta.llm.providers import key_refused

    groq = Fake("groq", fail=LLMError(f"groq: {key_refused('groq')}", kind="auth"))
    codex = Fake("codex")
    r = router("groq", True, groq=groq, codex=codex)
    r.reset_meter()
    assert ask(r).provider == "codex"
    note = r.fallback_note()
    assert "groq skipped" in note and "Groq key expired or invalid" in note


def test_main_ai_without_a_key_is_named_not_skipped() -> None:
    groq = Fake("groq", ready=False)
    codex = Fake("codex")
    r = router("groq", False, groq=groq, codex=codex)
    with pytest.raises(AllProvidersFailedError) as e:
        ask(r)
    assert codex.calls == [] and groq.calls == []
    assert "groq (your main AI)" in e.value.user_message


def test_a_job_the_main_ai_cannot_do_still_uses_the_route() -> None:
    """Pictures with a text-only main AI (Cline takes no screenshots): the vision route answers, as before."""
    cline, codex = Fake("cline", jobs=("plan", "chat", "verify")), Fake("codex", jobs=("chat", "vision"))
    r = router("cline", False, cline=cline, codex=codex)
    assert ask(r, "vision").provider == "codex"
    assert ask(r, "chat").provider == "cline"


def test_auto_keeps_the_configured_route_order() -> None:
    """No main AI chosen: unchanged, the first ready provider on the route answers (Codex in the default order)."""
    fakes = {n: Fake(n) for n in ("codex", "cline", "groq", "nim")}
    r = router("auto", False, **fakes)
    assert ask(r).provider == "codex"
    assert r.strict("chat") is False


def test_main_ai_menu_still_marks_every_ready_ai() -> None:
    fakes = {n: Fake(n) for n in ("codex", "cline", "groq", "nim")}
    r = router("groq", False, **fakes)
    assert set(r.usable()) == {"codex", "cline", "groq", "nim"}
    assert r.usable()[0] == "groq"


def test_rate_limited_main_ai_keeps_the_busy_message() -> None:
    groq = Fake("groq", fail=LLMError("groq: rate limited", kind="rate_limit", retry_after_s=999))
    r = router("groq", False, groq=groq, codex=Fake("codex"))
    with pytest.raises(AllProvidersFailedError) as e:
        ask(r)
    assert e.value.only_rate_limited
    from delta.core.orchestrator import friendly_model_error

    assert "busy" in friendly_model_error(e.value)


def test_task_names_the_provider_and_model_that_did_its_work() -> None:
    groq = Fake("groq", model="openai/gpt-oss-120b")
    r = router("groq", False, groq=groq, codex=Fake("codex"))
    r.reset_meter()
    assert r.answered_by() == ("", "")
    ask(r, "plan")
    ask(r, "chat")
    assert r.answered_by() == ("groq", "openai/gpt-oss-120b")
    r.reset_meter()
    assert r.answered_by() == ("", "")


def test_provider_label_for_the_task_list() -> None:
    from delta.core.state import TaskResult
    from delta.engine.recorder import provider_label

    assert provider_label(TaskResult("t", "done", "", provider="groq", model="openai/gpt-oss-120b")) == \
        "groq · openai/gpt-oss-120b"
    assert provider_label(TaskResult("t", "done", "", provider="codex", model="default")) == "codex"
    assert provider_label(TaskResult("t", "done", "", provider="")) == ""


# --- the real wiring: settings -> bootstrap -> router --------------------------------------------------------------
def test_settings_build_a_strict_router_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Groq and NVIDIA keys saved, Codex installed, Groq picked as main AI: Groq answers planner, executor and
    checker jobs; NVIDIA is picked the same way."""
    from delta import bootstrap
    from delta.config import load_settings
    from delta.llm import cli_providers

    monkeypatch.setenv("DELTA_DATA_DIR", str(tmp_path))  # Delta's shipped defaults, not this PC's own settings
    settings: Settings = load_settings()
    assert settings.llm.fallback is False
    monkeypatch.setattr(bootstrap, "get_secret", lambda name: FAKE_KEY if name in
                        ("groq_api_key", "nvidia_api_key") else None)
    monkeypatch.setattr(cli_providers, "find_cli", lambda names, configured=None: "C:/fake/codex.exe")
    providers = bootstrap.build_providers(settings)
    for main in ("groq", "nim"):
        r = ModelRouter(providers, settings.llm.routes, preferred=main, fallback=settings.llm.fallback)
        for job in ("plan", "chat", "verify"):
            assert [p.name for p in r.candidates(job)] == [main], (main, job)
    # Groq has no picture model configured: pictures follow the vision route, as before.
    r = ModelRouter(providers, settings.llm.routes, preferred="groq", fallback=False)
    assert "groq" not in [p.name for p in r.candidates("vision")]


def test_groq_401_becomes_a_named_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    import openai

    from delta.llm.providers import OpenAICompatProvider

    cfg = ProviderConfig(base_url="https://api.groq.com/openai/v1", secret="groq_api_key",
                         models={"chat": "openai/gpt-oss-120b"})
    p = OpenAICompatProvider("groq", cfg, FAKE_KEY)
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")

    class Completions:
        async def create(self, **kwargs: Any) -> Any:
            raise openai.AuthenticationError("Invalid API Key", response=httpx.Response(401, request=req),
                                             body={"error": {"message": "Invalid API Key"}})

    class Client:
        chat = type("Chat", (), {"completions": Completions()})()

    p._client_obj = Client()
    with pytest.raises(LLMError) as e:
        asyncio.run(p.complete("chat", [{"role": "user", "content": "hi"}], None))
    assert e.value.kind == "auth"
    assert str(e.value) == "groq: Groq key expired or invalid. Add a new one in Settings > Keys"
    assert FAKE_KEY not in str(e.value)


def test_nvidia_out_of_credits_is_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    import openai

    from delta.llm.providers import OpenAICompatProvider

    cfg = ProviderConfig(base_url="https://integrate.api.nvidia.com/v1", secret="nvidia_api_key",
                         models={"chat": "meta/llama-3.3-70b-instruct"})
    p = OpenAICompatProvider("nim", cfg, FAKE_KEY)
    req = httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")

    class Completions:
        async def create(self, **kwargs: Any) -> Any:
            raise openai.APIStatusError("Payment Required", response=httpx.Response(402, request=req), body=None)

    p._client_obj = type("C", (), {"chat": type("Chat", (), {"completions": Completions()})()})()
    with pytest.raises(LLMError) as e:
        asyncio.run(p.complete("chat", [{"role": "user", "content": "hi"}], None))
    assert e.value.kind == "quota" and "NVIDIA credits are used up" in str(e.value)


# --- Cline on Windows ------------------------------------------------------------------------------------------------
NPM_SHIM = r"""@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\cline\bin\cline" %*
"""


def _npm_install(tmp_path: Path) -> Path:
    shim = tmp_path / "cline.cmd"
    shim.write_text(NPM_SHIM, encoding="utf-8")
    script = tmp_path / "node_modules" / "cline" / "bin" / "cline"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    (tmp_path / "node.exe").write_text("", encoding="utf-8")
    return shim


def test_npm_shim_resolves_to_node_and_the_script(tmp_path: Path) -> None:
    from delta.llm.cli_providers import npm_shim_target

    shim = _npm_install(tmp_path)
    target = npm_shim_target(str(shim))
    assert target is not None
    assert Path(target[0]).name == "node.exe"
    assert Path(target[1]).parts[-4:] == ("node_modules", "cline", "bin", "cline")
    assert npm_shim_target(str(tmp_path / "missing.cmd")) is None
    other = tmp_path / "other.cmd"
    other.write_text("@echo off\r\nsomething.exe %*\r\n", encoding="utf-8")
    assert npm_shim_target(str(other)) is None


def _capture_cline(monkeypatch: pytest.MonkeyPatch, exe: str) -> tuple[Any, list[list[str]]]:
    from delta.llm import cli_providers

    seen: list[list[str]] = []

    class Proc:
        returncode = 0

        def __init__(self) -> None:
            done = json.dumps({"type": "run_result", "text": '{"final": "ready"}', "finishReason": "stop"})
            self.stdout = _Lines([done.encode() + b"\n"])
            self.stderr = None

        async def wait(self) -> int:
            return 0

        def kill(self) -> None:
            return None

    async def fake_exec(*cmd: str, **kw: Any) -> Proc:
        seen.append(list(cmd))
        return Proc()

    monkeypatch.setattr(cli_providers.asyncio, "create_subprocess_exec", fake_exec)
    p = cli_providers.ClineProvider("cline", ProviderConfig(kind="cline", models={"chat": ""}))
    p._path = exe
    return p, seen


class _Lines:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = lines

    def __aiter__(self) -> _Lines:
        return self

    async def __anext__(self) -> bytes:
        if not self.lines:
            raise StopAsyncIteration
        return self.lines.pop(0)


def test_cline_cmd_shim_never_gets_the_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shim = _npm_install(tmp_path)
    p, seen = _capture_cline(monkeypatch, str(shim))
    prompt = 'line one\nline two & "quoted" | more'
    resp = asyncio.run(p.complete("chat", [{"role": "user", "content": prompt}], None))
    assert resp.content
    cmd = seen[0]
    assert not cmd[0].lower().endswith(".cmd")
    assert Path(cmd[0]).name == "node.exe"
    assert prompt in cmd[-1]  # the whole prompt, every line, as one argument


def test_cline_batch_file_that_is_not_npm_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bat = tmp_path / "cline.bat"
    bat.write_text("@echo off\r\nC:\\tools\\cline.exe %*\r\n", encoding="utf-8")
    p, seen = _capture_cline(monkeypatch, str(bat))
    with pytest.raises(LLMError) as e:
        asyncio.run(p.complete("chat", [{"role": "user", "content": "hi"}], None))
    assert seen == [] and "cannot run it safely" in str(e.value)


def test_cline_command_line_limit_counts_quoting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every " becomes \\" on a Windows command line: 20k quotes fit the old character check but not Windows."""
    exe = tmp_path / "cline-cli.exe"
    exe.write_text("", encoding="utf-8")
    p, seen = _capture_cline(monkeypatch, str(exe))
    with pytest.raises(LLMError) as e:
        asyncio.run(p.complete("chat", [{"role": "user", "content": '"' * 20000}], None))
    assert e.value.kind == "too_large" and seen == []


def test_cline_desktop_exe_is_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from delta.llm import cli_providers

    app = tmp_path / "Cline" / "cline-cli.exe"
    app.parent.mkdir()
    app.write_text("", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(cli_providers, "find_cli", lambda names, configured=None: None)
    p = cli_providers.ClineProvider("cline", ProviderConfig(kind="cline", models={"chat": ""}))
    assert p.executable() == str(app)
    assert p.supports("chat") and not p.supports("vision")


def test_parallel_helpers_with_a_cli_main_ai_still_use_api_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Helpers never share a CLI session; with Codex as main AI they keep using the API providers, as before."""
    from delta.llm import router as router_mod

    codex, groq = Fake("codex", jobs=("chat", "helper")), Fake("groq", jobs=("chat", "helper"))
    monkeypatch.setattr(router_mod, "_is_cli", lambda p: p.name == "codex")
    r = router("codex", False, codex=codex, groq=groq)
    resp = asyncio.run(r.complete_api_only([{"role": "user", "content": "hi"}], task="helper"))
    assert resp.provider == "groq" and codex.calls == []
