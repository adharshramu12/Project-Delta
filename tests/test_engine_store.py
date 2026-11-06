"""tasks.db: append-only events, size caps, retention, migrations and secret redaction."""

import sqlite3
import time

import pytest

from delta.engine.redact import redact_args, redact_text
from delta.engine.states import TaskState
from delta.engine.store import SCHEMA_VERSION, TaskStore


def test_events_are_append_only(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    store.create("t1", TaskState.ACTING)
    eid = store.append("t1", "step_start", step=1, tool="write_file")
    with pytest.raises(sqlite3.DatabaseError):
        store._db.execute("UPDATE events SET tool='x' WHERE id=?", (eid,))
    assert store.events("t1", ["step_start"])[0]["tool"] == "write_file"


def test_event_cap_per_task(tmp_path):
    store = TaskStore(tmp_path / "tasks.db", max_events_per_task=10)
    store.create("t1", TaskState.ACTING)
    for i in range(30):
        store.append("t1", "step_start", step=i, tool="read_file")
    events = store.events("t1")
    assert len(events) == 10
    assert events[-1]["kind"] == "note" and "full" in events[-1]["summary"]
    store.transition("t1", TaskState.DONE)  # the final state still lands even when the log is full
    assert store.get("t1")["state"] == "done"


def test_retention_by_age_keeps_running_tasks(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    store.create("old", TaskState.ACTING)
    store.transition("old", TaskState.DONE)
    store.create("running", TaskState.ACTING)
    store.create("new", TaskState.ACTING)
    store.transition("new", TaskState.DONE)
    later = time.time() + 40 * 86400
    store._db.execute("UPDATE tasks SET updated=? WHERE id='new'", (later,))
    store._db.commit()
    assert store.prune(retention_days=30, max_mb=0, now=later + 1) == 1
    assert store.get("old") is None and store.events("old") == []
    assert store.get("running") is not None and store.get("new") is not None


def test_retention_by_size_drops_oldest_finished(tmp_path):
    store = TaskStore(tmp_path / "tasks.db", max_events_per_task=5000)
    for n in range(40):
        tid = f"t{n:02d}"
        store.create(tid, TaskState.ACTING)
        for i in range(40):
            store.append(tid, "step_end", step=i, tool="read_file", summary="x" * 900)
        store.transition(tid, TaskState.DONE)
    store.create("live", TaskState.ACTING)
    before = store.size_bytes()
    assert before > 1024 * 1024
    removed = store.prune(retention_days=0, max_mb=0.5)
    assert removed > 0 and store.size_bytes() <= 0.5 * 1024 * 1024
    assert store.get("t00") is None and store.get("live") is not None


def test_migrates_an_existing_file_and_tolerates_a_newer_one(tmp_path):
    path = tmp_path / "tasks.db"
    TaskStore(path).close()
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 5}")  # written by a newer Delta
    db.commit()
    db.close()
    store = TaskStore(path)  # opens, changes nothing
    store.create("t1", TaskState.QUEUED)
    assert store.get("t1")["state"] == "queued"


def test_redaction_masks_secrets_and_caps_size():
    args = {"path": "C:/x/notes.txt", "password": "hunter2", "api_key": "abc", "max_tokens": 5,
            "command": "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456' https://x",
            "content": "my key sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX and ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123",
            "token_url": "https://x.test/cb?access_token=SECRETSECRETSECRET&x=1", "long": "y" * 5000}
    out = redact_args(args)
    for secret in ("hunter2", "ABCDEFGHIJKLMNOPQRSTUVWX", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123",
                   "abcdefghijklmnopqrstuvwxyz123456", "SECRETSECRETSECRET"):
        assert secret not in out, secret
    assert "notes.txt" in out and '"max_tokens": 5' in out
    assert len(out) <= 2100
    assert "123456789:" not in redact_text("bot 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ")
    assert redact_text("plain words stay") == "plain words stay"
