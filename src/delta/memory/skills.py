"""Skill library: reusable procedures Delta learns from its own verified successes.

Lifecycle (evidence-gated, so learning can never quietly make Delta worse):
  candidate  learned from one verified success; offered to the planner as "untested"
  active     promoted after PROMOTE_AFTER verified successes
  retired    failed RETIRE_AFTER times and more often than it worked; never offered again
Users can see, pin (active), retire or delete any skill. Skills are plain JSON, so they can be
exported and shared.

Recipe candidates (Batch D, delta.learning.repair): when a step of a replayed recipe fails and self-heal finds
a way that works, the repaired recipe is saved here as a candidate. The skill's working recipe is never
overwritten until the candidate passes the promotion gate (delta.learning.gate); a candidate traced to outside
content is untrusted and can never be promoted.
"""

from __future__ import annotations

import builtins
import json
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from delta.memory.keywords import keywords, overlap
from delta.system import sqlite_safe

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
  last_used TEXT,
  recipe TEXT                   -- JSON {"steps": [{tool, args}], "slots": {...}}: replay without AI
);
CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(name, when_to_use, content='skills', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS skills_ai AFTER INSERT ON skills BEGIN
  INSERT INTO skills_fts(rowid, name, when_to_use) VALUES (new.id, new.name, new.when_to_use);
END;
CREATE TRIGGER IF NOT EXISTS skills_ad AFTER DELETE ON skills BEGIN
  INSERT INTO skills_fts(skills_fts, rowid, name, when_to_use) VALUES('delete', old.id, old.name, old.when_to_use);
END;
CREATE TABLE IF NOT EXISTS recipe_candidates(
  id INTEGER PRIMARY KEY,
  skill_id INTEGER NOT NULL,
  recipe TEXT NOT NULL,          -- the repaired recipe (JSON, same shape as skills.recipe)
  base_recipe TEXT NOT NULL,     -- the working recipe it was repaired from
  step INTEGER NOT NULL,         -- which step (0-based) was repaired
  failure TEXT NOT NULL DEFAULT '',
  change TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'candidate',  -- candidate | active | rejected | retired | superseded
  trust TEXT NOT NULL DEFAULT 'trusted',
  task_id TEXT NOT NULL DEFAULT '',
  successes INTEGER NOT NULL DEFAULT 0,
  failures INTEGER NOT NULL DEFAULT 0,
  evals_passed INTEGER NOT NULL DEFAULT 0,
  created TEXT NOT NULL,
  updated TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS recipe_candidates_skill ON recipe_candidates(skill_id, status);
"""
CANDIDATE_STATUSES = {"candidate", "active", "rejected", "retired", "superseded"}


@dataclass
class RecipeCandidate:
    id: int
    skill_id: int
    recipe: str
    base_recipe: str
    step: int
    failure: str
    change: str
    status: str
    trust: str
    task_id: str
    successes: int
    failures: int
    evals_passed: int
    created: str
    updated: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    recipe: str | None = None  # JSON, see delta.core.replay.Recipe

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
        self._db = sqlite_safe.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._migrate()
        self._lock = threading.Lock()

    def _migrate(self) -> None:
        """Databases created before recipes existed get the column added (data kept)."""
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(skills)")}
        if "recipe" not in cols:
            with self._db:
                self._db.execute("ALTER TABLE skills ADD COLUMN recipe TEXT")

    def close(self) -> None:
        self._db.close()

    def _row(self, r: tuple[Any, ...]) -> Skill:
        return Skill(r[0], r[1], r[2], json.loads(r[3]), json.loads(r[4]), *r[5:])

    _COLS = ("id, name, when_to_use, procedure, tools, status, successes, failures, source, created, last_used, "
             "recipe")

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
        """Skills that really fit `query`: sharing content words with its name or when-to-use (two, or
        all of them for a one-word query). A skill that does not fit costs prompt tokens, and for
        replay an extra AI call, for nothing."""
        words = keywords(query)
        if not words:
            return []
        match = " OR ".join(f'"{w}"*' for w in words)
        sql = (f"SELECT {', '.join('s.' + c.strip() for c in self._COLS.split(','))} FROM skills_fts f "
               "JOIN skills s ON s.id=f.rowid WHERE skills_fts MATCH ? AND s.status != 'retired' "
               "ORDER BY bm25(skills_fts) LIMIT ?")
        with self._lock:
            found = [self._row(r) for r in self._db.execute(sql, (match, k * 3))]
        need = min(2, len(words))
        return [s for s in found if overlap(words, f"{s.name} {s.when_to_use}") >= need][:k]

    def learn(self, name: str, when_to_use: str, procedure: builtins.list[str], tools: builtins.list[str],
              source: str = "learned", recipe: str | None = None) -> SkillChange:
        """Add a skill, or reinforce an existing one that does the same job with the same tools
        (giving it a replay recipe if it had none yet)."""
        for s in self.relevant(f"{name} {when_to_use}", k=5):
            if s.tools == tools:
                if recipe and not s.recipe:
                    self.set_recipe(s.id, recipe)
                return self.record(s.id, success=True)
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO skills(name, when_to_use, procedure, tools, source, created, recipe) "
                "VALUES (?,?,?,?,?,?,?)",
                (name.strip()[:80], when_to_use.strip()[:300], json.dumps(procedure[:15]), json.dumps(tools),
                 source, _now(), recipe))
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

    def set_recipe(self, skill_id: int, recipe: str | None) -> bool:
        with self._lock, self._db:
            return self._db.execute("UPDATE skills SET recipe=? WHERE id=?", (recipe, skill_id)).rowcount > 0

    def replayable(self, query: str, k: int = 3) -> builtins.list[Skill]:
        """Proven (active) skills with a recipe that may fit `query`, best match first."""
        return [s for s in self.relevant(query, k=k) if s.status == "active" and s.recipe]

    def set_status(self, skill_id: int, status: str) -> bool:
        if status not in {"candidate", "active", "retired"}:
            raise ValueError(status)
        with self._lock, self._db:
            return self._db.execute("UPDATE skills SET status=? WHERE id=?", (status, skill_id)).rowcount > 0

    def delete(self, skill_id: int) -> bool:
        with self._lock, self._db:
            self._db.execute("DELETE FROM recipe_candidates WHERE skill_id=?", (skill_id,))
            return self._db.execute("DELETE FROM skills WHERE id=?", (skill_id,)).rowcount > 0

    # --- recipe candidates (delta.learning.repair / gate) -----------------------------------------
    _CAND = ("id, skill_id, recipe, base_recipe, step, failure, change, status, trust, task_id, successes, failures, "
             "evals_passed, created, updated")

    def add_candidate(self, skill_id: int, recipe: str, base_recipe: str, step: int, failure: str, change: str,
                      trust: str = "trusted", task_id: str = "") -> RecipeCandidate:
        """Save a repaired recipe as a candidate; the same repair again returns the existing candidate."""
        for c in self.candidates(skill_id, status="candidate"):
            if c.recipe == recipe:
                return c
        now = _now()
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO recipe_candidates(skill_id, recipe, base_recipe, step, failure, change, status, trust, "
                "task_id, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (skill_id, recipe, base_recipe, step, failure, change[:300], "candidate", trust, task_id, now, now))
        found = self.get_candidate(int(cur.lastrowid or 0))
        assert found
        return found

    def get_candidate(self, cand_id: int) -> RecipeCandidate | None:
        with self._lock:
            r = self._db.execute(f"SELECT {self._CAND} FROM recipe_candidates WHERE id=?", (cand_id,)).fetchone()
        return RecipeCandidate(*r) if r else None

    def candidates(self, skill_id: int | None = None, status: str | None = None) -> builtins.list[RecipeCandidate]:
        q, params = f"SELECT {self._CAND} FROM recipe_candidates WHERE 1=1", list[Any]()
        if skill_id is not None:
            q, params = q + " AND skill_id=?", [*params, skill_id]
        if status:
            q, params = q + " AND status=?", [*params, status]
        with self._lock:
            return [RecipeCandidate(*r) for r in self._db.execute(q + " ORDER BY id DESC", params)]

    def count_candidate(self, cand_id: int, *, success: bool | None = None,
                        eval_passed: bool = False) -> RecipeCandidate | None:
        col = "evals_passed" if eval_passed else ("successes" if success else "failures")
        reset = "" if eval_passed else (", failures=0" if success else "")
        with self._lock, self._db:
            self._db.execute(f"UPDATE recipe_candidates SET {col}={col}+1{reset}, updated=? WHERE id=?",
                             (_now(), cand_id))
        return self.get_candidate(cand_id)

    def set_candidate_status(self, cand_id: int, status: str) -> RecipeCandidate | None:
        """Change a candidate's status. Making it active puts its recipe on the skill (the old working one stays
        in the candidate's base_recipe, so it can be told apart and restored); untrusted ones never become active."""
        if status not in CANDIDATE_STATUSES:
            raise ValueError(status)
        cand = self.get_candidate(cand_id)
        if cand is None:
            return None
        if status == "active":
            from delta.learning.store import UntrustedPromotionError

            if cand.trust == "untrusted":
                raise UntrustedPromotionError(f"recipe candidate {cand_id} came from outside content")
            with self._lock, self._db:
                self._db.execute("UPDATE recipe_candidates SET status='superseded', updated=? "
                                 "WHERE skill_id=? AND status='active' AND id != ?", (_now(), cand.skill_id, cand_id))
                self._db.execute("UPDATE skills SET recipe=? WHERE id=?", (cand.recipe, cand.skill_id))
        with self._lock, self._db:
            self._db.execute("UPDATE recipe_candidates SET status=?, updated=? WHERE id=?", (status, _now(), cand_id))
        return self.get_candidate(cand_id)

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
