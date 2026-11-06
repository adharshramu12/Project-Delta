"""Scheduled tasks wired into the real container and API: tools registered, the scheduler bound, routes behind
the dashboard token, and a reminder delivered end to end through the real Container (no AI, no network)."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from delta.api.server import create_app, load_token
from delta.bootstrap import build
from delta.config import Settings
from delta.scheduler.rules import Rule, now_utc

pytest.importorskip("winreg", reason="the full container reads Windows folders (runs on Windows CI)")

TOOLS = {"schedule_task", "list_schedules", "pause_schedule", "resume_schedule", "delete_schedule", "run_schedule_now"}


@pytest.fixture
def container(tmp_path):
    c = build(Settings(data_dir=tmp_path))
    yield c
    c.scheduler.store.close()


def test_the_container_has_the_scheduler_and_its_tools(container):
    assert set(container.registry.names()) >= TOOLS
    assert container.scheduler.c is container
    assert not container.scheduler.started  # the loop only runs inside `delta run`


def test_schedule_routes_use_the_dashboard_token(container, tmp_path):
    token = load_token(tmp_path)
    with TestClient(create_app(container, token)) as tc:
        assert tc.get("/api/schedules").status_code == 401
        assert tc.get("/api/schedules", headers={"Authorization": "Bearer nope"}).status_code == 401
        h = {"Authorization": f"Bearer {token}"}
        made = tc.post("/api/schedules", json={"text": "remind me in 2 hours to stretch", "confirm": True},
                       headers=h).json()
        assert made["kind"] == "reminder" and made["owner_channel"] == "api"
        assert [s["id"] for s in tc.get("/api/schedules", headers=h).json()] == [made["id"]]
        assert tc.get("/schedules.js").status_code == 200


async def test_a_due_reminder_reaches_the_dashboard_chat(container, monkeypatch):
    shown: list = []
    container.scheduler.alert = lambda title, text: shown.append(text)
    t0 = now_utc()
    s = container.scheduler.store.add("water", "drink water", Rule("interval", minutes=5,
                                      anchor=(t0 - timedelta(minutes=5)).isoformat()), kind="reminder", now=t0)
    container.scheduler.clock = lambda: s.next_run + timedelta(seconds=1)
    for t in await container.scheduler.tick():
        await t
    assert shown and "drink water" in shown[0]
    chats = [ch for ch in container.chats.list("Scheduled tasks") if ch.title == "Scheduled tasks"]
    assert chats and any("drink water" in m["text"] for m in container.chats.messages(chats[0].id))
