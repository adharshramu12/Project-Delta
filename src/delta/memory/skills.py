"""Skill library: reusable procedures Delta learns from its own verified successes.

Lifecycle (evidence-gated, so learning can never quietly make Delta worse):
  candidate  learned from one verified success; offered to the planner as "untested"
  active     promoted after PROMOTE_AFTER verified successes
  retired    failed RETIRE_AFTER times and more often than it worked; never offered again
Users can see, pin (active), retire or delete any skill. Skills are plain JSON, so they can be
exported and shared.
"""

from __future__ import annotations

import builtins
import json
import re
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROMOTE_AFTER = 2
RETIRE_AFTER = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS skills(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  when_to_use TEXT NOT NULL,
  procedure TEXT NOT NULL,      -- JSON list of steps
  tools TEXT NOT NULL,          -- JSON list of tool names, in order
  status TEXT NOT NULL DEFAULT 'candidate',
  successes INTEGER NOT NULL DEFAULT 1,
  failures INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'learned',
  created TEXT NOT NULL,
  last_used TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(name, when_to_use, content='skills', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS skills_ai AFTER INSERT ON skills BEGIN
  INSERT INTO skills_fts(rowid, name, when_to_use) VALUES (new.id, new.name, new.when_to_use);
END;
CREATE TRIGGER IF NOT EXISTS skills_ad AFTER DELETE ON skills BEGIN
  INSERT INTO skills_fts(skills_fts, rowid, name, when_to_use) VALUES('delete', old.id, old.name, old.when_to_use);
END;
"""


@dataclass
class Skill:
    id: int
    name: str
    when_to_use: str
    procedure: list[str]
    tools: list[str]
    status: str
    successes: int
    failures: int
    source: str
    created: str
    last_used: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def prompt_line(self) -> str:
        tag = "" if self.status == "active" else " (untested)"
        steps = " → ".join(self.procedure)
        return f"#{self.id} {self.name}{tag} [{self.successes}✓/{self.failures}✗]: {self.when_to_use}. Steps: {steps}"


@dataclass
class SkillChange:
    skill: Skill
    change: str  # learned | reinforced | promoted | retired | weakened


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class SkillStore:
    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._db.close()

    def _row(self, r: tuple[Any, ...]) -> Skill:
        return Skill(r[0], r[1], r[2], json.loads(r[3]), json.loads(r[4]), *r[5:])

    _COLS = "id, name, when_to_use, procedure, tools, status, successes, failures, source, created, last_used"

    def get(self, skill_id: int) -> Skill | None:
        with self._lock:
            r = self._db.execute(f"SELECT {self._COLS} FROM skills WHERE id=?", (skill_id,)).fetchone()
        return self._row(r) if r else None

    def list(self, include_retired: bool = True) -> builtins.list[Skill]:
        q = f"SELECT {self._COLS} FROM skills"
        if not include_retired:
            q += " WHERE status != 'retired'"
        with self._lock:
            return [self._row(r) for r in self._db.execute(q + " ORDER BY successes DESC, id DESC")]

    def relevant(self, query: str, k: int = 3) -> builtins.list[Skill]:
        words = re.findall(r"\w{3,}", query.lower())[:12]
        if not words:
            return []
        match = " OR ".join(f'"{w}"' for w in words)
        sql = (f"SELECT {', '.join('s.' + c.strip() for c in self._COLS.split(','))} FROM skills_fts f "
               "JOIN skills s ON s.id=f.rowid WHERE skills_fts MATCH ? AND s.status != 'retired' "
               "ORDER BY bm25(skills_fts) LIMIT ?")
        with self._lock:
            return [self._row(r) for r in self._db.execute(sql, (match, k))]

    def learn(self, name: str, when_to_use: str, procedure: builtins.list[str], tools: builtins.list[str],
              source: str = "learned") -> SkillChange:
        """Add a skill, or reinforce an existing one that does the same job with the same tools."""
        for s in self.relevant(f"{name} {when_to_use}", k=5):
            if s.tools == tools:
                return self.record(s.id, success=True)
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO skills(name, when_to_use, procedure, tools, source, created) VALUES (?,?,?,?,?,?)",
                (name.strip()[:80], when_to_use.strip()[:300], json.dumps(procedure[:15]), json.dumps(tools),
                 source, _now()))
            sid = int(cur.lastrowid or 0)
        skill = self.get(sid)
        assert skill
        return SkillChange(skill, "learned")

    def record(self, skill_id: int, success: bool) -> SkillChange:
        s = self.get(skill_id)
        if s is None:
            raise KeyError(skill_id)
        succ, fail, status = s.successes + success, s.failures + (not success), s.status
        change = "reinforced" if success else "weakened"
        if success and status == "candidate" and succ >= PROMOTE_AFTER:
            status, change = "active", "promoted"
        if not success and fail >= RETIRE_AFTER and fail > succ:
            status, change = "retired", "retired"
        with self._lock, self._db:
            self._db.execute("UPDATE skills SET successes=?, failures=?, status=?, last_used=? WHERE id=?",
                             (succ, fail, status, _now(), skill_id))
        updated = self.get(skill_id)
        assert updated
        return SkillChange(updated, change)

    def set_status(self, skill_id: int, status: str) -> bool:
        if status not in {"candidate", "active", "retired"}:
            raise ValueError(status)
        with self._lock, self._db:
            return self._db.execute("UPDATE skills SET status=? WHERE id=?", (status, skill_id)).rowcount > 0

    def delete(self, skill_id: int) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM skills WHERE id=?", (skill_id,)).rowcount > 0

    def export(self) -> builtins.list[dict[str, Any]]:
        """Shareable form: no ids, counters or dates."""
        return [{"name": s.name, "when_to_use": s.when_to_use, "procedure": s.procedure, "tools": s.tools}
                for s in self.list(include_retired=False) if s.status == "active"]

    def import_skills(self, items: builtins.list[dict[str, Any]]) -> int:
        """Imported skills start as candidates: they must prove themselves on this PC first."""
        n = 0
        for it in items:
            if {"name", "when_to_use", "procedure", "tools"} <= it.keys():
                self.learn(it["name"], it["when_to_use"], list(it["procedure"]), list(it["tools"]), source="shared")
                n += 1
        return n
