"""What Delta learns about how the user wants it to behave, from their own decisions.

kinds
  avoid       user refused this kind of action AVOID_AFTER times in a row -> stop proposing it
  trust       user approved a plain write TRUST_AFTER times in a row -> *suggest* not asking
              (status 'pending' until the user presses Keep; never for deletes/sends/system)
  preference  a standing instruction from the user's corrections ("keep answers short")
  lesson      something learned from a failed task (written by the reflection step)
status
  active | pending (waiting for the user's Keep) | undone (the user said no: never re-learned)

Every item is visible, and the user can keep or undo it. Undone items block re-learning the same
key, so Delta never argues with an explicit "undo".
"""

from __future__ import annotations

import builtins
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

AVOID_AFTER = 2
TRUST_AFTER = 3
TRUSTABLE_RISKS = {"write"}  # deletes, sends and system actions always ask

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, risk TEXT NOT NULL, summary TEXT NOT NULL,
  approved INTEGER NOT NULL, ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_key ON decisions(key, id);
CREATE TABLE IF NOT EXISTS learned(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL, content TEXT NOT NULL,
  status TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS learned_kind ON learned(kind, status);
"""


@dataclass
class Learned:
    id: int
    kind: str
    key: str
    content: str
    status: str
    created: str
    updated: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class LearningStore:
    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._db.close()

    # --- items -------------------------------------------------------------------------------
    def add(self, kind: str, key: str, content: str, status: str = "active") -> Learned:
        now = _now()
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO learned(kind, key, content, status, created, updated) VALUES (?,?,?,?,?,?)",
                (kind, key, content.strip()[:300], status, now, now))
        item = self.get(int(cur.lastrowid or 0))
        assert item
        return item

    def get(self, item_id: int) -> Learned | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM learned WHERE id=?", (item_id,)).fetchone()
        return Learned(*r) if r else None

    def find(self, kind: str, key: str) -> Learned | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM learned WHERE kind=? AND key=? ORDER BY id DESC LIMIT 1",
                                 (kind, key)).fetchone()
        return Learned(*r) if r else None

    def list(self, status: str | None = None, kind: str | None = None) -> builtins.list[Learned]:
        q, params = "SELECT * FROM learned WHERE 1=1", list[str]()
        if status:
            q, params = q + " AND status=?", [*params, status]
        if kind:
            q, params = q + " AND kind=?", [*params, kind]
        with self._lock:
            return [Learned(*r) for r in self._db.execute(q + " ORDER BY id DESC", params)]

    def set_status(self, item_id: int, status: str) -> Learned | None:
        if status not in {"active", "pending", "undone"}:
            raise ValueError(status)
        with self._lock, self._db:
            self._db.execute("UPDATE learned SET status=?, updated=? WHERE id=?", (status, _now(), item_id))
        return self.get(item_id)

    def is_active(self, kind: str, key: str) -> Learned | None:
        item = self.find(kind, key)
        return item if item and item.status == "active" else None

    # --- decisions ---------------------------------------------------------------------------
    def _streak(self, key: str) -> tuple[bool | None, int]:
        """(latest decision, how many identical decisions in a row ending with it)."""
        with self._lock:
            rows = [bool(r[0]) for r in self._db.execute(
                "SELECT approved FROM decisions WHERE key=? ORDER BY id DESC LIMIT 20", (key,))]
        if not rows:
            return None, 0
        n = 0
        for r in rows:
            if r != rows[0]:
                break
            n += 1
        return rows[0], n

    def record_decision(self, key: str, risk: str, summary: str, approved: bool) -> Learned | None:
        """Store a human decision; return a newly learned item if this decision taught something."""
        with self._lock, self._db:
            self._db.execute("INSERT INTO decisions(key, risk, summary, approved, ts) VALUES (?,?,?,?,?)",
                             (key, risk, summary[:300], int(approved), _now()))
        latest, n = self._streak(key)
        if latest is False and n >= AVOID_AFTER and self.find("avoid", key) is None:
            return self.add("avoid", key, f"Don't propose this again: {describe(key)} (e.g. {summary[:120]})")
        if latest is True and n >= TRUST_AFTER and risk in TRUSTABLE_RISKS and self.find("trust", key) is None:
            return self.add("trust", key, f"Stop asking before: {describe(key)} (approved {n}× in a row)",
                            status="pending")
        return None


VERBS = {"write": "changing files in", "destructive": "deleting or overwriting things in",
         "external": "sending things from", "act": "doing this in"}


def split_key(key: str) -> tuple[str, str, str]:
    """'tool|risk|scope' -> (tool, risk, scope); missing parts are ''."""
    parts = [*key.split("|", 2), "", ""]
    return parts[0], parts[1], parts[2]


def is_folder_scope(scope: str) -> bool:
    return len(scope) > 3 and (":\\" in scope or scope.startswith("\\\\") or scope.startswith("/"))


def describe(key: str) -> str:
    """Human wording, e.g. 'delete_path|destructive|c:\\x\\downloads' ->
    'deleting or overwriting things in c:\\x\\downloads'. Folder-scoped rules apply to every tool
    (a refused delete cannot come back as a shell command)."""
    tool, risk, scope = split_key(key)
    if is_folder_scope(scope):
        return f"{VERBS.get(risk, risk)} {scope}"
    return f"{tool} {scope}".strip()
