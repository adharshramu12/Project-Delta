"""Connected apps (delta.connectors): catalog, OAuth, transports, reliability, and the safety gate around app tools.

Everything runs against the fake notes app (delta.evals.fakemcp) on 127.0.0.1, or its stdio twin: no network, no
real accounts. Secrets are sealed with a test sealer here (on Windows CI the real DPAPI path has its own test)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest

from delta.config import Boundaries, Settings
from delta.connectors.catalog import (
    AppSpec,
    Catalog,
    RuntimeMissingError,
    command_for,
    custom_spec,
    load_catalog,
)
from delta.connectors.manager import AppManager
from delta.connectors.manager import Settings as MSettings
from delta.connectors.oauth import LoopbackReceiver, OAuthError, discover
from delta.connectors.secrets import SecretBox, SecretsUnavailableError
from delta.connectors.tools import AppTools, resolve_risk, tool_enabled
from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.evals.fakemcp import INJECTION, FakeApp, auto_consent
from delta.evals.standin_vault import STANDIN
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine
from delta.safety.remote_guard import RemoteGuard
from delta.tools.base import Risk
from delta.tools.registry import ToolExecutor, ToolRegistry

REPO = Path(__file__).resolve().parents[1]
FAST = MSettings(read_retries=2, backoff_s=0.01, breaker_failures=3, breaker_cooldown_s=60, call_timeout_s=5,
                 connect_timeout_s=10, sign_in_timeout_s=20)


def box(folder: Path) -> SecretBox:
    return SecretBox(folder / "secrets", seal=STANDIN.seal, unseal=STANDIN.unseal)


def fake_spec(app: FakeApp, auth: str = "oauth", **kw: Any) -> AppSpec:
    return AppSpec(id="fake", name="Fake Notes", status="official", url=app.url, auth={"type": auth},
                   **({"sse_url": app.base + "/sse"} | kw))


def manager(tmp_path: Path, spec: AppSpec, **kw: Any) -> AppManager:
    return AppManager(tmp_path, Catalog({spec.id: spec}), kw.pop("settings", FAST), secrets=box(tmp_path),
                      open_browser=auto_consent, **kw)


async def connected(tmp_path: Path, app: FakeApp, auth: str = "oauth", **kw: Any) -> AppManager:
    m = manager(tmp_path, fake_spec(app, auth), **kw)
    r = await m.connect("fake", token=app.static_token if auth == "token" else "")
    if r["status"] == "sign_in":
        r = await m.wait_connect("fake", 20)
    assert r["status"] == "connected", r
    return m


def stdio_spec(*extra: str) -> AppSpec:
    return AppSpec(id="local", name="Local Notes", status="reference", transport="stdio", runtime="command",
                   command=sys.executable, args=["-m", "delta.evals.fakemcp", "stdio", *extra])


# --- the catalog -----------------------------------------------------------------------------------------------

def test_shipped_catalog_is_valid_and_sourced():
    cat = load_catalog(REPO / "config" / "mcp_catalog.yaml")
    assert not cat.problems
    assert {"notion", "github", "slack", "linear", "atlassian", "sentry", "stripe", "gmail"} <= set(cat.apps)
    for spec in cat.list():
        assert spec.docs.startswith("https://") and spec.checked, spec.id
        assert spec.status in ("official", "official-preview", "reference"), spec.id
        if spec.transport == "stdio":
            assert spec.runtime in ("node", "python") and ("@" in spec.package or "==" in spec.package)


def test_catalog_refuses_plain_http_and_unpinned_packages(tmp_path):
    with pytest.raises(ValueError):
        AppSpec(id="bad", name="Bad", url="http://example.com/mcp")
    AppSpec(id="local", name="Local", url="http://127.0.0.1:3845/mcp")  # this PC is fine
    with pytest.raises(ValueError):
        AppSpec(id="np", name="NP", transport="stdio", runtime="node", package="some-server")
    f = tmp_path / "c.yaml"
    f.write_text("apps:\n  ok: {name: OK, url: https://ok.example/mcp}\n  bad: {name: Bad, url: http://x.example}\n")
    cat = load_catalog(f)
    assert set(cat.apps) == {"ok"} and cat.problems  # a broken entry is skipped, never fatal


def test_missing_node_or_uv_says_how_to_install():
    node = AppSpec(id="nd", name="N", transport="stdio", runtime="node", package="@x/server@1.0.0")
    with pytest.raises(RuntimeMissingError, match=r"nodejs\.org"):
        command_for(node, which=lambda name: None)
    py = AppSpec(id="py", name="P", transport="stdio", runtime="python", package="mcp-server-fetch==1.0")
    with pytest.raises(RuntimeMissingError, match="uv"):
        command_for(py, which=lambda name: None)
    assert command_for(py, which=lambda name: "/bin/uvx") == ["/bin/uvx", "--from", "mcp-server-fetch==1.0",
                                                              "mcp-server-fetch"]


async def test_missing_node_message_on_connect(tmp_path):
    spec = AppSpec(id="files", name="Files", status="reference", transport="stdio", runtime="node",
                   package="@modelcontextprotocol/server-filesystem@2.2.0", args=["{folder}"],
                   inputs=[{"name": "folder"}])
    m = AppManager(tmp_path, Catalog({"files": spec}), FAST, secrets=box(tmp_path), which=lambda n: None)
    r = await m.connect("files", inputs={"folder": str(tmp_path)})
    assert r["status"] == "error" and r["missing_runtime"] and "Node.js" in r["message"]
    assert m.store.get("files") is None


async def test_custom_server_needs_a_confirmation_showing_exactly_what_runs(tmp_path):
    m = AppManager(tmp_path, Catalog({}), FAST, secrets=box(tmp_path))
    custom = {"name": "Mine", "command": f'"{sys.executable}" -m delta.evals.fakemcp stdio'}
    first = await m.connect("mine", custom=custom)
    assert first["status"] == "confirm" and sys.executable in first["message"]
    assert "delta.evals.fakemcp" in " ".join(first["command"]) and m.store.get("mine") is None
    done = await m.connect("mine", custom=custom, confirm=True)
    assert done["status"] == "connected"
    assert m.store.get("mine").custom["status"] == "custom"
    await m.disconnect("mine")
    await m.close()
    with pytest.raises(ValueError):
        custom_spec("x", "x", url="https://a.example", command="b")


# --- OAuth -----------------------------------------------------------------------------------------------------

async def test_oauth_connect_uses_dcr_pkce_and_seals_tokens(tmp_path):
    with FakeApp() as app:
        m = await connected(tmp_path, app)
        assert len(app.clients) == 1 and app.active_grants() == 1
        sealed = (tmp_path / "connectors" / "secrets" / "secrets" / "fake.bin")
        sealed = next((tmp_path).rglob("fake.bin"))
        token = next(iter(app.access))
        assert token not in sealed.read_text() and sealed.read_text().startswith("standin:")
        assert token not in (tmp_path / "connectors" / "connections.json").read_text()
        out = await m.call("fake", "search_notes", {"query": "delta"}, Risk.READ)
        assert out.ok and "Project Delta" in out.text
        await m.close()


async def test_expired_token_is_refreshed(tmp_path):
    with FakeApp() as app:
        m = await connected(tmp_path, app)
        app.expire_access_tokens()  # the server ends the access token early: 401, refresh, retry
        out = await m.call("fake", "whoami", {}, Risk.READ)
        assert out.ok and app.refreshes == 1
        saved = m.secrets.load("fake")  # a token Delta knows is expired is refreshed before the call
        saved["expires_at"] = 1.0
        m.secrets.save("fake", saved)
        await m._drop("fake")
        out = await m.call("fake", "whoami", {}, Risk.READ)
        assert out.ok and app.refreshes == 2
        await m.close()


async def test_revoked_sign_in_says_reconnect(tmp_path):
    with FakeApp() as app:
        m = await connected(tmp_path, app)
        app.revoke_everything()
        await m._drop("fake")
        out = await m.call("fake", "whoami", {}, Risk.READ)
        assert not out.ok and out.evidence["reconnect"]
        assert "Reconnect Fake Notes" in out.text and "delta apps connect fake" in out.text
        assert m.status()[0]["state"] == "needs reconnect"
        await m.close()


async def test_disconnect_revokes_and_deletes(tmp_path):
    with FakeApp() as app:
        m = await connected(tmp_path, app)
        r = await m.disconnect("fake")
        assert r["status"] == "disconnected" and r["revoked"]
        assert app.active_grants() == 0 and not m.secrets.exists("fake") and m.store.get("fake") is None


async def test_loopback_only_accepts_the_matching_state():
    import httpx

    rx = LoopbackReceiver("right-state")
    redirect = await rx.start()
    assert redirect.startswith("http://127.0.0.1:")
    async with httpx.AsyncClient(trust_env=False) as c:
        bad = await c.get(redirect + "?code=evil&state=wrong")
        assert bad.status_code == 400 and not rx.result.done()
        assert (await c.get(redirect.replace("/callback", "/other"))).status_code == 404
        await c.get(redirect + "?code=good&state=right-state")
    assert (await rx.wait(5)) == {"code": "good"}


async def test_discovery_requires_pkce():
    import httpx

    with FakeApp() as app:
        async with httpx.AsyncClient(trust_env=False) as c:
            server = await discover(app.url, "", c)
            assert server.registration_endpoint and server.resource == app.url
    plain = httpx.MockTransport(lambda req: httpx.Response(200, json={
        "authorization_endpoint": "https://as.example/a", "token_endpoint": "https://as.example/t"})
        if "authorization-server" in req.url.path else httpx.Response(404))
    async with httpx.AsyncClient(transport=plain) as c:
        with pytest.raises(OAuthError, match="PKCE"):
            await discover("https://app.example/mcp", "", c)


async def test_user_client_needs_a_client_id(tmp_path):
    with FakeApp(dcr=False) as app:
        spec = fake_spec(app)
        spec.auth.client = "user"
        m = manager(tmp_path, spec)
        r = await m.connect("fake")
        assert r["status"] == "error" and r["needs"] == "client_id"
        await m.close()


async def test_tokens_are_never_stored_without_dpapi(tmp_path, monkeypatch):
    from delta.system import vault

    monkeypatch.setattr(vault, "available", lambda: False)
    real = SecretBox(tmp_path / "s")
    with pytest.raises(SecretsUnavailableError):
        real.save("x", {"token": "secret-value"})
    assert not list((tmp_path).rglob("*.bin"))
    with FakeApp(auth="token") as app:
        m = AppManager(tmp_path, Catalog({"fake": fake_spec(app, "token")}), FAST, secrets=real)
        r = await m.connect("fake", token="fake-pat-123")
        assert r["status"] == "error" and "DPAPI" in r["message"] and m.store.get("fake") is None


# --- token and no-auth servers, transports ---------------------------------------------------------------------

async def test_token_auth_bad_token_is_not_kept(tmp_path):
    with FakeApp(auth="token") as app:
        m = manager(tmp_path, fake_spec(app, "token"))
        r = await m.connect("fake", token="wrong")
        assert r["status"] == "error" and not m.secrets.exists("fake")
        r = await m.connect("fake", token="fake-pat-123")
        assert r["status"] == "connected"
        await m.close()


async def test_sse_fallback_and_event_stream_answers(tmp_path):
    with FakeApp(auth="none", streamable=False) as app:
        m = await connected(tmp_path / "a", app, auth="none")
        assert type(m.sessions["fake"].transport).__name__ == "SseTransport"
        assert (await m.call("fake", "whoami", {}, Risk.READ)).ok
        await m.close()
    with FakeApp(auth="none", sse_answers=True) as app:
        m = await connected(tmp_path / "b", app, auth="none")
        assert type(m.sessions["fake"].transport).__name__ == "HttpTransport"
        assert (await m.call("fake", "read_note", {"id": "n1"}, Risk.READ)).text.startswith("Shopping")
        await m.close()


async def test_stdio_server_runs_with_scrubbed_env_and_is_cleaned_up(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    seen: dict[str, str] = {}

    def env(extra: dict[str, str]) -> dict[str, str]:
        from delta.system import childenv

        out = childenv.scrubbed_env(extra)
        seen.update(out)
        return out

    spec = stdio_spec()
    m = AppManager(tmp_path, Catalog({"local": spec}), FAST, secrets=box(tmp_path), env=env)
    assert (await m.connect("local"))["status"] == "connected"
    assert "OPENAI_API_KEY" not in seen and "PATH" in seen
    out = await m.call("local", "read_note", {"id": "n1"}, Risk.READ)
    assert out.ok and "milk" in out.text
    pid = m.sessions["local"].transport.pid
    assert pid and psutil.pid_exists(pid)
    await m.close()
    await asyncio.sleep(0.2)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


# --- reliability -----------------------------------------------------------------------------------------------

async def test_reads_retry_transient_errors_writes_do_not(tmp_path):
    with FakeApp(auth="none") as app:
        m = await connected(tmp_path, app, auth="none")
        app.fail_next = 1
        out = await m.call("fake", "search_notes", {"query": "milk"}, Risk.READ)
        assert out.ok and out.evidence["attempts"] == 2
        app.fail_next = 1
        out = await m.call("fake", "create_note", {"title": "x"}, Risk.EXTERNAL)
        assert not out.ok and app.logic.count("create_note") == 0 and "not answering" in out.text
        await m.close()


async def test_circuit_breaker_and_timeout(tmp_path):
    with FakeApp(auth="none") as app:
        settings = MSettings(read_retries=0, backoff_s=0.01, breaker_failures=2, breaker_cooldown_s=60,
                             call_timeout_s=5, connect_timeout_s=10)
        m = await connected(tmp_path, app, auth="none", settings=settings)
        m.cfg.call_timeout_s = 0.5
        out = await m.call("fake", "slow_note", {"seconds": 3}, Risk.READ)
        assert not out.ok and "timed out" in out.text
        m.cfg.call_timeout_s = 5
        app.fail_next = 5
        await m.call("fake", "whoami", {}, Risk.READ)
        out = await m.call("fake", "whoami", {}, Risk.READ)
        assert not out.ok and out.evidence.get("breaker") == "open"
        assert m.status()[0]["state"] == "paused"
        await m.close()


# --- risk, lazy loading and the safety gate ---------------------------------------------------------------------

def test_risk_order_follows_the_mcp_trust_rules():
    from delta.connectors.store import Connection

    spec = AppSpec(id="ab", name="A", url="https://a.example/mcp", tools={"read": ["list_*"]})
    conn = Connection("ab")
    ro = {"name": "get_thing", "annotations": {"readOnlyHint": True}}
    assert resolve_risk(spec, conn, ro) == Risk.EXTERNAL  # a server's own label is not trusted by default
    conn.trust_read_only_hints = True
    assert resolve_risk(spec, conn, ro) == Risk.READ
    assert resolve_risk(spec, conn, {"name": "list_items"}) == Risk.READ  # Delta's verified default
    destructive = {"name": "list_wipe", "annotations": {"destructiveHint": True}}
    assert resolve_risk(spec, conn, destructive) == Risk.DESTRUCTIVE  # the server can only be more careful
    assert not tool_enabled(conn, destructive, Risk.DESTRUCTIVE)  # off until switched on
    conn.choices["get_thing"] = {"risk": "external"}
    assert resolve_risk(spec, conn, ro) == Risk.EXTERNAL  # the user's own choice wins


class Gate:
    def __init__(self, tmp_path: Path, m: AppManager, autonomy: str = "ask-risky", approve: bool = False,
                 guard: Any = None) -> None:
        self.registry = ToolRegistry([])
        self.apps = AppTools(m, self.registry)
        self.asked: list[Any] = []
        self.approvals = ApprovalBroker(timeout_s=2)

        async def present(req: Any) -> None:
            self.asked.append(req)
            asyncio.get_running_loop().call_soon(self.approvals.resolve, req.id, approve, "test")

        self.approvals.add_presenter("test", present)
        self.ex = ToolExecutor(self.registry, PolicyEngine(Boundaries(autonomy=autonomy)), self.approvals,
                               AuditLog(tmp_path / "audit.jsonl"))
        self.ex.guard = guard

    async def run(self, tool: str, args: dict[str, Any], channel: str = "api", user: str = "") -> Any:
        token = CURRENT_REQUEST.set(TaskRequest("do it", channel=channel, user=user))
        try:
            return await self.ex.execute(tool, args, "task1")
        finally:
            CURRENT_REQUEST.reset(token)


async def test_lazy_loading_only_two_small_tools_reach_the_model(tmp_path):
    with FakeApp(auth="none") as app:
        m = await connected(tmp_path, app, auth="none")
        g = Gate(tmp_path, m)
        names = [s["function"]["name"] for s in g.registry.specs()]
        assert names == ["find_app_tool", "use_app_tool"]
        assert "fake__create_note" in g.registry.names() and "fake__create_note" not in g.registry.planner_names()
        assert "Fake Notes: 5 tools" in g.registry.get("find_app_tool").spec()["function"]["description"]
        r = await g.run("find_app_tool", {"query": "create a new note"})
        assert r.ok and r.evidence["tools"][0] == "fake__create_note"
        r = await g.run("find_app_tool", {"query": "search notes"})
        assert r.evidence["tools"][0] == "fake__search_notes"
        assert "delete" not in r.output  # destructive tools are off: never offered
        await m.close()


async def test_read_runs_write_asks_and_results_taint(tmp_path):
    with FakeApp(auth="none") as app:
        m = await connected(tmp_path, app, auth="none")
        m.set_tool("fake", "search_notes", risk="read")
        g = Gate(tmp_path, m)
        r = await g.run("use_app_tool", {"tool": "fake__search_notes", "arguments": {"query": "milk"}})
        assert r.ok and not g.asked and g.ex.tainted_by("task1") == "fake__search_notes"
        r = await g.run("use_app_tool", {"tool": "fake__create_note", "arguments": {"title": "hi"}})
        assert not r.ok and "DENIED" in r.output and len(g.asked) == 1
        assert "Fake Notes: create_note" in g.asked[0].summary and app.logic.count("create_note") == 0
        await m.close()


async def test_autopilot_writes_until_outside_content_was_read(tmp_path):
    with FakeApp(auth="none") as app:
        m = await connected(tmp_path, app, auth="none")
        g = Gate(tmp_path, m, autonomy="autopilot")
        r = await g.run("use_app_tool", {"tool": "fake__create_note", "arguments": {"title": "first"}})
        assert r.ok and not g.asked
        await g.run("use_app_tool", {"tool": "fake__read_note", "arguments": {"id": "n1"}})
        r = await g.run("use_app_tool", {"tool": "fake__create_note", "arguments": {"title": "second"}})
        assert not r.ok and g.asked and "outside content" in g.asked[-1].reason
        assert app.logic.count("create_note") == 1
        await m.close()


async def test_injected_text_in_a_result_stays_data(tmp_path):
    with FakeApp(auth="none", inject=INJECTION) as app:
        m = await connected(tmp_path, app, auth="none")
        m.set_tool("fake", "delete_note", enabled=True)
        g = Gate(tmp_path, m, autonomy="autopilot")
        r = await g.run("use_app_tool", {"tool": "fake__read_note", "arguments": {"id": "n1"}})
        assert r.ok and "IMPORTANT SYSTEM INSTRUCTION" in r.output  # returned as text, nothing else happens
        r = await g.run("use_app_tool", {"tool": "fake__delete_note", "arguments": {"id": "n1"}})
        assert not r.ok and g.asked and app.logic.count("delete_note") == 0 and "n1" in app.logic.notes
        await m.close()


async def test_switched_off_tool_is_refused(tmp_path):
    with FakeApp(auth="none") as app:
        m = await connected(tmp_path, app, auth="none")
        g = Gate(tmp_path, m, autonomy="autopilot", approve=True)
        r = await g.run("use_app_tool", {"tool": "fake__delete_note", "arguments": {"id": "n1"}})
        assert not r.ok and "switched off" in r.output and app.logic.count("delete_note") == 0
        await m.close()


async def test_app_calls_from_a_phone_need_the_pin(tmp_path):
    store: dict[str, str] = {}
    guard = RemoteGuard(tmp_path / "g", SimpleNamespace(record=lambda *a, **k: None), alert=lambda *a: None,
                        get_secret=store.get, set_secret=store.__setitem__)
    guard.set_pin("4821", by="test")
    with FakeApp(auth="none") as app:
        m = await connected(tmp_path, app, auth="none")
        g = Gate(tmp_path, m, autonomy="autopilot", approve=True, guard=guard)
        r = await g.run("use_app_tool", {"tool": "fake__create_note", "arguments": {"title": "x"}}, "telegram", "5")
        assert not r.ok and r.evidence.get("needs_pin") and app.logic.count("create_note") == 0
        r = await g.run("use_app_tool", {"tool": "fake__whoami", "arguments": {}}, "telegram", "5")
        assert not r.ok and r.evidence.get("needs_pin")
        assert guard.try_pin("5", "4821")[0]
        r = await g.run("use_app_tool", {"tool": "fake__create_note", "arguments": {"title": "x"}}, "telegram", "5")
        assert r.ok and app.logic.count("create_note") == 1
        await m.close()


async def test_exfil_guard_asks_before_data_leaves_through_an_app(tmp_path):
    with FakeApp(auth="none") as app:
        m = await connected(tmp_path, app, auth="none")
        m.set_tool("fake", "search_notes", risk="read")
        m.set_tool("fake", "create_note", risk="write")  # even a tool the user lowered to "write"
        g = Gate(tmp_path, m, autonomy="trusted")
        g.ex.taint("task1", "fetch_page")
        r = await g.run("use_app_tool", {"tool": "fake__create_note", "arguments": {"title": "leak"}})
        assert not r.ok and "sends data to Fake Notes" in g.asked[-1].reason
        g2 = Gate(tmp_path, m, autonomy="trusted")  # a new task (a "no" covers the whole app for the rest of one)
        g2.ex.taint("task1", "fetch_page")
        r = await g2.run("use_app_tool", {"tool": "fake__search_notes", "arguments": {"query": "milk"}})
        assert r.ok and not g2.asked  # a plain read still runs
        r = await g2.run("use_app_tool", {"tool": "fake__search_notes", "arguments": {"query": "A" * 80}})
        assert not r.ok and "encoded value" in g2.asked[-1].reason
        await m.close()


async def test_old_settings_without_connectors_load_unchanged(tmp_path):
    s = Settings.model_validate({"addons": {"enabled": ["mcp"], "config": {"mcp": {"servers": {
        "x": {"command": "npx", "args": ["-y", "pkg"]}}}}}})
    assert s.connectors.enabled and s.addons.config["mcp"]["servers"]["x"]["command"] == "npx"


async def test_another_process_connecting_is_picked_up(tmp_path):
    with FakeApp(auth="none") as app:
        spec = fake_spec(app, "none")
        service = AppManager(tmp_path, Catalog({"fake": spec}), FAST, secrets=box(tmp_path))
        tools = AppTools(service, ToolRegistry([]))
        assert not tools.index()
        cli = AppManager(tmp_path, Catalog({"fake": spec}), FAST, secrets=box(tmp_path))
        assert (await cli.connect("fake"))["status"] == "connected"
        await cli.close()
        await asyncio.sleep(0.05)
        assert service.refresh_if_changed() and tools.index() == ["Fake Notes: 5 tools"]
        await service.close()


def test_cli_list_and_tools(tmp_path):
    import argparse

    from delta.connectors.cli import run_cli

    with FakeApp(auth="none") as app:
        lines: list[str] = []
        spec = fake_spec(app, "none")

        def args(**kw: Any) -> argparse.Namespace:
            base = {"action": "list", "app": None, "url": "", "command": "", "name": "", "oauth": False,
                    "client_id": "", "input": [], "enable": "", "disable": "", "yes": False}
            return argparse.Namespace(**(base | kw))

        def mgr() -> AppManager:
            return AppManager(tmp_path, Catalog({"fake": spec}), FAST, secrets=box(tmp_path))

        settings = Settings(data_dir=tmp_path)
        assert run_cli(settings, args(), out=lines.append, manager=mgr()) == 0
        assert any("fake" in line and "not connected" in line for line in lines)
        assert run_cli(settings, args(action="connect", app="fake"), out=lines.append, manager=mgr()) == 0
        assert run_cli(settings, args(action="tools", app="fake", enable="delete_note"), out=lines.append,
                       manager=mgr()) == 0
        assert any("[on ] delete_note" in line for line in lines)
        assert run_cli(settings, args(action="disconnect", app="Fake Notes"), out=lines.append, manager=mgr()) == 0


def test_api_routes(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from delta.api.connectors_routes import register

    with FakeApp(auth="token") as app:
        m = manager(tmp_path, fake_spec(app, "token"))
        layer = AppTools(m, ToolRegistry([]))
        api = FastAPI()
        register(api, SimpleNamespace(connectors=layer), lambda: None)
        with TestClient(api) as c:
            assert c.get("/api/connectors").json()["apps"][0]["state"] == "not connected"
            r = c.post("/api/connectors/fake/connect", json={"token": "fake-pat-123"}).json()
            assert r["status"] == "connected"
            tools = c.get("/api/connectors/fake/tools").json()["tools"]
            assert {t["name"]: t["enabled"] for t in tools}["delete_note"] is False
            assert "fake-pat-123" not in c.get("/api/connectors").text
            assert c.post("/api/connectors/fake/tools/delete_note", json={"enabled": True}).json()["ok"]
            assert c.post("/api/connectors/custom", json={"id": "mine", "url": "https://x.example/mcp"}).json()[
                "status"] == "confirm"
            assert c.post("/api/connectors/fake/disconnect").json()["status"] == "disconnected"
            assert c.get("/apps.js").status_code == 200


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_real_dpapi_seals_tokens(tmp_path):
    from delta.system import vault

    if not vault.available():
        pytest.skip("pywin32 is not installed")
    real = SecretBox(tmp_path / "s")
    real.save("notion", {"type": "token", "token": "very-secret-token"})
    raw = (tmp_path / "s" / "notion.bin").read_text()
    assert raw.startswith("dpapi:") and "very-secret-token" not in raw
    assert real.load("notion")["token"] == "very-secret-token"
    assert real.delete("notion") and not real.exists("notion")


class FakeChannel:
    def __init__(self, layer: Any) -> None:
        self.c = SimpleNamespace(connectors=layer)
        self.allowed = ["5"]
        self.sent: list[str] = []
        self.guarded: list[str] = []

    @staticmethod
    def _buttons(rows: Any) -> Any:
        return rows

    async def send_text(self, user: str, text: str) -> None:
        self.sent.append(text)

    async def _guarded(self, uid: str, action: str, say: Any, run: Any) -> None:
        self.guarded.append(action)
        await run()  # the PIN check itself is RemoteGuard's (tested in test_remote_guard)


def _update(replies: list[str]) -> Any:
    async def reply_text(text: str, **kw: Any) -> None:
        replies.append(text)

    return SimpleNamespace(effective_user=SimpleNamespace(id=5), message=SimpleNamespace(reply_text=reply_text))


async def test_telegram_connect_is_pin_guarded_and_finished_on_the_pc(tmp_path):
    from delta.channels.telegram_apps import apps_cmd, connect_cmd, disconnect_cmd

    with FakeApp() as app:
        cat = load_catalog(REPO / "config" / "mcp_catalog.yaml")
        cat.apps["fake"] = fake_spec(app)
        m = AppManager(tmp_path, cat, FAST, secrets=box(tmp_path), open_browser=auto_consent)
        ch = FakeChannel(AppTools(m, ToolRegistry([])))
        replies: list[str] = []
        await connect_cmd(ch, _update(replies), ["github"])  # a token app: never over Telegram
        assert "on the PC" in replies[-1] and "never take tokens" in replies[-1] and not ch.guarded
        await connect_cmd(ch, _update(replies), ["Fake", "Notes"])
        assert ch.guarded == ["apps"] and "browser on your PC" in ch.sent[0]
        for _ in range(100):
            if any("is connected" in s for s in ch.sent):
                break
            await asyncio.sleep(0.1)
        assert any("is connected" in s for s in ch.sent)
        await apps_cmd(ch, _update(replies))
        assert "Fake Notes" in replies[-1] and "connected" in replies[-1]
        await disconnect_cmd(ch, _update(replies), ["fake"])
        assert ch.guarded == ["apps", "apps"] and "disconnected" in ch.sent[-1]
        assert not any("fake-" in s or "Bearer" in s for s in [*ch.sent, *replies])
        await m.close()
