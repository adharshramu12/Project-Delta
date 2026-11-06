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


def test_a_crashing_task_is_reported_failed_not_left_running(client):
    """API work(): an unexpected exception still ends the task with a result for the dashboard."""
    import time

    from delta.core.state import TASK_CRASHED

    tc, c = client

    async def boom(req):
        raise RuntimeError("disk on fire")

    c.submit = boom
    task_id = tc.post("/api/tasks", json={"text": "x"}).json()["task_id"]
    for _ in range(100):
        r = tc.get(f"/api/tasks/{task_id}").json()
        if r.get("status") != "running":
            break
        time.sleep(0.02)
    assert r["status"] == "failed" and r["answer"] == TASK_CRASHED and "disk on fire" not in str(r)


def test_memory_provenance_forget_and_learning_gate_endpoints(client):
    tc, c = client
    c.memory.add("page said hello", "profile", "agent", origin="web_read", trust="untrusted", task_id="t7")
    items = tc.get("/api/memory?trust=untrusted").json()
    assert items and items[0]["kind"] == "note" and "data only" in items[0]["provenance"]
    assert tc.get("/api/memory/stats").json()["trust:untrusted"] == 1
    assert tc.post("/api/memory/forget", json={"task_id": "t7"}).json() == {"forgotten": 1}
    assert tc.post("/api/memory/forget", json={"trust": "nonsense"}).status_code == 400
    store = c.orchestrator.learning
    bad = store.add("lesson", "k1", "x", status="candidate", trust="untrusted")
    good = store.add("lesson", "k2", "y", status="candidate")
    assert {i["ref"] for i in tc.get("/api/learn").json()} == {f"L{bad.id}", f"L{good.id}"}
    assert tc.post(f"/api/learn/L{bad.id}", json={"action": "promote"}).status_code == 409
    assert tc.post(f"/api/learned/{bad.id}", json={"action": "keep"}).status_code == 409
    assert tc.post(f"/api/learn/L{good.id}", json={"action": "promote"}).json()["change"] == "promoted"
    assert tc.post("/api/learn/R999", json={"action": "reject"}).status_code == 404
    assert any(h["event"] == "promoted" for h in tc.get(f"/api/learn/history?item=L{good.id}").json())


def test_an_email_draft_can_be_edited_from_the_dashboard(client):
    from concurrent.futures import Future

    tc, c = client
    assert {"draft_email", "send_email"} <= set(c.registry.names())
    req = ApprovalRequest("send_email", "Send this email:\nTo: a@x.com", "external", "every email", can_edit=True)
    fut: Future = Future()  # stands in for the waiting task's future (done / set_result / result)
    c.approvals._pending[req.id] = (req, fut)  # a request waiting for an answer, as approvals.request leaves it
    try:
        listed = next(a for a in tc.get("/api/approvals").json() if a["id"] == req.id)
        assert listed["can_edit"] is True
        r = tc.post(f"/api/approvals/{req.id}", json={"approved": False, "edit": "say 5 pm"})
        assert r.status_code == 200 and fut.done() and fut.result().edit == "say 5 pm" and not fut.result().approved
    finally:
        c.approvals._pending.pop(req.id, None)
