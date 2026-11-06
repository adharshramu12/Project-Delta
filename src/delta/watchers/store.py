"""Watchers and what they last saw, in watchers.db in Delta's data folder.

A watcher's settings (which login, what to read, how often, how to tell the user) are plain. What a watcher read
from the site (the snapshots, the items waiting for a digest, the items offered as reminders) is personal, so each
of those is sealed with Windows DPAPI (delta.system.vault) before it is written. On Windows without a working DPAPI
nothing read from a site is written at all (VaultUnavailableError), never plain text. Snapshots are bounded: the
last KEEP_SNAPSHOTS per section, MAX_ITEMS items each, MAX_FIELD characters per field.
"""

from __future__ import annotations

import builtins
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from delta.system import sqlite_safe, vault
from delta.watchers.recipe import Recipe

KEEP_SNAPSHOTS = 3
MAX_PENDING = 300
MAX_OFFERS = 100
MIN_INTERVAL_MINUTES = 60  # never more often than hourly
DEFAULT_INTERVAL_MINUTES = 360  # every 6 hours
NOTIFY = ("instant", "digest")

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchers(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  login TEXT NOT NULL,
  what TEXT NOT NULL DEFAULT '',
  recipe TEXT NOT NULL,
  interval_minutes INTEGER NOT NULL DEFAULT 360,
  notify TEXT NOT NULL DEFAULT 'instant',
  digest_time TEXT NOT NULL DEFAULT '18:00',
  schedule_id TEXT NOT NULL DEFAULT '',
  digest_schedule_id TEXT NOT NULL DEFAULT '',
  skill_id INTEGER,
  owner_channel TEXT NOT NULL DEFAULT 'api',
  owner_user TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  created TEXT NOT NULL,
  last_run TEXT,
  last_status TEXT NOT NULL DEFAULT '',
  last_message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  watcher_id TEXT NOT NULL,
  section TEXT NOT NULL,
  taken TEXT NOT NULL,
  sealed TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_by ON snapshots(watcher_id, section, id);
CREATE TABLE IF NOT EXISTS pending(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  watcher_id TEXT NOT NULL,
  kind TEXT NOT NULL,             -- digest | offer
  created TEXT NOT NULL,
  sealed TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS pending_by ON pending(watcher_id, kind, state);
"""


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass
class Watcher:
    id: str
    name: str
    login: str
    recipe: Recipe
    what: str = ""
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES
    notify: str = "instant"
    digest_time: str = "18:00"
    schedule_id: str = ""
    digest_schedule_id: str = ""
    skill_id: int | None = None
    owner_channel: str = "api"
    owner_user: str = ""
    enabled: bool = True
    created: str = ""
    last_run: str = ""
    last_status: str = ""
    last_message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["recipe"] = json.loads(self.recipe.to_json())
        d.pop("extra", None)
        return d


class WatchStore:
    def __init__(self, path: Path, seal: Callable[[str], str] | None = None,
                 unseal: Callable[[str], str] | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite_safe.connect(path, check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._seal = seal or vault.seal
        self._unseal = unseal or vault.unseal
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA secure_delete = ON")
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- watchers --------------------------------------------------------------------------------------
    @staticmethod
    def _row(r: sqlite3.Row) -> Watcher:
        return Watcher(id=r["id"], name=r["name"], login=r["login"], recipe=Recipe.parse(r["recipe"]), what=r["what"],
                       interval_minutes=int(r["interval_minutes"]), notify=r["notify"], digest_time=r["digest_time"],
                       schedule_id=r["schedule_id"], digest_schedule_id=r["digest_schedule_id"],
                       skill_id=r["skill_id"], owner_channel=r["owner_channel"], owner_user=r["owner_user"],
                       enabled=bool(r["enabled"]), created=r["created"], last_run=r["last_run"] or "",
                       last_status=r["last_status"], last_message=r["last_message"])

    def add(self, name: str, login: str, recipe: Recipe, *, what: str = "", interval_minutes: int = 360,
            notify: str = "instant", digest_time: str = "18:00", owner_channel: str = "api",
            owner_user: str = "") -> Watcher:
        if notify not in NOTIFY:
            raise ValueError("notify must be instant or digest")
        w = Watcher(uuid.uuid4().hex[:8], name.strip()[:80] or login, login, recipe, what[:300],
                    max(MIN_INTERVAL_MINUTES, int(interval_minutes)), notify, digest_time,
                    owner_channel=owner_channel, owner_user=owner_user, created=_now())
        with self._lock:
            self._db.execute(
                "INSERT INTO watchers(id,name,login,what,recipe,interval_minutes,notify,digest_time,owner_channel,"
                "owner_user,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (w.id, w.name, w.login, w.what, recipe.to_json(), w.interval_minutes, notify, digest_time,
                 owner_channel, owner_user, w.created))
            self._db.commit()
        return w

    def get(self, wid: str) -> Watcher | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM watchers WHERE id=?", (wid,)).fetchone()
        return self._row(r) if r else None

    def find(self, ref: str) -> Watcher | None:
        ref = (ref or "").strip()
        if not ref:
            return None
        if w := self.get(ref):
            return w
        hits = [w for w in self.list() if ref.lower() in w.name.lower()]
        return hits[0] if len(hits) == 1 else None

    def list(self) -> builtins.list[Watcher]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM watchers ORDER BY created").fetchall()
        return [self._row(r) for r in rows]

    def update(self, wid: str, **fields: Any) -> None:
        if "recipe" in fields and isinstance(fields["recipe"], Recipe):
            fields["recipe"] = fields["recipe"].to_json()
        if "interval_minutes" in fields:
            fields["interval_minutes"] = max(MIN_INTERVAL_MINUTES, int(fields["interval_minutes"]))
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE watchers SET {cols} WHERE id=?", (*fields.values(), wid))  # nosec B608: fixed names
            self._db.commit()

    def delete(self, wid: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM watchers WHERE id=?", (wid,))
            self._db.execute("DELETE FROM snapshots WHERE watcher_id=?", (wid,))
            self._db.execute("DELETE FROM pending WHERE watcher_id=?", (wid,))
            self._db.commit()
        return cur.rowcount > 0

    # --- snapshots (sealed) ------------------------------------------------------------------------------
    def last_snapshot(self, wid: str, section: str) -> builtins.list[dict[str, str]] | None:
        with self._lock:
            r = self._db.execute("SELECT sealed FROM snapshots WHERE watcher_id=? AND section=? ORDER BY id DESC "
                                 "LIMIT 1", (wid, section)).fetchone()
        if r is None:
            return None
        try:
            data = json.loads(self._unseal(r["sealed"]))
        except (ValueError, TypeError):
            return None  # sealed by another Windows account, or damaged: starts again from this run
        return data if isinstance(data, list) else None

    def save_snapshot(self, wid: str, section: str, items: builtins.list[dict[str, str]]) -> None:
        sealed = self._seal(json.dumps(items, ensure_ascii=False))  # VaultUnavailableError: nothing written
        with self._lock:
            self._db.execute("INSERT INTO snapshots(watcher_id,section,taken,sealed) VALUES(?,?,?,?)",
                             (wid, section, _now(), sealed))
            self._db.execute("DELETE FROM snapshots WHERE watcher_id=? AND section=? AND id NOT IN (SELECT id FROM "
                             "snapshots WHERE watcher_id=? AND section=? ORDER BY id DESC LIMIT ?)",
                             (wid, section, wid, section, KEEP_SNAPSHOTS))
            self._db.commit()

    # --- digest queue and reminder offers (sealed) ---------------------------------------------------------
    def add_pending(self, wid: str, kind: str, item: dict[str, Any]) -> int:
        sealed = self._seal(json.dumps(item, ensure_ascii=False))
        limit = MAX_PENDING if kind == "digest" else MAX_OFFERS
        with self._lock:
            cur = self._db.execute("INSERT INTO pending(watcher_id,kind,created,sealed) VALUES(?,?,?,?)",
                                   (wid, kind, _now(), sealed))
            self._db.execute("DELETE FROM pending WHERE watcher_id=? AND kind=? AND id NOT IN (SELECT id FROM pending "
                             "WHERE watcher_id=? AND kind=? ORDER BY id DESC LIMIT ?)", (wid, kind, wid, kind, limit))
            self._db.commit()
        return int(cur.lastrowid or 0)

    def pending(self, wid: str | None, kind: str, state: str = "open") -> builtins.list[dict[str, Any]]:
        q = "SELECT * FROM pending WHERE kind=? AND state=?" + (" AND watcher_id=?" if wid else "") + " ORDER BY id"
        with self._lock:
            rows = self._db.execute(q, (kind, state, wid) if wid else (kind, state)).fetchall()
        out = []
        for r in rows:
            try:
                item = json.loads(self._unseal(r["sealed"]))
            except (ValueError, TypeError):
                continue
            out.append({"id": r["id"], "watcher_id": r["watcher_id"], "created": r["created"], "item": item})
        return out

    def set_pending_state(self, pid: int, state: str) -> bool:
        with self._lock:
            cur = self._db.execute("UPDATE pending SET state=? WHERE id=?", (state, pid))
            self._db.commit()
        return cur.rowcount > 0

    def clear_pending(self, wid: str, kind: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM pending WHERE watcher_id=? AND kind=?", (wid, kind))
            self._db.commit()
