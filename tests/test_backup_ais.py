"""Gemini and OpenRouter as providers, and backup AIs: when the main AI runs out of credits or hits its limit, the
user's own backups answer, on every call while it rests (before, only on the first call and then 10 minutes of
"main AI could not answer").

The rotation test at the end goes through real HTTP and the real OpenAI client, so the error handling of the
providers (402 = out of credits, 429 = limit, Google's 400 = unknown key) is part of what is checked."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient

from delta.config import ProviderConfig, load_settings, read_user_settings
from delta.llm import router as router_mod
from delta.llm.capabilities import BRAINS
from delta.llm.providers import OpenAICompatProvider, display_name
from delta.llm.router import AllProvidersFailedError, ModelRouter
from delta.llm.schemas import LLMError, LLMResponse
from delta.setup import checks
from delta.system.secrets import KNOWN_SECRETS

# --- the two providers -------------------------------------------------------------------------------------


def test_gemini_and_openrouter_are_providers_on_the_right_routes():
    s = load_settings()
    gemini, openrouter = s.llm.providers["gemini"], s.llm.providers["openrouter"]
    assert gemini.base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert gemini.secret == "gemini_api_key" and gemini.kind == "openai" and not gemini.local
    # models of the free tier; gemini-2.5-flash is not used (Google retires it on 1 December 2026)
    assert gemini.models == {"chat": "gemini-3.8-flash", "plan": "gemini-3.8-flash",
                             "verify": "gemini-3.5-flash-lite", "vision": "gemini-3.8-flash"}
    assert openrouter.base_url == "https://openrouter.ai/api/v1" and openrouter.secret == "openrouter_api_key"
    assert set(openrouter.models.values()) == {"openrouter/free"} and "vision" not in openrouter.models
    for task in ("chat", "plan", "verify"):
        assert "gemini" in s.llm.routes[task] and "openrouter" in s.llm.routes[task]
    assert "gemini" in s.llm.routes["vision"] and "openrouter" not in s.llm.routes["vision"]
    # a free, key-based AI never goes in front of the user's own plans or local models
    chat = s.llm.routes["chat"]
    assert chat.index("groq") < chat.index("gemini") < chat.index("nebius")
    assert display_name("gemini") == "Gemini" and display_name("openrouter") == "OpenRouter"
    assert "gemini" in BRAINS and "openrouter" in BRAINS
    assert "gemini_api_key" in checks.KEY_INFO and "openrouter_api_key" in checks.KEY_INFO
    assert "gemini_api_key" in KNOWN_SECRETS and "openrouter_api_key" in KNOWN_SECRETS


def test_the_setup_wizard_has_a_row_for_each_key():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "src" / "delta" / "api" / "static" / "index.html").read_text(encoding="utf-8")
    for key in ("gemini_api_key", "openrouter_api_key"):
        assert f'data-key="{key}"' in html


@pytest.fixture
def seen(monkeypatch):
    """Answers key checks from a table keyed by the URL; records what was asked."""
    calls: list[tuple[str, dict]] = []
    table: dict[str, httpx.Response] = {}

    def handler(request):
        calls.append((str(request.url), dict(request.headers)))
        return table[str(request.url)]

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return calls, table


GEMINI_MODELS = "https://generativelanguage.googleapis.com/v1beta/openai/models"
OPENROUTER_KEY = "https://openrouter.ai/api/v1/key"


async def test_a_gemini_key_is_checked_and_googles_400_for_an_unknown_key_is_said_plainly(seen):
    calls, table = seen
    table[GEMINI_MODELS] = httpx.Response(200, json={"data": []})
    assert await checks.validate_key("gemini_api_key", "AIzaGood") == (True, "Key works.")
    assert calls[0][0] == GEMINI_MODELS and calls[0][1]["authorization"] == "Bearer AIzaGood"
    # observed live: an unknown key is HTTP 400 with "API key not valid", not 401
    table[GEMINI_MODELS] = httpx.Response(400, json={"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                                               "message": "API key not valid. Please pass a valid "
                                                                          "API key."}})
    ok, msg = await checks.validate_key("gemini_api_key", "AIzaBad")
    assert not ok and "rejected" in msg
    table[GEMINI_MODELS] = httpx.Response(400, json={"error": {"message": "something else"}})
    ok, msg = await checks.validate_key("gemini_api_key", "AIzaOdd")
    assert not ok and "error (400)" in msg  # only a message about the key counts as a refused key


async def test_an_openrouter_key_is_checked_on_the_key_endpoint_because_models_is_public(seen):
    calls, table = seen
    table[OPENROUTER_KEY] = httpx.Response(200, json={"data": {"label": "x"}})
    assert await checks.validate_key("openrouter_api_key", "sk-or-v1-good") == (True, "Key works.")
    assert calls[0][0] == OPENROUTER_KEY and calls[0][1]["authorization"] == "Bearer sk-or-v1-good"
    table[OPENROUTER_KEY] = httpx.Response(401, json={"error": {"message": "User not found.", "code": 401}})
    ok, msg = await checks.validate_key("openrouter_api_key", "sk-or-v1-bad")
    assert not ok and "rejected" in msg


# --- a fake of the three services, over real HTTP ----------------------------------------------------------

CHAT_OK = {"id": "c", "object": "chat.completion", "created": 0, "model": "m", "usage": {
    "prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}, "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hello from {who}"}}]}


class Services:
    """/<name>/v1/chat/completions answers by what `mode[name]` says; counts the calls each one got."""

    def __init__(self):
        self.mode: dict[str, str] = {}
        self.hits: dict[str, int] = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                name = self.path.split("/")[1]
                outer.hits[name] = outer.hits.get(name, 0) + 1
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                mode = outer.mode.get(name, "ok")
                status, body, headers = 200, json.loads(json.dumps(CHAT_OK).replace("{who}", name)), {}
                if mode == "credits":      # OpenRouter and others: out of credits
                    status, body = 402, {"error": {"code": 402, "message": "Insufficient credits. Add more at ..."}}
                elif mode == "daily":      # OpenRouter's free models, per day
                    status, body = 429, {"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-day. "
                                                                          "Add 10 credits to unlock 1000 free model "
                                                                          "requests per day"}}
                elif mode == "gemini-daily":  # Google's free tier: the day's quota is spelled without spaces
                    status, body = 429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": (
                        "You exceeded your current quota. Quota exceeded for metric: generativelanguage.googleapis"
                        ".com/generate_content_free_tier_requests, limit: 20. quotaId: GenerateRequestsPerDayPer"
                        "ProjectPerModel-FreeTier")}}
                elif mode == "minute":     # a short limit that clears in seconds
                    status, body = 429, {"error": {"code": 429, "message": "Rate limit exceeded: try again in 2s"}}
                elif mode == "badkey":     # Google: 400, and a list around the error, as seen live
                    status, body = 400, [{"error": {"code": 400, "message": "Please pass a valid API key",
                                                    "status": "INVALID_ARGUMENT"}}]
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def provider(self, name, model="m"):
        cfg = ProviderConfig(base_url=f"{self.base}/{name}/v1", secret="x", timeout_s=10,
                             models={"chat": model, "plan": model, "verify": model})
        return OpenAICompatProvider(name, cfg, "key")

    def close(self):
        self.server.shutdown()


@pytest.fixture
def services():
    s = Services()
    yield s
    s.close()


async def test_each_services_own_error_becomes_the_right_kind_of_failure(services):
    messages = [{"role": "user", "content": "hi"}]
    for name, mode, kind, words in [("openrouter", "credits", "quota", "credits are used up"),
                                    ("openrouter", "daily", "quota", "OpenRouter limit for today is used up"),
                                    ("gemini", "gemini-daily", "quota", "Gemini limit for today is used up"),
                                    ("openrouter", "minute", "rate_limit", "rate limited"),  # waited out, as before
                                    ("gemini", "badkey", "auth", "Gemini key expired or invalid")]:
        services.mode[name] = mode
        with pytest.raises(LLMError) as e:
            await services.provider(name).complete("chat", messages, None)
        assert e.value.kind == kind and words in str(e.value), (name, mode, str(e.value))
    services.mode["gemini"] = "ok"
    assert (await services.provider("gemini").complete("chat", messages, None)).content == "hello from gemini"


# --- backup AIs: the router ---------------------------------------------------------------------------------


class Fake:
    local = False

    def __init__(self, name, fail=None, retry_after=None):
        self.name, self.fail, self.retry_after, self.calls = name, fail, retry_after, 0

    def supports(self, task):
        return True

    handles = supports

    async def complete(self, task, messages, tools=None, json_mode=False):
        self.calls += 1
        if self.fail:
            raise LLMError(f"{self.name}: {self.fail[1]}", kind=self.fail[0], retry_after_s=self.retry_after)
        return LLMResponse(content=f"answer from {self.name}")


def rig(main, *backups, listed=None):
    providers = {p.name: p for p in (main, *backups)}
    r = ModelRouter(providers, {"chat": list(providers)}, preferred=main.name, fallback=False)
    r.fallbacks = [p.name for p in backups] if listed is None else listed
    return r


ASK = [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize("kind, why", [("quota", "your Groq credits are used up"),
                                       ("quota", "your Groq limit for today is used up"),
                                       ("auth", "key expired or invalid")])
async def test_a_main_ai_that_cannot_answer_hands_over_on_every_call_while_it_rests(kind, why):
    main, backup = Fake("groq", (kind, why)), Fake("gemini")
    r = rig(main, backup)
    for _ in range(4):
        assert (await r.complete(ASK, task="chat")).content == "answer from gemini"
    assert main.calls == 1 and backup.calls == 4  # the main AI is asked once, then left to rest
    assert r.fallback_used == "gemini" and r.last_provider == "gemini"


@pytest.mark.parametrize("kind, why", [("rate_limit", "rate limited"), ("unreachable", "not reachable")])
async def test_a_busy_or_unreachable_main_ai_is_retried_as_before_and_the_backup_answers_meanwhile(
        kind, why, monkeypatch):
    async def no_wait(s):
        return None

    monkeypatch.setattr(router_mod.asyncio, "sleep", no_wait)
    main, backup = Fake("groq", (kind, why)), Fake("gemini")
    r = rig(main, backup)
    for _ in range(5):
        assert (await r.complete(ASK, task="chat")).content == "answer from gemini"
    assert backup.calls == 5 and main.calls >= 1


async def test_the_main_ai_is_used_again_when_it_is_back_and_a_new_key_clears_the_rest():
    main, backup = Fake("groq", ("quota", "credits")), Fake("gemini")
    r = rig(main, backup)
    await r.complete(ASK, task="chat")
    main.fail = None
    r.refresh()  # the user saved a key or topped up
    assert (await r.complete(ASK, task="chat")).content == "answer from groq" and not r.down_kind


async def test_other_failures_are_not_a_reason_to_switch_and_unlisted_ais_are_never_used():
    for fail in (("error", "HTTP 400: bad request"), ("bad_tool_call", "malformed tool call"),
                 ("too_large", "request too large")):
        main, backup = Fake("groq", fail), Fake("gemini")
        with pytest.raises(AllProvidersFailedError):
            await rig(main, backup).complete(ASK, task="chat")
        assert backup.calls == 0, fail
    main, listed, unlisted = Fake("groq", ("quota", "credits")), Fake("gemini"), Fake("openrouter")
    r = rig(main, listed, unlisted, listed=["gemini"])
    for _ in range(3):
        assert (await r.complete(ASK, task="chat")).content == "answer from gemini"
    assert unlisted.calls == 0  # "never a provider you did not list"


async def test_with_no_backup_the_user_is_told_how_to_get_one():
    main = Fake("groq", ("quota", "your Groq credits are used up"))
    with pytest.raises(AllProvidersFailedError) as e:
        await rig(main, listed=[]).complete(ASK, task="chat")
    msg = e.value.user_message
    assert "could not answer" in msg and "fallback is off" in msg and "Gemini or OpenRouter" in msg
    with pytest.raises(AllProvidersFailedError) as e:  # the second call, while the main AI rests
        await rig(main, listed=[]).complete(ASK, task="chat")
    assert "backup AIs" in e.value.user_message


async def test_every_backup_failing_says_what_happened_to_each(monkeypatch):
    async def no_wait(s):
        return None

    monkeypatch.setattr(router_mod.asyncio, "sleep", no_wait)
    main, one, two = Fake("groq", ("quota", "credits")), Fake("gemini", ("rate_limit", "limit")), Fake("openrouter")
    two.fail = ("quota", "your OpenRouter credits are used up")
    r = rig(main, one, two)
    with pytest.raises(AllProvidersFailedError) as e:  # the first call finds out about all three
        await r.complete(ASK, task="chat")
    text = " ".join(e.value.errors)
    assert "groq" in text and "gemini" in text and "openrouter" in text, text
    with pytest.raises(AllProvidersFailedError) as e:  # then the two that ran out rest; only Gemini is asked again
        await r.complete(ASK, task="chat")
    text = " ".join(e.value.errors)
    assert "groq" in text and "gemini" in text and "openrouter" not in text, text
    assert two.calls == 1  # an AI that is out of credits is not asked on every message


async def test_a_long_rate_limit_on_the_main_ai_hands_over_at_once_when_a_backup_is_ready(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(router_mod.asyncio, "sleep", fake_sleep)
    main, backup = Fake("groq", ("rate_limit", "rate limited"), retry_after=45), Fake("gemini")
    assert (await rig(main, backup).complete(ASK, task="chat")).content == "answer from gemini"
    assert slept == []  # it used to wait up to 60 s for the last candidate; with a backup that is wasted time
    slept.clear()
    only = Fake("groq", ("rate_limit", "rate limited"), retry_after=45)
    with pytest.raises(AllProvidersFailedError):
        await rig(only, listed=[]).complete(ASK, task="chat")
    assert slept and slept[0] > 40  # no backup: waiting is still better than failing the task


# --- the whole thing over real HTTP ----------------------------------------------------------------------


async def test_out_of_credits_on_the_main_ai_rotates_to_gemini_then_openrouter_over_real_http(services):
    messages = [{"role": "user", "content": "hi"}]
    groq, gemini, openrouter = services.provider("groq"), services.provider("gemini"), services.provider("openrouter")
    r = ModelRouter({"groq": groq, "gemini": gemini, "openrouter": openrouter},
                    {"chat": ["groq", "gemini", "openrouter"]}, preferred="groq", fallback=False)
    r.fallbacks = ["gemini", "openrouter"]
    services.mode.update(groq="credits")
    answers = [(await r.complete(messages, task="chat")).content for _ in range(3)]
    assert answers == ["hello from gemini"] * 3
    assert services.hits == {"groq": 1, "gemini": 3}  # the main AI got one call, not one per message
    services.mode.update(gemini="gemini-daily")  # the free Gemini quota for the day is used up as well
    assert (await r.complete(messages, task="chat")).content == "hello from openrouter"
    assert (await r.complete(messages, task="chat")).content == "hello from openrouter"
    assert (await r.complete(messages, task="chat")).content == "hello from openrouter"
    # Gemini was asked once more (that call found out), then left to rest: no waiting, no retrying per message
    assert services.hits == {"groq": 1, "gemini": 4, "openrouter": 3}
    assert "limit for today" in r.why_down["gemini"] and r.down_kind["gemini"] == "quota"
    # a bad Gemini key (400) is a refused key, which also hands over
    r2 = ModelRouter({"gemini": services.provider("gemini"), "openrouter": services.provider("openrouter")},
                     {"chat": ["gemini", "openrouter"]}, preferred="gemini", fallback=False)
    r2.fallbacks = ["openrouter"]
    services.mode.update(gemini="badkey", openrouter="ok")
    assert (await r2.complete(messages, task="chat")).content == "hello from openrouter"
    assert "Gemini key expired or invalid" in r2.why_down["gemini"]


# --- backup AIs: the Settings page ----------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    from delta.api.server import create_app, load_token
    from delta.bootstrap import build

    monkeypatch.setenv("DELTA_DATA_DIR", str(tmp_path))
    c = build(load_settings())
    token = load_token(tmp_path)
    with TestClient(create_app(c, token)) as tc:
        tc.headers["Authorization"] = f"Bearer {token}"
        yield tc, c


def test_only_your_own_api_keys_can_be_backups(client):
    tc, c = client
    choices = {o["name"] for o in tc.get("/api/apps/preferred").json()["backup_choices"]}
    assert {"gemini", "openrouter", "groq", "nim", "nebius", "deepseek"} <= choices
    assert not choices & {"claude_code", "codex", "cline", "ollama", "lmstudio"}  # a plan or a local model: never
    for bad in ("claude_code", "codex", "ollama", "lmstudio", "nothing"):
        r = tc.put("/api/apps/backups", json={"names": ["gemini", bad]})
        assert r.status_code == 400 and bad in r.json()["detail"], bad
    assert c.router.fallbacks == []  # a refused list changes nothing


def test_the_backups_are_saved_and_used_from_the_next_message(client):
    tc, c = client
    r = tc.put("/api/apps/backups", json={"names": ["gemini", "openrouter", "gemini"]}).json()
    assert r == {"ok": True, "backups": ["gemini", "openrouter"]}  # in the order given, once each
    assert c.router.fallbacks == ["gemini", "openrouter"] == c.settings.router.fallbacks
    assert read_user_settings(c.settings.data_dir)["router"]["fallbacks"] == ["gemini", "openrouter"]
    assert tc.get("/api/apps/preferred").json()["backups"] == ["gemini", "openrouter"]
    assert tc.put("/api/apps/backups", json={"names": []}).json()["backups"] == []
    assert c.router.fallbacks == [] and read_user_settings(c.settings.data_dir)["router"]["fallbacks"] == []


def test_the_backup_choice_needs_the_token(client):
    tc, _ = client
    saved = tc.headers.pop("Authorization")
    try:
        assert tc.put("/api/apps/backups", json={"names": ["gemini"]}).status_code == 401
    finally:
        tc.headers["Authorization"] = saved


def test_a_server_restart_keeps_the_backups(tmp_path, monkeypatch):
    from delta.bootstrap import build

    monkeypatch.setenv("DELTA_DATA_DIR", str(tmp_path))
    first = build(load_settings())
    from delta.api.apps_routes import backup_allowed

    assert backup_allowed(first, "gemini") and not backup_allowed(first, "codex")
    from delta.config import write_user_settings

    write_user_settings(tmp_path, {"router": {"fallbacks": ["gemini", "ghost"]}})
    again = build(load_settings())
    assert again.router.fallbacks == ["gemini"]  # a name that no longer exists is dropped, not an error


def test_the_asyncio_loop_is_not_needed_here():
    assert asyncio.iscoroutinefunction(ModelRouter.complete)
