"""Schedules and their run history in SQLite (schedules.db in Delta's data folder).

One file, standard library only. The background service and the `delta schedule` command can both open
it at the same time (WAL mode, a busy timeout), so a schedule added from a terminal is picked up by the
running service on its next check without a restart. Run history is bounded per schedule.
"""

from __future__ import annotations

import builtins
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from delta.scheduler.rules import Rule, describe, fmt_when, from_iso, iso, next_run, now_utc, zone_label

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  prompt TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'task',
  rule TEXT NOT NULL,
  tz TEXT NOT NULL DEFAULT '',
  owner_channel TEXT NOT NULL DEFAULT 'api',
  owner_user TEXT NOT NULL DEFAULT '',
  autonomy TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  next_run TEXT,
  last_run TEXT,
  last_status TEXT NOT NULL DEFAULT '',
  last_message TEXT NOT NULL DEFAULT '',
  held INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  schedule_id TEXT NOT NULL,
  due TEXT,
  started TEXT NOT NULL,
  finished TEXT,
  status TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL DEFAULT '',
  trigger TEXT NOT NULL DEFAULT 'schedule'
);
CREATE INDEX IF NOT EXISTS runs_by_schedule ON runs(schedule_id, id);
"""

KINDS = ("task", "reminder")


@dataclass
class Schedule:
    id: str
    name: str
    prompt: str
    kind: str
    rule: Rule
    tz: str = ""
    owner_channel: str = "api"
    owner_user: str = ""
    autonomy: str = ""  # the permission level when it was made: a run never gets more than this
    enabled: bool = True
    next_run: datetime | None = None
    last_run: datetime | None = None
    last_status: str = ""
    last_message: str = ""
    held: bool = False  # due during quiet hours: runs when they end (never counted as missed)
    created_at: str = ""
    updated_at: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def when(self) -> str:
        return describe(self.rule, self.tz)

    def plain(self) -> str:
        """'Every day at 9:00 AM: check my attendance on the ERP. Next run: Sat 7 Nov 2026, 9:00 AM.'"""
        what = "remind you to " + self.prompt if self.kind == "reminder" else self.prompt
        nxt = fmt_when(self.next_run, self.tz) if self.enabled else "paused"
        return f"{self.when[0].upper()}{self.when[1:]}: {what}. Next run: {nxt}."

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "prompt": self.prompt, "kind": self.kind,
                "rule": self.rule.to_dict(), "when": self.when, "tz": self.tz, "tz_label": zone_label(self.tz),
                "owner_channel": self.owner_channel, "owner_user": self.owner_user, "enabled": self.enabled,
                "next_run": iso(self.next_run), "next_run_text": fmt_when(self.next_run, self.tz)
                if self.enabled else "paused", "last_run": iso(self.last_run),
                "last_run_text": fmt_when(self.last_run, self.tz) if self.last_run else "",
                "last_status": self.last_status, "last_message": self.last_message, "held": self.held,
                "created_at": self.created_at, "history": self.history}


def new_id() -> str:
    return uuid.uuid4().hex[:8]


class ScheduleStore:
    def __init__(self, path: Path, history_limit: int = 50) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.history_limit = max(1, history_limit)
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- schedules ------------------------------------------------------------------------------------
    @staticmethod
    def _row(r: sqlite3.Row) -> Schedule:
        return Schedule(
            id=r["id"], name=r["name"], prompt=r["prompt"], kind=r["kind"],
            rule=Rule.from_dict(json.loads(r["rule"])), tz=r["tz"], owner_channel=r["owner_channel"],
            owner_user=r["owner_user"], autonomy=r["autonomy"], enabled=bool(r["enabled"]),
            next_run=from_iso(r["next_run"]), last_run=from_iso(r["last_run"]), last_status=r["last_status"],
            last_message=r["last_message"], held=bool(r["held"]), created_at=r["created_at"],
            updated_at=r["updated_at"])

    def add(self, name: str, prompt: str, rule: Rule, *, kind: str = "task", tz: str = "",
            owner_channel: str = "api", owner_user: str = "", autonomy: str = "",
            now: datetime | None = None) -> Schedule:
        rule.validate()
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind}")
        now = now or now_utc()
        s = Schedule(new_id(), name.strip()[:80] or prompt[:60], prompt.strip(), kind, rule, tz,
                     owner_channel, owner_user, autonomy, True, next_run(rule, tz, now),
                     created_at=iso(now) or "", updated_at=iso(now) or "")
        with self._lock:
            self._db.execute(
                "INSERT INTO schedules(id,name,prompt,kind,rule,tz,owner_channel,owner_user,autonomy,enabled,"
                "next_run,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)",
                (s.id, s.name, s.prompt, s.kind, json.dumps(rule.to_dict()), tz, owner_channel, owner_user,
                 autonomy, iso(s.next_run), s.created_at, s.updated_at))
            self._db.commit()
        return s

    def get(self, sid: str, with_history: bool = False) -> Schedule | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
        if r is None:
            return None
        s = self._row(r)
        if with_history:
            s.history = self.history(sid)
        return s

    def find(self, ref: str) -> Schedule | None:
        """By id, else by a unique name match ('attendance' finds 'check my attendance on the ERP')."""
        ref = (ref or "").strip()
        if not ref:
            return None
        if s := self.get(ref):
            return s
        low = ref.lower()
        hits = [s for s in self.list() if low in s.name.lower() or low in s.prompt.lower()]
        return hits[0] if len(hits) == 1 else None

    def list(self) -> builtins.list[Schedule]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM schedules ORDER BY enabled DESC, next_run IS NULL, next_run, "
                                    "created_at").fetchall()
        return [self._row(r) for r in rows]

    def due(self, now: datetime) -> builtins.list[Schedule]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM schedules WHERE enabled=1 AND next_run IS NOT NULL "
                                    "AND next_run<=? ORDER BY next_run", (iso(now),)).fetchall()
        return [self._row(r) for r in rows]

    def earliest(self) -> datetime | None:
        with self._lock:
            r = self._db.execute("SELECT MIN(next_run) FROM schedules WHERE enabled=1 AND next_run IS NOT NULL "
                                 "AND held=0").fetchone()
        return from_iso(r[0]) if r and r[0] else None

    def _update(self, sid: str, **fields: Any) -> bool:
        fields["updated_at"] = iso(now_utc())
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            cur = self._db.execute(f"UPDATE schedules SET {cols} WHERE id=?", (*fields.values(), sid))
            self._db.commit()
        return cur.rowcount > 0

    def set_next(self, sid: str, when: datetime | None, held: bool = False) -> None:
        self._update(sid, next_run=iso(when), held=int(held))

    def set_held(self, sid: str, held: bool) -> None:
        self._update(sid, held=int(held))

    def set_enabled(self, sid: str, enabled: bool, now: datetime | None = None) -> Schedule | None:
        s = self.get(sid)
        if s is None:
            return None
        if enabled:
            nxt = next_run(s.rule, s.tz, now or now_utc())
            if nxt is None:
                raise ValueError("This one-time schedule has already run. Make a new one instead.")
            self._update(sid, enabled=1, next_run=iso(nxt), held=0)
        else:
            self._update(sid, enabled=0, held=0)
        return self.get(sid)

    def edit(self, sid: str, *, name: str | None = None, prompt: str | None = None, rule: Rule | None = None,
             tz: str | None = None, kind: str | None = None, now: datetime | None = None) -> Schedule | None:
        s = self.get(sid)
        if s is None:
            return None
        fields: dict[str, Any] = {}
        if name is not None:
            fields["name"] = name.strip()[:80]
        if prompt is not None:
            fields["prompt"] = prompt.strip()
        if kind is not None:
            if kind not in KINDS:
                raise ValueError(f"unknown kind {kind}")
            fields["kind"] = kind
        if rule is not None or tz is not None:
            rule = rule or s.rule
            rule.validate()
            tz = s.tz if tz is None else tz
            fields.update(rule=json.dumps(rule.to_dict()), tz=tz, held=0,
                          next_run=iso(next_run(rule, tz, now or now_utc())) if s.enabled else iso(s.next_run))
        if fields:
            self._update(sid, **fields)
        return self.get(sid)

    def delete(self, sid: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM schedules WHERE id=?", (sid,))
            self._db.execute("DELETE FROM runs WHERE schedule_id=?", (sid,))
            self._db.commit()
        return cur.rowcount > 0

    def finish(self, sid: str, ran_at: datetime, status: str, message: str, next_at: datetime | None,
               disable: bool = False) -> None:
        """Record the outcome on the schedule itself (history goes through record_run)."""
        fields: dict[str, Any] = {"last_run": iso(ran_at), "last_status": status, "last_message": message[:500],
                                  "next_run": iso(next_at), "held": 0}
        if disable:
            fields["enabled"] = 0
        self._update(sid, **fields)

    # --- history -------------------------------------------------------------------------------------
    def record_run(self, sid: str, status: str, summary: str = "", *, due: datetime | None = None,
                   started: datetime | None = None, finished: datetime | None = None, task_id: str = "",
                   trigger: str = "schedule") -> int:
        started = started or now_utc()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO runs(schedule_id,due,started,finished,status,summary,task_id,trigger) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (sid, iso(due), iso(started), iso(finished or started), status, summary[:1000], task_id, trigger))
            # bounded: keep only the newest runs of this schedule
            self._db.execute("DELETE FROM runs WHERE schedule_id=? AND id NOT IN (SELECT id FROM runs "
                             "WHERE schedule_id=? ORDER BY id DESC LIMIT ?)", (sid, sid, self.history_limit))
            self._db.commit()
        return int(cur.lastrowid or 0)

    def history(self, sid: str, limit: int | None = None) -> builtins.list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM runs WHERE schedule_id=? ORDER BY id DESC LIMIT ?",
                                    (sid, limit or self.history_limit)).fetchall()
        return [dict(r) for r in rows]
