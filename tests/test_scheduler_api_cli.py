"""Schedules API (token required, preview before save, pause/resume/run/delete) and `delta schedule`."""

import argparse
import secrets
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient

from delta.api.schedule_routes import register
from delta.config import Settings
from delta.scheduler.cli import add_parser, run_cli
from delta.scheduler.rules import Rule
from delta.scheduler.service import Scheduler
from delta.scheduler.store import ScheduleStore

TOKEN = "t0ken"
TEXT = "every day at 9 am check my attendance on the ERP"


def make_app(tmp_path: Path) -> tuple[TestClient, SimpleNamespace]:
    store = ScheduleStore(tmp_path / "schedules.db")
    sched = Scheduler(store, alert=lambda t, x: None)
    started: list = []
    sched.run_now = lambda s, ch="", u="": (started.append((s.id, ch)) or True, "Started")  # type: ignore[method-assign]
    audit: list = []
    c = SimpleNamespace(scheduler=sched, settings=Settings(data_dir=tmp_path),
                        orchestrator=SimpleNamespace(executor=SimpleNamespace(policy=SimpleNamespace(
                            b=SimpleNamespace(autonomy="ask-risky")))),
                        audit=SimpleNamespace(record=lambda e, **k: audit.append((e, k))), started=started,
                        audit_log=audit)
    app = FastAPI()
    bearer = HTTPBearer(auto_error=False)

    def auth(cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:  # noqa: B008
        if not cred or not secrets.compare_digest(cred.credentials, TOKEN):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad token")

    register(app, c, auth)
    return TestClient(app), c


H = {"Authorization": f"Bearer {TOKEN}"}


def test_every_schedule_route_needs_the_token(tmp_path):
    client, _ = make_app(tmp_path)
    for method, path in [("get", "/api/schedules"), ("post", "/api/schedules/preview"), ("post", "/api/schedules"),
                         ("get", "/api/schedules/x"), ("patch", "/api/schedules/x"), ("delete", "/api/schedules/x"),
                         ("post", "/api/schedules/x/run"), ("post", "/api/schedules/x/pause")]:
        r = client.request(method, path, json={"text": TEXT})
        assert r.status_code == 401, (method, path)
        r = client.request(method, path, json={"text": TEXT}, headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401, (method, path)


def test_preview_then_save_then_manage(tmp_path):
    client, c = make_app(tmp_path)
    prev = client.post("/api/schedules/preview", json={"text": TEXT}, headers=H).json()
    assert prev["when"] == "every day at 9:00 AM" and prev["prompt"] == "check my attendance on the ERP"
    assert "Next run:" in prev["plain"]
    assert client.get("/api/schedules", headers=H).json() == []  # a preview saves nothing
    assert client.post("/api/schedules", json={"text": TEXT}, headers=H).status_code == 400  # needs confirm
    made = client.post("/api/schedules", json={"text": TEXT, "confirm": True}, headers=H).json()
    sid = made["id"]
    assert made["owner_channel"] == "api" and made["enabled"] and made["next_run"]
    listed = client.get("/api/schedules", headers=H).json()
    assert [s["id"] for s in listed] == [sid]
    assert client.post(f"/api/schedules/{sid}/pause", headers=H).json()["enabled"] is False
    assert client.post(f"/api/schedules/{sid}/resume", headers=H).json()["enabled"] is True
    changed = client.patch(f"/api/schedules/{sid}", json={"text": "every day at 8 am"}, headers=H).json()
    assert changed["when"] == "every day at 8:00 AM" and changed["prompt"] == "check my attendance on the ERP"
    assert client.post(f"/api/schedules/{sid}/run", headers=H).json()["ok"] is True
    assert c.started == [(sid, "dashboard")]
    assert client.delete(f"/api/schedules/{sid}", headers=H).json()["ok"] is True
    assert client.get(f"/api/schedules/{sid}", headers=H).status_code == 404
    assert {e for e, _ in c.audit_log} >= {"schedule.created", "schedule.changed", "schedule.deleted"}


def test_unclear_text_gets_a_question(tmp_path):
    client, _ = make_app(tmp_path)
    r = client.post("/api/schedules/preview", json={"text": "every 2 minutes ping"}, headers=H)
    assert r.status_code == 400 and "5 minutes" in r.json()["detail"]


def test_the_dashboard_script_is_served(tmp_path):
    client, _ = make_app(tmp_path)
    r = client.get("/schedules.js")
    assert r.status_code == 200 and "/api/schedules" in r.text
    html = (Path(__file__).parents[1] / "src/delta/api/static/index.html").read_text(encoding="utf-8")
    assert 'id="schedules"' in html and '<script src="/schedules.js">' in html


# --- CLI ------------------------------------------------------------------------------------------------

def cli(tmp_path: Path, argv: list[str], answers: str = "y", post=None) -> tuple[int, str]:
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["schedule", *argv])
    out: list[str] = []
    code = run_cli(Settings(data_dir=tmp_path), args, ask=lambda q: answers, out=out.append, post=post)
    return code, "\n".join(out)


def test_cli_add_list_pause_resume_remove(tmp_path):
    code, out = cli(tmp_path, ["add", "every", "Monday", "10", "am", "summarise", "my", "downloads", "folder"])
    assert code == 0 and "Every Monday at 10:00 AM: summarise my downloads folder" in out and "Next run:" in out
    code, out = cli(tmp_path, ["list"])
    assert "summarise my downloads folder" in out and "every Monday at 10:00 AM" in out
    sid = out.split()[0]
    assert cli(tmp_path, ["pause", sid])[0] == 0
    assert "paused" in cli(tmp_path, ["list"])[1]
    assert cli(tmp_path, ["resume", "downloads"])[0] == 0
    assert cli(tmp_path, ["remove", sid], answers="n")[0] == 1  # kept
    assert cli(tmp_path, ["remove", sid, "-y"])[0] == 0
    assert "No schedules yet" in cli(tmp_path, ["list"])[1]


def test_cli_add_asks_first_and_explains_problems(tmp_path):
    code, out = cli(tmp_path, ["add", "remind", "me", "at", "6", "pm", "to", "call", "Ravi"], answers="n")
    assert code == 1 and "remind you to call Ravi" in out and "Not saved" in out
    assert ScheduleStore(tmp_path / "schedules.db").list() == []
    code, out = cli(tmp_path, ["add", "every", "minute", "check"])
    assert code == 1 and "5 minutes" in out
    assert cli(tmp_path, ["pause", "nothing"])[0] == 1


def test_cli_run_asks_the_running_service(tmp_path):
    store = ScheduleStore(tmp_path / "schedules.db")
    s = store.add("erp", "check attendance", Rule("daily", time="09:00"))
    store.close()
    calls: list[str] = []
    code, out = cli(tmp_path, ["run", s.id], post=lambda path: calls.append(path) or {"ok": True, "message": "Started"})
    assert code == 0 and calls == [f"/api/schedules/{s.id}/run"] and "Started" in out

    def down(path):
        raise ConnectionError("refused")

    code, out = cli(tmp_path, ["run", s.id], post=down)
    assert code == 1 and "not running" in out


def test_old_settings_without_a_scheduler_section_still_load(tmp_path):
    s = Settings.model_validate({"agent": {"name": "Delta"}, "data_dir": str(tmp_path)})
    assert s.scheduler.enabled and s.scheduler.grace_minutes == 60 and s.scheduler.max_concurrent == 1
    with pytest.raises(ValueError):
        Settings.model_validate({"scheduler": {"max_concurrent": 0}})
