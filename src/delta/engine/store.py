"""tasks.db: every task as a state machine row plus an append-only log of what happened in it.

One SQLite file in Delta's data folder, standard library only, WAL mode and a busy timeout so the
background service and the `delta tasks` command can both open it at once (like schedules.db).

tasks   one row per task: request, channel, owner, permission level at start, current state, outcome.
events  append-only (an UPDATE is refused by a trigger): state changes, each tool step's start and
        end (tool, redacted arguments, short result, model, tokens, duration, screenshot), checks.

Bounded: at most `max_events_per_task` events per task, tasks older than `retention_days` are removed
(never one that is still running or interrupted and recent), and when the file passes `max_mb` the
oldest finished tasks go first. Schema changes are numbered migrations (PRAGMA user_version), so an
existing data folder is upgraded in place and an older file never breaks start-up.
"""

from __future__ import annotations

import builtins
import json
import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from delta.engine.states import FINAL, RUNNING, IllegalTransitionError, TaskState, can_transition
from delta.system import sqlite_safe

# Each entry upgrades the file by one version. Never edit a shipped entry; add a new one.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS tasks(
      id TEXT PRIMARY KEY,
      text TEXT NOT NULL DEFAULT '',
      channel TEXT NOT NULL DEFAULT '',
      user TEXT NOT NULL DEFAULT '',
      chat_id TEXT NOT NULL DEFAULT '',
      autonomy TEXT NOT NULL DEFAULT '',
      state TEXT NOT NULL,
      answer TEXT NOT NULL DEFAULT '',
      provider TEXT NOT NULL DEFAULT '',
      tokens_in INTEGER NOT NULL DEFAULT 0,
      tokens_out INTEGER NOT NULL DEFAULT 0,
      calls INTEGER NOT NULL DEFAULT 0,
      steps INTEGER NOT NULL DEFAULT 0,
      events INTEGER NOT NULL DEFAULT 0,
      resumes INTEGER NOT NULL DEFAULT 0,
      notified INTEGER NOT NULL DEFAULT 0,
      note TEXT NOT NULL DEFAULT '',
      created REAL NOT NULL,
      updated REAL NOT NULL,
      finished REAL
    );
    CREATE INDEX IF NOT EXISTS tasks_by_state ON tasks(state, updated);
    CREATE INDEX IF NOT EXISTS tasks_by_created ON tasks(created);
    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      task_id TEXT NOT NULL,
      ts REAL NOT NULL,
      kind TEXT NOT NULL,
      step INTEGER,
      tool TEXT NOT NULL DEFAULT '',
      args TEXT NOT NULL DEFAULT '',
      risk TEXT NOT NULL DEFAULT '',
      side_effect INTEGER NOT NULL DEFAULT 0,
      fp TEXT NOT NULL DEFAULT '',
      ok INTEGER,
      summary TEXT NOT NULL DEFAULT '',
      model TEXT NOT NULL DEFAULT '',
      tokens_in INTEGER NOT NULL DEFAULT 0,
      tokens_out INTEGER NOT NULL DEFAULT 0,
      duration_ms INTEGER,
      screenshot TEXT NOT NULL DEFAULT '',
      data TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS events_by_task ON events(task_id, id);
    CREATE TRIGGER IF NOT EXISTS events_append_only BEFORE UPDATE ON events
    BEGIN SELECT RAISE(ABORT, 'the event log is append-only'); END;
    """,
]
SCHEMA_VERSION = len(MIGRATIONS)
EVENT_FIELDS = ("step", "tool", "args", "risk", "side_effect", "fp", "ok", "summary", "model", "tokens_in",
                "tokens_out", "duration_ms", "screenshot", "data")
TASK_FIELDS = ("text", "channel", "user", "chat_id", "autonomy", "answer", "provider", "tokens_in", "tokens_out",
               "calls", "steps", "resumes", "notified", "note", "finished")


class TaskStore:
    def __init__(self, path: Path, max_events_per_task: int = 2000) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.max_events_per_task = max(10, max_events_per_task)
        self._db = sqlite_safe.connect(path, check_same_thread=False, timeout=5)  # a damaged file is set aside
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        new = self._db.execute("PRAGMA page_count").fetchone()[0] == 0  # no tables yet (also a set-aside file)
        with self._lock:
            if new:  # set before any table exists: lets prune() give space back without a full VACUUM
                self._db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._migrate()

    def _migrate(self) -> None:
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            return  # written by a newer Delta: read what we understand, change nothing
        for i in range(version, SCHEMA_VERSION):
            self._db.executescript(MIGRATIONS[i])
            self._db.execute(f"PRAGMA user_version={i + 1}")
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._db.commit()
                self._db.close()
            except sqlite3.ProgrammingError:
                pass

    # --- tasks ----------------------------------------------------------------------------------
    def create(self, task_id: str, state: TaskState = TaskState.QUEUED, **fields: Any) -> dict[str, Any]:
        now = time.time()
        cols = {k: v for k, v in fields.items() if k in TASK_FIELDS}
        names = ["id", "state", "created", "updated", *cols]
        with self._lock:
            self._db.execute(f"INSERT INTO tasks({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                             [task_id, str(state), now, now, *cols.values()])
            self._append(task_id, "state", {"summary": f"-> {state}", "data": json.dumps({"to": str(state)})}, now)
            self._db.commit()
        return self.get(task_id) or {}

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def update(self, task_id: str, **fields: Any) -> None:
        cols = {k: v for k, v in fields.items() if k in TASK_FIELDS}
        if not cols:
            return
        sets = ", ".join(f"{k}=?" for k in cols)
        with self._lock:
            self._db.execute(f"UPDATE tasks SET {sets}, updated=? WHERE id=?", [*cols.values(), time.time(), task_id])
            self._db.commit()

    def transition(self, task_id: str, new: TaskState | str, reason: str = "", **fields: Any) -> dict[str, Any]:
        """Move a task to `new`, only along an allowed transition (IllegalTransitionError otherwise).
        Moving to the state it is already in is a no-op."""
        new = TaskState(new)
        now = time.time()
        with self._lock:
            row = self._db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = str(row["state"])
            if current == new:
                return self._get_locked(task_id)
            if not can_transition(current, new):
                raise IllegalTransitionError(task_id, current, str(new))
            cols = {k: v for k, v in fields.items() if k in TASK_FIELDS}
            if new in FINAL:
                cols.setdefault("finished", now)
            sets = "".join(f", {k}=?" for k in cols)
            self._db.execute(f"UPDATE tasks SET state=?, updated=?{sets} WHERE id=?",
                             [str(new), now, *cols.values(), task_id])
            self._append(task_id, "state", {"summary": f"{current} -> {new}" + (f": {reason}" if reason else ""),
                                            "data": json.dumps({"from": current, "to": str(new), "reason": reason})},
                         now)
            self._db.commit()
            return self._get_locked(task_id)

    def _get_locked(self, task_id: str) -> dict[str, Any]:
        row = self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else {}

    def list(self, states: Iterable[str] | None = None, channel: str = "", query: str = "", since: float = 0,
             limit: int = 50, offset: int = 0) -> builtins.list[dict[str, Any]]:
        where: builtins.list[str] = []
        params: builtins.list[Any] = []
        states = [s for s in (states or []) if s]
        if states:
            where.append(f"state IN ({', '.join('?' * len(states))})")
            params += states
        if channel:
            where.append("channel=?")
            params.append(channel)
        if query:
            where.append("(text LIKE ? OR answer LIKE ?)")
            params += [f"%{query}%", f"%{query}%"]
        if since:
            where.append("created>=?")
            params.append(since)
        sql = "SELECT * FROM tasks" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY created DESC LIMIT ? OFFSET ?"
        with self._lock:
            rows = self._db.execute(sql, [*params, max(1, min(limit, 500)), max(0, offset)]).fetchall()
        return [dict(r) for r in rows]

    def running(self) -> builtins.list[dict[str, Any]]:
        return self.list(states=[str(s) for s in RUNNING], limit=500)

    # --- events ---------------------------------------------------------------------------------
    def append(self, task_id: str, kind: str, **fields: Any) -> int | None:
        """Add one event. None when the task's event budget is used up (one note says so)."""
        with self._lock:
            event_id = self._append(task_id, kind, fields, time.time())
            self._db.commit()
        return event_id

    def _append(self, task_id: str, kind: str, fields: dict[str, Any], now: float) -> int | None:
        row = self._db.execute("SELECT events FROM tasks WHERE id=?", (task_id,)).fetchone()
        count = int(row["events"]) if row else 0
        if count >= self.max_events_per_task:
            return None
        if count == self.max_events_per_task - 1 and kind != "state":
            kind, fields = "note", {"summary": "event log for this task is full; later steps are not recorded"}
        cols = {k: fields[k] for k in EVENT_FIELDS if k in fields and fields[k] is not None}
        names = ["task_id", "ts", "kind", *cols]
        cur = self._db.execute(f"INSERT INTO events({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                               [task_id, now, kind, *cols.values()])
        if row:
            self._db.execute("UPDATE tasks SET events=events+1 WHERE id=?", (task_id,))
        return cur.lastrowid

    def events(self, task_id: str, kinds: Iterable[str] | None = None,
               limit: int = 5000) -> builtins.list[dict[str, Any]]:
        kinds = list(kinds or [])
        sql = "SELECT * FROM events WHERE task_id=?"
        params: builtins.list[Any] = [task_id]
        if kinds:
            sql += f" AND kind IN ({', '.join('?' * len(kinds))})"
            params += kinds
        sql += " ORDER BY id LIMIT ?"
        with self._lock:
            rows = self._db.execute(sql, [*params, limit]).fetchall()
        return [dict(r) for r in rows]

    def next_step(self, task_id: str) -> int:
        with self._lock:
            row = self._db.execute("SELECT MAX(step) FROM events WHERE task_id=?", (task_id,)).fetchone()
        return int(row[0] or 0) + 1

    # --- retention ------------------------------------------------------------------------------
    def size_bytes(self) -> int:
        with self._lock:
            pages = self._db.execute("PRAGMA page_count").fetchone()[0]
            free = self._db.execute("PRAGMA freelist_count").fetchone()[0]
            size = self._db.execute("PRAGMA page_size").fetchone()[0]
        return int((pages - free) * size)

    def _delete(self, ids: builtins.list[str]) -> None:
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            marks = ", ".join("?" * len(chunk))
            self._db.execute(f"DELETE FROM events WHERE task_id IN ({marks})", chunk)
            self._db.execute(f"DELETE FROM tasks WHERE id IN ({marks})", chunk)

    def prune(self, retention_days: float = 30, max_mb: float = 50, now: float | None = None) -> int:
        """Remove old tasks and their events. Running tasks are never removed. Returns tasks removed."""
        now = time.time() if now is None else now
        removed = 0
        keep = [str(s) for s in RUNNING]
        marks = ", ".join("?" * len(keep))
        with self._lock:
            if retention_days > 0:
                cutoff = now - retention_days * 86400
                ids = [r[0] for r in self._db.execute(
                    f"SELECT id FROM tasks WHERE updated<? AND state NOT IN ({marks})", [cutoff, *keep])]
                self._delete(ids)
                removed += len(ids)
                # events of tasks that no longer exist (e.g. a crash between two writes)
                self._db.execute("DELETE FROM events WHERE ts<? AND task_id NOT IN (SELECT id FROM tasks)", (cutoff,))
            self._db.commit()
        limit = max_mb * 1024 * 1024
        while max_mb > 0 and self.size_bytes() > limit:
            with self._lock:
                ids = [r[0] for r in self._db.execute(
                    f"SELECT id FROM tasks WHERE state NOT IN ({marks}) ORDER BY updated LIMIT 20", keep)]
                if not ids:
                    break
                self._delete(ids)
                self._db.commit()
                removed += len(ids)
        if removed:
            with self._lock:
                self._db.execute("PRAGMA incremental_vacuum")
                self._db.commit()
        return removed
