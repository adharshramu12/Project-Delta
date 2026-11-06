"""What Delta learns about how the user wants it to behave, from their own decisions.

kinds
  avoid       user refused this kind of action AVOID_AFTER times in a row -> stop proposing it
  trust       user approved a plain write TRUST_AFTER times in a row -> *suggest* not asking
              (status 'pending' until the user presses Keep; never for deletes/sends/system)
  preference  a standing instruction from the user's corrections ("keep answers short")
  lesson      a recovery that worked in self-heal, for a site or app (delta.learning.lessons)
status
  active | pending (waiting for the user's Keep) | undone (the user said no: never re-learned)
  candidate (a lesson waiting for evidence: not used until the promotion gate passes it, delta.learning.gate)
  retired (it kept failing after it was promoted: no longer used)

Every item is visible, and the user can keep or undo it. Undone items block re-learning the same
key, so Delta never argues with an explicit "undo".

Provenance (Batch D): every item records its trust level, origin and the task it came from
(delta.memory.provenance). An untrusted item (traced to outside content) can never become active: set_status
refuses it in code, whoever asks. Every change is appended to `learn_events` (append-only), the item's full
history.
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
CREATE TABLE IF NOT EXISTS learn_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, item TEXT NOT NULL, event TEXT NOT NULL,
  task_id TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS learn_events_item ON learn_events(item, id);
CREATE TRIGGER IF NOT EXISTS learn_events_append_only BEFORE UPDATE ON learn_events
BEGIN SELECT RAISE(ABORT, 'the learning history is append-only'); END;
"""
# Columns added after the first release (additive: an older learning.db keeps every row).
ADDED_COLUMNS = [
    ("trust", "TEXT NOT NULL DEFAULT 'trusted'"),
    ("origin", "TEXT NOT NULL DEFAULT 'user_message'"),
    ("task_id", "TEXT NOT NULL DEFAULT ''"),
    ("successes", "INTEGER NOT NULL DEFAULT 0"),
    ("failures", "INTEGER NOT NULL DEFAULT 0"),
    ("evals_passed", "INTEGER NOT NULL DEFAULT 0"),
]
STATUSES = {"active", "pending", "undone", "candidate", "retired"}


class UntrustedPromotionError(ValueError):
    """An item traced to outside content was about to become a rule. Never allowed."""


@dataclass
class Learned:
    id: int
    kind: str
    key: str
    content: str
    status: str
    created: str
    updated: str
    trust: str = "trusted"
    origin: str = "user_message"
    task_id: str = ""
    successes: int = 0
    failures: int = 0
    evals_passed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class LearningStore:
    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(learned)")}
        with self._db:
            for name, decl in ADDED_COLUMNS:
                if name not in cols:
                    self._db.execute(f"ALTER TABLE learned ADD COLUMN {name} {decl}")

    _SELECT = ("SELECT id, kind, key, content, status, created, updated, trust, origin, task_id, successes, "
               "failures, evals_passed FROM learned")

    def close(self) -> None:
        self._db.close()

    # --- items -------------------------------------------------------------------------------
    def add(self, kind: str, key: str, content: str, status: str = "active", *, trust: str = "trusted",
            origin: str = "user_message", task_id: str = "") -> Learned:
        from delta.memory.redact import redact_secrets

        if trust == "untrusted" and status == "active":
            raise UntrustedPromotionError(f"an untrusted {kind} cannot be stored as active")
        now = _now()
        text = redact_secrets(content.strip())[0][:300]
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO learned(kind, key, content, status, created, updated, trust, origin, task_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (kind, key, text, status, now, now, trust, origin, task_id))
        item = self.get(int(cur.lastrowid or 0))
        assert item
        self.log(f"L{item.id}", "added", task_id, f"{kind} as {status} ({trust}): {text[:120]}")
        return item

    def get(self, item_id: int) -> Learned | None:
        with self._lock:
            r = self._db.execute(self._SELECT + " WHERE id=?", (item_id,)).fetchone()
        return Learned(*r) if r else None

    def find(self, kind: str, key: str) -> Learned | None:
        with self._lock:
            r = self._db.execute(self._SELECT + " WHERE kind=? AND key=? ORDER BY id DESC LIMIT 1",
                                 (kind, key)).fetchone()
        return Learned(*r) if r else None

    def list(self, status: str | None = None, kind: str | None = None) -> builtins.list[Learned]:
        q, params = self._SELECT + " WHERE 1=1", list[str]()
        if status:
            q, params = q + " AND status=?", [*params, status]
        if kind:
            q, params = q + " AND kind=?", [*params, kind]
        with self._lock:
            return [Learned(*r) for r in self._db.execute(q + " ORDER BY id DESC", params)]

    def set_status(self, item_id: int, status: str, by: str = "", task_id: str = "") -> Learned | None:
        if status not in STATUSES:
            raise ValueError(status)
        item = self.get(item_id)
        if item is None:
            return None
        if status == "active" and item.trust == "untrusted":
            self.log(f"L{item_id}", "promotion_refused", task_id, "traced to outside content: never a rule")
            raise UntrustedPromotionError(f"learned item {item_id} came from outside content and cannot be used")
        with self._lock, self._db:
            self._db.execute("UPDATE learned SET status=?, updated=? WHERE id=?", (status, _now(), item_id))
        if status != item.status:
            self.log(f"L{item_id}", f"status:{status}", task_id,
                     f"{item.status} -> {status}" + (f" by {by}" if by else ""))
        return self.get(item_id)

    def count(self, item_id: int, *, success: bool | None = None, eval_passed: bool = False) -> Learned | None:
        """Add one real run (success True / False) or one passing eval to an item's evidence."""
        col = "evals_passed" if eval_passed else ("successes" if success else "failures")
        reset = "" if eval_passed else (", failures=0" if success else "")
        with self._lock, self._db:
            self._db.execute(f"UPDATE learned SET {col}={col}+1{reset}, updated=? WHERE id=?", (_now(), item_id))
        return self.get(item_id)

    # --- history -----------------------------------------------------------------------------
    def log(self, item: str, event: str, task_id: str = "", detail: str = "") -> None:
        """One line of an item's history (L<id> lessons, R<id> recipe candidates, S<id> skills)."""
        with self._lock, self._db:
            self._db.execute("INSERT INTO learn_events(ts, item, event, task_id, detail) VALUES (?,?,?,?,?)",
                             (_now(), item, event, task_id, detail[:500]))

    def history(self, item: str | None = None, limit: int = 200) -> builtins.list[dict[str, Any]]:
        q, params = "SELECT ts, item, event, task_id, detail FROM learn_events", list[Any]()
        if item:
            q, params = q + " WHERE item=?", [item]
        with self._lock:
            rows = self._db.execute(q + " ORDER BY id DESC LIMIT ?", [*params, limit]).fetchall()
        return [{"ts": r[0], "item": r[1], "event": r[2], "task_id": r[3], "detail": r[4]} for r in rows]

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
