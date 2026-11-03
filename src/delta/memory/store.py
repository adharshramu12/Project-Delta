"""Local long-term memory in SQLite with FTS5 full-text recall.

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

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'user',
  created TEXT NOT NULL,
  updated TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(content, content='memories', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content);
  INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
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

    def close(self) -> None:
        self._db.close()

    def add(self, content: str, kind: str = "profile", source: str = "user") -> int:
        content = content.strip()
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock, self._db:
            row = self._db.execute("SELECT id FROM memories WHERE kind=? AND lower(content)=lower(?)",
                                   (kind, content)).fetchone()
            if row:
                self._db.execute("UPDATE memories SET updated=? WHERE id=?", (now, row[0]))
                return int(row[0])
            cur = self._db.execute(
                "INSERT INTO memories(kind, content, source, created, updated) VALUES (?,?,?,?,?)",
                (kind, content, source, now, now))
            return int(cur.lastrowid or 0)

    def update(self, mem_id: int, content: str) -> bool:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock, self._db:
            return self._db.execute("UPDATE memories SET content=?, updated=? WHERE id=?",
                                    (content, now, mem_id)).rowcount > 0

    def delete(self, mem_id: int) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM memories WHERE id=?", (mem_id,)).rowcount > 0

    def clear(self) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM memories")

    def list(self, kind: str | None = None, limit: int = 200) -> list[Memory]:
        q = "SELECT id, kind, content, source, updated FROM memories"
        params: tuple = ()
        if kind:
            q += " WHERE kind=?"
            params = (kind,)
        q += " ORDER BY updated DESC LIMIT ?"
        with self._lock:
            return [Memory(*r) for r in self._db.execute(q, (*params, limit))]

    def search(self, query: str, k: int = 6, kind: str | None = None) -> builtins.list[Memory]:
        words = keywords(query)
        if not words:
            return []
        match = " OR ".join(f'"{w}"*' for w in words)
        sql = ("SELECT m.id, m.kind, m.content, m.source, m.updated FROM memories_fts f "
               "JOIN memories m ON m.id=f.rowid WHERE memories_fts MATCH ?")
        params: builtins.list[str | int] = [match]
        if kind:
            sql += " AND m.kind=?"
            params.append(kind)
        sql += " ORDER BY bm25(memories_fts) LIMIT ?"
        params.append(k)
        with self._lock:
            return [Memory(*r) for r in self._db.execute(sql, params)]

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
