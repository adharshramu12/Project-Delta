"""Apps is the one place to connect apps: the old Settings > Add-ons "mcp" tick moved to Apps > Advanced, MCP
servers start by themselves, and an old settings file keeps exactly what it had."""

from __future__ import annotations

import pytest

from delta.addons import mcp_settings as ms
from delta.config import AddonsConfig, Settings, read_user_settings, write_user_settings

SERVERS = {"files": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem@1.0.0", "C:/x"]},
           "remote": {"url": "https://tools.example/mcp", "token_secret": "remote_token"}}


def addons(ticked=(), **mcp):
    return AddonsConfig(enabled=list(ticked), config={"mcp": mcp} if mcp else {})


@pytest.mark.parametrize(("cfg", "on"), [
    (addons(), False),                                    # nothing configured: nothing runs
    (addons(servers={}), False),
    (addons(servers=SERVERS), True),                      # servers configured: on by themselves
    (addons(["mcp"], servers=SERVERS), True),
    (addons(servers=SERVERS, enabled=False), False),      # switched off on the Apps page
    (addons(["mcp"], servers={}), False),                 # the old tick alone never ran anything
])
def test_mcp_runs_whenever_servers_are_configured(cfg, on):
    assert ms.legacy_on(cfg) is on
    assert ("mcp" in ms.effective_addons(cfg)) is on


def test_other_add_ons_are_kept_in_order():
    cfg = addons(["weather", "mcp", "spotify"], servers=SERVERS)
    assert ms.effective_addons(cfg) == ["weather", "spotify", "mcp"]


@pytest.mark.parametrize(("enabled", "mcp", "after_on", "written"), [
    (["mcp"], {"servers": SERVERS}, True, {"enabled": True}),     # ticked: stays on
    ([], {"servers": SERVERS}, False, {"enabled": False}),        # servers but unticked: stays off
    (["mcp"], {}, False, {"enabled": True}),                      # ticked, no servers: on when some are added
    ([], {}, False, None),                                        # nothing to move
])
def test_migration_keeps_every_setting(tmp_path, enabled, mcp, after_on, written):
    write_user_settings(tmp_path, {"addons": {"enabled": [*enabled, "weather"], "config": {"mcp": mcp}},
                                   "memory": {"enabled": True}})
    s = Settings(addons=AddonsConfig(enabled=[*enabled, "weather"], config={"mcp": dict(mcp)}), data_dir=tmp_path)
    change = ms.apply_migration(s)
    saved = read_user_settings(tmp_path)
    if written is None:
        assert change is None and saved["addons"]["enabled"] == ["weather"]
        return
    assert saved["addons"]["config"]["mcp"]["enabled"] is written["enabled"]
    assert saved["addons"]["config"]["mcp"].get("servers", {}) == mcp.get("servers", {})  # servers untouched
    assert "mcp" not in saved["addons"]["enabled"] and "weather" in saved["addons"]["enabled"]
    assert saved["memory"] == {"enabled": True}  # unrelated settings untouched
    assert ms.legacy_on(s.addons) is after_on and "mcp" not in s.addons.enabled
    assert ms.apply_migration(s) is None  # once only


def test_describe_shows_what_runs_never_a_secret():
    rows = {r["name"]: r for r in ms.describe(addons(servers=SERVERS))}
    assert rows["files"]["runs"].startswith("npx -y @modelcontextprotocol/server-filesystem")
    assert not rows["files"]["remote"]
    assert rows["remote"]["runs"] == "https://tools.example/mcp" and "remote_token" not in str(rows)


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from delta.api.server import create_app, load_token
    from delta.bootstrap import build

    c = build(Settings(addons=addons(servers=SERVERS), data_dir=tmp_path))
    token = load_token(tmp_path)
    with TestClient(create_app(c, token)) as tc:
        tc.headers["Authorization"] = f"Bearer {token}"
        yield tc, c


def test_apps_page_lists_and_switches_the_servers(client):
    tc, c = client
    r = tc.get("/api/mcp-servers").json()
    assert {s["name"] for s in r["servers"]} == {"files", "remote"} and r["enabled"] is True
    assert tc.post("/api/mcp-servers", json={"enabled": False}).json()["restart_needed"] is True
    assert tc.get("/api/mcp-servers").json()["enabled"] is False
    assert read_user_settings(c.settings.data_dir)["addons"]["config"]["mcp"]["enabled"] is False
    assert "mcp" not in tc.get("/api/settings").json()["addons"]["available"]


def test_saving_settings_without_mcp_keeps_the_servers_as_they_were(client):
    tc, c = client
    tc.post("/api/mcp-servers", json={"enabled": True})
    assert tc.put("/api/settings", json={"addons_enabled": []}).json()["ok"]
    assert ms.legacy_on(c.settings.addons) is True  # the Settings page cannot switch MCP off by leaving it out
    assert tc.put("/api/settings", json={"addons_enabled": ["mcp"]}).status_code == 200  # an old page still works


def test_the_ui_has_one_place_for_apps():
    from pathlib import Path

    import delta

    static = Path(delta.__file__).parent / "api" / "static"
    apps = (static / "apps.js").read_text(encoding="utf-8")
    settings = (static / "settings.js").read_text(encoding="utf-8")
    assert "Advanced: custom MCP servers" in apps and "/api/mcp-servers" in apps
    assert "Model Context Protocol" not in settings  # no MCP switch on the Settings page
