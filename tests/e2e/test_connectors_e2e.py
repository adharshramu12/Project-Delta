"""Connected apps end to end on a clean Windows PC (CI job e2e-windows), with the fake notes app on 127.0.0.1.

The real pieces: the catalog file loader, Windows DPAPI sealing of the sign-in, the OAuth flow (discovery, dynamic
registration, PKCE, the loopback listener), the dashboard's API routes, the ToolExecutor gate, a real local server
process (stdio) with its process tree ended on disconnect, and `delta apps` in a separate process. Only the user's
browser is replaced (it consents automatically). Run with DELTA_E2E=1 pytest -m e2e."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from delta.config import Boundaries, ConnectorsConfig, Settings
from delta.connectors.setup import attach
from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.evals.fakemcp import FakeApp, auto_consent
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine
from delta.tools.registry import ToolExecutor, ToolRegistry

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DELTA_E2E") != "1", reason="e2e: set DELTA_E2E=1"),
]


def _catalog(path: Path, url: str) -> Path:
    path.write_text("version: 1\napps:\n  fake:\n    name: Fake Notes\n    status: official\n"
                    f"    url: {url}\n    auth: {{type: oauth, client: dynamic}}\n"
                    "    docs: https://example.invalid/docs\n    checked: '2026-11-06'\n"
                    "    tools: {read: [search_notes, whoami]}\n", encoding="utf-8")
    return path


async def test_connect_use_disconnect_through_the_dashboard_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import webbrowser

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from delta.api.connectors_routes import register

    monkeypatch.setattr(webbrowser, "open", auto_consent)  # the user's browser, consenting
    if sys.platform != "win32":  # a developer run elsewhere: stand in for DPAPI (CI runs the real one)
        import base64

        from delta.system import vault

        monkeypatch.setattr(vault, "available", lambda: True)
        monkeypatch.setattr(vault, "seal", lambda t: "dpapi:" + base64.b64encode(t.encode()).decode())
        monkeypatch.setattr(vault, "unseal", lambda v: base64.b64decode(v[6:]).decode())
    with FakeApp() as app:
        settings = Settings(data_dir=tmp_path / "data", boundaries=Boundaries(autonomy="ask-risky"),
                            connectors=ConnectorsConfig(catalog=str(_catalog(tmp_path / "cat.yaml", app.url))))
        registry = ToolRegistry([])
        audit = AuditLog(tmp_path / "audit.jsonl")
        layer = attach(settings, registry, audit)
        m = layer.manager
        api = FastAPI()

        class C:
            connectors = layer

        register(api, C(), lambda: None)
        with TestClient(api) as client:
            r = client.post("/api/connectors/fake/connect", json={}).json()
            assert r["status"] == "sign_in" and r["url"].startswith(app.base + "/authorize")
            for _ in range(100):
                state = client.get("/api/connectors").json()["apps"][0]["state"]
                if state == "connected":
                    break
                await asyncio.sleep(0.1)
            assert state == "connected"
        m.sessions.clear()  # those sessions belong to the test client's own event loop; this test opens new ones

        sealed = next((tmp_path / "data").rglob("fake.bin")).read_text(encoding="ascii")
        if sys.platform == "win32":
            assert sealed.startswith("dpapi:")  # Windows DPAPI, never plain text
        assert not any(t in sealed for t in app.access)

        approvals = ApprovalBroker(timeout_s=5)
        ex = ToolExecutor(registry, PolicyEngine(settings.boundaries), approvals, audit)
        token = CURRENT_REQUEST.set(TaskRequest("search my notes", channel="api"))
        try:
            out = await ex.execute("use_app_tool", {"tool": "fake__search_notes", "arguments": {"query": "milk"}}, "t1")
        finally:
            CURRENT_REQUEST.reset(token)
        assert out.ok and "Shopping list" in out.output and ex.tainted_by("t1") == "fake__search_notes"
        assert any('"app.call"' in line for line in (tmp_path / "audit.jsonl").read_text().splitlines())

        result = await m.disconnect("fake", by="e2e")
        assert result["revoked"] and app.active_grants() == 0 and not m.secrets.exists("fake")
        await m.close()


async def test_local_server_process_tree_ends_on_disconnect(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data")
    layer = attach(settings, ToolRegistry([]), None)
    m = layer.manager
    command = f'"{sys.executable}" -m delta.evals.fakemcp stdio'
    first = await m.connect("localnotes", custom={"name": "Local notes", "command": command})
    assert first["status"] == "confirm" and sys.executable in " ".join(first["command"])
    assert (await m.connect("localnotes", custom={"name": "Local notes", "command": command},
                            confirm=True))["status"] == "connected"
    pid = m.sessions["localnotes"].transport.pid
    assert pid and psutil.pid_exists(pid)
    out = await m.call("localnotes", "read_note", {"id": "n2"}, "read")  # type: ignore[arg-type]
    assert out.ok and "lazy tool loading" in out.text
    await m.disconnect("localnotes")
    await asyncio.sleep(0.5)
    assert not psutil.pid_exists(pid)
    await m.close()


def test_delta_apps_cli_in_its_own_process(tmp_path: Path):
    env = {**os.environ, "DELTA_DATA_DIR": str(tmp_path / "data")}
    run = subprocess.run([sys.executable, "-m", "delta", "apps", "list"], capture_output=True, text=True, env=env,
                         timeout=120, check=False)
    assert run.returncode == 0, run.stderr
    assert "notion" in run.stdout and "not connected" in run.stdout
    missing = subprocess.run([sys.executable, "-m", "delta", "apps", "status", "notion"], capture_output=True,
                             text=True, env=env, timeout=120, check=False)
    assert missing.returncode == 1 and "not connected" in missing.stdout
