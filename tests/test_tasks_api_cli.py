"""Task history, run timeline and resume / discard through the dashboard API and `delta tasks`."""

import argparse
import time

import pytest
from engine_helpers import no_registry
from fastapi.testclient import TestClient

from delta.api.server import create_app, load_token
from delta.bootstrap import build
from delta.config import EngineConfig, Settings
from delta.engine.cli import run_cli
from delta.engine.states import TaskState
from delta.system.hardware_guard import Snapshot


class QuietSensor:
    def read(self):
        return Snapshot(cpu_pct=5, ram_pct=40)


@pytest.fixture
def client(tmp_path, monkeypatch):
    no_registry(monkeypatch, tmp_path)
    c = build(Settings(data_dir=tmp_path))
    c.governor.sensor = QuietSensor()
    token = load_token(tmp_path)
    with TestClient(create_app(c, token)) as tc:
        tc.headers["Authorization"] = f"Bearer {token}"
        yield tc, c


def interrupted(c, task_id="int1", finished=True):
    store = c.engine.store
    store.create(task_id, TaskState.QUEUED, text="tidy my downloads", channel="api", autonomy="ask-risky")
    store.transition(task_id, TaskState.PLANNING)
    store.transition(task_id, TaskState.ACTING)
    store.append(task_id, "step_start", step=1, tool="move_path", args='{"source": "a"}', risk="write",
                 side_effect=1, fp="abc", model="fake", tokens_in=10, tokens_out=2)
    if finished:
        store.append(task_id, "step_end", step=1, tool="move_path", ok=1, summary="moved", duration_ms=12)
    c.engine.recover()


def test_list_filter_and_timeline(client):
    tc, c = client
    interrupted(c)
    c.engine.store.create("done1", TaskState.QUEUED, text="say hi", channel="cli")
    c.engine.store.transition("done1", TaskState.DONE)
    all_tasks = tc.get("/api/tasks").json()
    assert all_tasks["enabled"] and {t["id"] for t in all_tasks["tasks"]} == {"int1", "done1"}
    only = tc.get("/api/tasks?state=interrupted").json()["tasks"]
    assert [t["id"] for t in only] == ["int1"]
    assert [t["id"] for t in tc.get("/api/tasks?channel=cli").json()["tasks"]] == ["done1"]
    assert [t["id"] for t in tc.get("/api/tasks?q=downloads").json()["tasks"]] == ["int1"]
    assert tc.get("/api/tasks?state=bogus").status_code == 400
    tl = tc.get("/api/tasks/int1/timeline").json()
    assert tl["task"]["state"] == "interrupted"
    assert tl["steps"][0]["tool"] == "move_path" and tl["steps"][0]["model"] == "fake"
    assert tl["steps"][0]["tokens_in"] == 10 and tl["steps"][0]["duration_ms"] == 12
    assert tc.get("/api/tasks/nope/timeline").status_code == 404
    assert tc.get("/api/tasks/int1/timeline", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert tc.get("/tasks.js").status_code == 200 and "openTimeline" in tc.get("/tasks.js").text
    assert 'id="tasks"' in tc.get("/").text and "/tasks.js" in tc.get("/").text


def test_resume_asks_about_an_unfinished_step_then_runs(client):
    tc, c = client
    interrupted(c, "int2", finished=False)
    first = tc.post("/api/tasks/int2/resume", json={}).json()
    assert not first["ok"] and first["needs_confirmation"] and len(first["options"]) == 2
    assert c.engine.store.get("int2")["state"] == "interrupted"
    ran = []

    async def fake_submit(req, autonomy_cap=None):
        from delta.core.state import TaskResult

        ran.append((req.id, autonomy_cap, req.history))
        return TaskResult(req.id, "done", "ok")

    c.submit = fake_submit
    second = tc.post("/api/tasks/int2/resume", json={"confirm_last": "done"}).json()
    assert second["ok"] and second["task_id"] == "int2"
    for _ in range(100):
        if ran:
            break
        time.sleep(0.02)
    assert ran and ran[0][0] == "int2" and ran[0][1] == "ask-risky" and "interrupted" in ran[0][2].lower()
    assert tc.post("/api/tasks/int2/resume", json={"confirm_last": "maybe"}).status_code == 422


def test_discard(client):
    tc, c = client
    interrupted(c, "int3")
    assert tc.post("/api/tasks/int3/discard").json()["ok"]
    assert c.engine.store.get("int3")["state"] == "cancelled"
    assert not tc.post("/api/tasks/int3/discard").json()["ok"]
    assert not tc.post("/api/tasks/int3/resume", json={}).json()["ok"]


def test_engine_off_keeps_the_dashboard_working(tmp_path, monkeypatch):
    no_registry(monkeypatch, tmp_path)
    c = build(Settings(data_dir=tmp_path, engine=EngineConfig(enabled=False)))
    c.governor.sensor = QuietSensor()
    assert c.engine is None and c.orchestrator.executor.recorder is None
    token = load_token(tmp_path)
    with TestClient(create_app(c, token)) as tc:
        tc.headers["Authorization"] = f"Bearer {token}"
        assert tc.get("/api/tasks").json() == {"enabled": False, "tasks": [],
                                               "states": [s.value for s in TaskState]}
        assert tc.get("/api/status").status_code == 200
    assert not (tmp_path / "tasks.db").exists()


def _args(action, task_id=None, **kw):
    return argparse.Namespace(action=action, id=task_id, state=kw.get("state", ""), limit=20,
                              json=kw.get("json", False), confirm=kw.get("confirm"))


def test_cli_list_show_discard(tmp_path, monkeypatch, capsys):
    no_registry(monkeypatch, tmp_path)
    settings = Settings(data_dir=tmp_path)
    settings.api.port = 1  # nothing listens there: the CLI works on the file itself
    c = build(settings)
    interrupted(c, "cli1")
    c.engine.store.close()
    assert run_cli(settings, _args("list")) == 0
    out = capsys.readouterr().out
    assert "cli1" in out and "interrupted" in out and "delta tasks resume" in out
    assert run_cli(settings, _args("show", "cli1")) == 0
    out = capsys.readouterr().out
    assert "move_path" in out and "[changes something]" in out
    assert run_cli(settings, _args("show")) == 2
    assert run_cli(settings, _args("discard", "cli1")) == 0
    assert run_cli(settings, _args("list", state="cancelled")) == 0
    assert "cli1" in capsys.readouterr().out
    assert run_cli(settings, _args("prune")) == 0
