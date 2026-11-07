"""What a brand-new user hit on a clean Windows PC (found by the fresh-user CI journey,
tests/ci/fresh_user_journey.ps1).

- The first message with no AI model answered with router internals ("No model provider is available for task
  'plan'. Errors: lmstudio: not reachable at http://127.0.0.1:1234/v1; ollama: ...") instead of how to add one:
  with no key, the only providers tried are the local servers, which are not running.
- Starting LM Studio or Ollama after that did not help for 5 minutes: the failed call had put the local server to
  rest (UNREACHABLE_COOLDOWN_S), so the top bar still said "No AI model" and every message failed the same way.
- Another program on Delta's port (7878): the Desktop icon opened that program's page in the Delta window and
  never started Delta; `delta run` died on a bind error.
- uninstall.ps1 -RemoveData deleted every saved Windows credential with "delta" anywhere in its name (another
  program's login for a bank or airline called Delta), not just Delta's own keys.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from types import SimpleNamespace

from fakes import FakeProvider, text

from delta.llm.router import ModelRouter
from delta.llm.schemas import AllProvidersFailedError, LLMError

ROOT = Path(__file__).resolve().parents[1]


class LocalServer(FakeProvider):
    """A local OpenAI-style server (LM Studio, Ollama) that is or is not running."""

    def __init__(self, name: str) -> None:
        super().__init__(name, local=True, handler=self._answer)
        self.running = False
        self.pings = 0

    def _answer(self, task: str, messages: list) -> object:
        if not self.running:
            raise LLMError(f"{self.name}: not reachable at http://127.0.0.1:1234/v1", kind="unreachable")
        return text("hello from the local model")

    def ping(self) -> bool:
        self.pings += 1
        return self.running


async def test_no_model_at_all_says_how_to_add_one_not_router_internals():
    from delta.core.orchestrator import friendly_model_error

    lm, ollama = LocalServer("lmstudio"), LocalServer("ollama")
    router = ModelRouter({"lmstudio": lm, "ollama": ollama}, {"plan": ["lmstudio", "ollama"]})
    try:
        await router.complete([], task="plan")
        raise AssertionError("no provider should answer")
    except AllProvidersFailedError as e:
        assert e.nothing_set_up
        msg = friendly_model_error(e)
    assert "No AI model is set up yet" in msg and "Groq key" in msg and "Settings > Keys" in msg
    assert "not reachable" not in msg and "127.0.0.1" not in msg and "Errors:" not in msg


async def test_a_cloud_outage_is_not_reported_as_nothing_set_up():
    from delta.core.orchestrator import friendly_model_error

    down = LLMError("groq: down", kind="unreachable")
    groq = FakeProvider("groq", [down, down])  # the quick network retry fails too
    lm = LocalServer("lmstudio")
    router = ModelRouter({"groq": groq, "lmstudio": lm}, {"plan": ["groq", "lmstudio"]})
    try:
        await router.complete([], task="plan")
        raise AssertionError("no provider should answer")
    except AllProvidersFailedError as e:
        assert not e.nothing_set_up  # a key is set up; the network or the service is down
        assert "No AI model is set up yet" not in friendly_model_error(e)


async def test_a_local_model_started_after_a_failed_message_is_used_at_once():
    lm = LocalServer("lmstudio")
    router = ModelRouter({"lmstudio": lm}, {"chat": ["lmstudio"], "plan": ["lmstudio"]})
    with contextlib.suppress(AllProvidersFailedError):
        await router.complete([], task="plan")
    assert not router.breakers["lmstudio"].available()  # resting: nothing answered
    assert router.candidates("plan") == [] and router.usable() == []  # still not running: no call made
    lm.running = True  # the user starts LM Studio
    assert router.usable() == ["lmstudio"]  # the top bar shows it without waiting out the cooldown
    assert (await router.complete([], task="plan")).content == "hello from the local model"
    assert router.breakers["lmstudio"].available()


async def test_a_provider_resting_after_real_errors_is_not_woken_by_a_ping():
    lm = LocalServer("lmstudio")
    lm.running = True
    router = ModelRouter({"lmstudio": lm}, {"chat": ["lmstudio"]})
    router.breakers["lmstudio"].trip(600)  # e.g. out of quota, not "nothing answered"
    assert router.candidates("chat") == [] and lm.pings == 0


def test_provider_forgets_its_cached_ping(monkeypatch):
    import httpx

    from delta.config import ProviderConfig
    from delta.llm.providers import OpenAICompatProvider

    p = OpenAICompatProvider("lmstudio", ProviderConfig(base_url="http://127.0.0.1:9/v1", local=True,
                                                        models={"chat": "auto"}), None)
    p._ping_at, p._ping_ok = 1e18, True  # a fresh "it answers"
    assert p.ping()
    asked: list[str] = []

    def refused(url: str, **kw: object) -> object:
        asked.append(url)
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", refused)
    assert p.ping() and not asked  # cached
    p.forget_ping()
    assert not p.ping() and asked == ["http://127.0.0.1:9/v1/models"]  # asked again: LM Studio is gone


# --- another program on Delta's port ---------------------------------------------------------------------------
def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(data_dir=tmp_path, api=SimpleNamespace(host="127.0.0.1", port=7878))


def test_icon_does_not_open_another_programs_page(tmp_path, monkeypatch):
    import delta.__main__ as main
    import delta.daemon as daemon

    alerts: list[str] = []
    monkeypatch.setattr(daemon, "load_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(daemon, "_port_open", lambda *a, **k: True)  # something answers on 7878
    monkeypatch.setattr(main, "running_delta", lambda data_dir: None)  # and it is not Delta
    monkeypatch.setattr(daemon, "alert", alerts.append)
    monkeypatch.setattr(daemon, "open_dashboard", lambda *a: (_ for _ in ()).throw(AssertionError("opened")))
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("started")))
    assert daemon.launch_app() == 1
    assert len(alerts) == 1 and "Another program is using Delta's port 127.0.0.1:7878" in alerts[0]
    assert "api: {port: 7879}" in alerts[0] and str(tmp_path / "settings.yaml") in alerts[0]


def test_icon_still_shows_a_running_delta(tmp_path, monkeypatch):
    import delta.__main__ as main
    import delta.daemon as daemon

    opened: list[str] = []
    monkeypatch.setattr(daemon, "load_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(daemon, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(main, "running_delta", lambda data_dir: 4242)  # Delta's own service
    monkeypatch.setattr(daemon, "open_dashboard", lambda s, token: opened.append(token))
    assert daemon.launch_app() == 0 and len(opened) == 1


async def test_service_stops_with_a_clear_message_when_the_port_is_taken(tmp_path, monkeypatch):
    import delta.bootstrap as bootstrap
    import delta.daemon as daemon

    stopped: list[bool] = []

    class FakeContainer:
        def __init__(self) -> None:
            self.lifecycle = SimpleNamespace(stopping=None)

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            stopped.append(True)

    s = _settings(tmp_path)
    monkeypatch.setattr(daemon, "load_settings", lambda: s)
    monkeypatch.setattr(daemon, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(bootstrap, "build", lambda settings: FakeContainer())
    monkeypatch.setattr("delta.system.crashguard.guard_of",
                        lambda c: SimpleNamespace(install_threads=lambda: None, install_loop=lambda loop: None))
    try:
        await daemon.run()
        raise AssertionError("run should refuse a taken port")
    except daemon.PortTakenError as e:
        assert "Another program is using Delta's port" in str(e)
    assert stopped == [True]  # what start() opened is closed again


# --- uninstall -------------------------------------------------------------------------------------------------
def _uninstall_pattern() -> re.Pattern[str]:
    ps = (ROOT / "scripts" / "uninstall.ps1").read_text(encoding="utf-8")
    m = re.search(r"cmdkey /list \| Select-String -Pattern '([^']+)'", ps)
    assert m, "uninstall.ps1 must pick Delta's keys from `cmdkey /list` with one Select-String pattern"
    return re.compile(m.group(1), re.I)  # Select-String is case-insensitive, like this


def test_uninstall_removes_only_deltas_own_saved_keys():
    pattern = _uninstall_pattern()
    listing = {
        "    Target: LegacyGeneric:target=delta-agent": "delta-agent",
        "    Target: LegacyGeneric:target=groq_api_key@delta-agent": "groq_api_key@delta-agent",
        "    Ziel: LegacyGeneric:target=telegram_bot_token@delta-agent": "telegram_bot_token@delta-agent",  # German
        "    Target: LegacyGeneric:target=deltadental.com": None,
        "    Target: LegacyGeneric:target=MyDeltaAirlines": None,
        "    Target: Domain:target=delta-corp-vpn": None,
        "    User: delta-agent-fan": None,
        "    Target: WindowsLive:target=virtualapp/didlogical": None,
    }
    for line, want in listing.items():
        m = pattern.search(line)
        assert (m.group(1) if m else None) == want, line


def test_uninstall_matches_the_service_name_delta_saves_keys_under():
    from delta.system import secrets

    assert secrets.SERVICE == "delta-agent"


def test_installer_keeps_its_download_caches_in_deltas_own_folder():
    """uv's shared cache kept about 500 MB on the PC after uninstall (fresh-user CI journey); pip's another 15 MB."""
    ps = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    assert 'if (-not "$env:UV_CACHE_DIR".Trim()) { $env:UV_CACHE_DIR = Join-Path $InstallDir ".uv-cache" }' in ps
    assert "pip install --quiet --disable-pip-version-check --no-cache-dir --upgrade uv" in ps
    robocopy = next(line for line in ps.splitlines() if line.strip().startswith("robocopy"))
    assert re.search(r"/XD .*\.uv-cache ", robocopy)  # an update keeps (and reuses) the cache
    assert '$cache = (Join-Path $dir ".uv-cache")' in ps  # not walked file by file to unblock
    # the user's own window gets its uv settings back (irm | iex runs in it)
    assert '[Environment]::SetEnvironmentVariable($k, $uvSaved[$k])' in ps
    assert ps.index("$uvSaved[$k] = ") < ps.index("$env:UV_PROJECT_ENVIRONMENT = ")
