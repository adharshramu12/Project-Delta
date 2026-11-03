"""Local long-term memory in SQLite. Contents are encrypted at rest with Windows DPAPI (system/vault.py),
so recall scores keywords in Python instead of a full-text index (an index would store plain text).

kinds:
  profile  - durable facts/preferences about the user ("prefers Antigravity for Python")
  episode  - one-line summaries of past tasks and how they ended
"""

from __future__ import annotations

import builtins
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from delta.memory.keywords import keywords, overlap
from delta.system.vault import PREFIX, scrub, seal, unseal

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'user',
  created TEXT NOT NULL,
  updated TEXT NOT NULL
);
-- the old plain-text full-text index goes: it kept a readable copy of every memory
DROP TRIGGER IF EXISTS memories_ai;
DROP TRIGGER IF EXISTS memories_ad;
DROP TRIGGER IF EXISTS memories_au;
DROP TABLE IF EXISTS memories_fts;
"""


@dataclass
class Memory:
    id: int
    kind: str
    content: str
    source: str
    updated: str


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._seal_old_rows()

    def _seal_old_rows(self) -> None:
        """Rows written before encryption: seal them once, then VACUUM so no plain copy stays in the file."""
        with self._lock:
            rows = self._db.execute("SELECT id, content FROM memories WHERE content NOT LIKE ?",
                                    (PREFIX + "%",)).fetchall()
            sealed = [(seal(c), i) for i, c in rows]
            if any(s != c for (s, _), (_, c) in zip(sealed, rows, strict=True)):
                with self._db:
                    self._db.executemany("UPDATE memories SET content=? WHERE id=?", sealed)
            scrub(self._db)

    def _rows(self, kind: str | None = None) -> builtins.list[Memory]:
        q = "SELECT id, kind, content, source, updated FROM memories"
        params: tuple = ()
        if kind:
            q += " WHERE kind=?"
            params = (kind,)
        with self._lock:
            rows = self._db.execute(q + " ORDER BY updated DESC, id DESC", params).fetchall()
        return [Memory(i, k, unseal(c), s, u) for i, k, c, s, u in rows]

    def close(self) -> None:
        self._db.close()

    def add(self, content: str, kind: str = "profile", source: str = "user") -> int:
        content = content.strip()
        now = datetime.now(UTC).isoformat(timespec="seconds")
        same = next((m for m in self._rows(kind) if m.content.lower() == content.lower()), None)
        with self._lock, self._db:
            if same:
                self._db.execute("UPDATE memories SET updated=? WHERE id=?", (now, same.id))
                return same.id
            cur = self._db.execute(
                "INSERT INTO memories(kind, content, source, created, updated) VALUES (?,?,?,?,?)",
                (kind, seal(content), source, now, now))
            return int(cur.lastrowid or 0)

    def update(self, mem_id: int, content: str) -> bool:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock, self._db:
            return self._db.execute("UPDATE memories SET content=?, updated=? WHERE id=?",
                                    (seal(content), now, mem_id)).rowcount > 0

    def delete(self, mem_id: int) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM memories WHERE id=?", (mem_id,)).rowcount > 0

    def clear(self) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM memories")

    def list(self, kind: str | None = None, limit: int = 200) -> list[Memory]:
        return self._rows(kind)[:limit]

    def search(self, query: str, k: int = 6, kind: str | None = None) -> builtins.list[Memory]:
        """Best keyword matches (a word counts when a word in the memory starts with it), newest first on ties."""
        words = keywords(query)
        if not words:
            return []
        scored = []
        for m in self._rows(kind):
            tokens = re.findall(r"\w+", m.content.lower())
            score = sum(any(t.startswith(w) for t in tokens) for w in words)
            if score:
                scored.append((score, m))
        scored.sort(key=lambda sm: -sm[0])  # stable: newest first among equal scores
        return [m for _, m in scored[:k]]

    def context_for(self, query: str, k: int = 6) -> str:
        """Prompt block: recent profile facts + past tasks that really match this request.
        Repeated facts ("User's name is X" / "The user's name is X") are sent once."""
        profile = self.list("profile", limit=25)
        words = keywords(query)
        need = min(2, len(words))
        related = [m for m in self.search(query, k) if m.id not in {p.id for p in profile}
                   and overlap(words, m.content) >= need]
        lines: dict[str, str] = {}
        for m in profile:
            key = re.sub(r"\W+|^the ", " ", m.content.lower().removeprefix("the ")).strip()
            lines.setdefault(key, f"- {m.content}")
        lines.update({f"past {m.id}": f"- (past) {m.content}" for m in related})
        return "\n".join(lines.values())
