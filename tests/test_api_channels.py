import subprocess
import sys

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from delta.api.server import create_app, load_token
from delta.bootstrap import build
from delta.channels.base import approval_text, chunks, is_allowed, result_text
from delta.config import Settings
from delta.core.state import TaskResult
from delta.safety.approvals import ApprovalRequest
from delta.system.hardware_guard import Snapshot


class QuietSensor:
    def read(self):
        return Snapshot(cpu_pct=5, ram_pct=40)


@pytest.fixture
def client(tmp_path):
    c = build(Settings(data_dir=tmp_path))
    c.governor.sensor = QuietSensor()
    token = load_token(tmp_path)
    with TestClient(create_app(c, token)) as tc:
        tc.headers["Authorization"] = f"Bearer {token}"
        yield tc, c


def test_requires_token(client):
    tc, _ = client
    assert tc.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert tc.get("/").status_code == 200  # dashboard page itself is public, data is not


def test_status_memory_boundaries(client):
    tc, _ = client
    s = tc.get("/api/status").json()
    assert s["governor"]["health"] == "NORMAL" and "write_file" in s["tools"]
    mid = tc.post("/api/memory", json={"content": "likes dark mode"}).json()["id"]
    assert any(m["content"] == "likes dark mode" for m in tc.get("/api/memory").json())
    assert tc.delete(f"/api/memory/{mid}").json()["ok"]
    assert tc.get("/api/boundaries").json()["autonomy"] == "ask-risky"
    assert tc.get("/api/approvals").json() == []
    assert tc.post("/api/approvals/nope", json={"approved": True}).status_code == 404


def test_shutdown_requests_stop(client):
    tc, c = client
    assert tc.post("/api/shutdown").json()["ok"]
    assert c.lifecycle.stopping.is_set()


def test_websocket_rejects_bad_token(client):
    tc, _ = client
    with pytest.raises(WebSocketDisconnect), tc.websocket_connect("/api/ws?token=bad") as ws:
        ws.receive_json()


def test_channel_helpers():
    assert not is_allowed(123, [])  # empty allow-list = nobody
    assert is_allowed(123, ["123"])
    req = ApprovalRequest(tool="delete_path", summary="move to Recycle Bin: x", risk="destructive", reason="r")
    assert "Recycle Bin" in approval_text(req)
    assert "not verified" in result_text(TaskResult("t", "failed", "hmm"))
    assert len(chunks("a" * 9000)) == 3


def test_core_import_stays_light():
    """Starting Delta must not drag in channel, web or scraping libraries until they are used."""
    heavy = ["telegram", "slack_bolt", "fastapi", "uvicorn", "trafilatura", "ddgs", "playwright"]
    code = f"import sys, delta.bootstrap; print([m for m in {heavy!r} if m in sys.modules])"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
