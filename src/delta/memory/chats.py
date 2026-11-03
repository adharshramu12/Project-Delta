"""Chats: separate conversations, like Claude's sidebar. Stored on this PC in SQLite.

Each chat keeps its own messages. What a new request sees of its chat is a short, bounded recap of
the last few turns (`context_for`), never the whole history, so follow-ups ("send it to him",
"make it 5 pm instead") work while every request stays cheap. Memory, skills and approvals are
shared by all chats on purpose: what Delta learns in one chat it knows in every chat.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from builtins import list as List  # the class has a list() method that hides the builtin
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from delta.core.intents import small_talk
from delta.system.vault import PREFIX, seal, unseal

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL, text TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}', ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS chat_messages_chat ON chat_messages(chat_id, id);
"""

NEW_TITLE = "New chat"
_FILLER = re.compile(r"^(?:please|pls|can you|could you|hey|hi|delta|ok|okay|now|just)[,\s]+", re.I)


def title_from(text: str, words: int = 7) -> str:
    """A chat title from its first message, with no AI call: "can you play seetharamam song" ->
    "Play seetharamam song"."""
    t = " ".join(text.split())
    while (s := _FILLER.sub("", t)) != t:
        t = s
    t = " ".join(t.split()[:words]).rstrip(".,!?:;")
    return (t[:1].upper() + t[1:])[:60] if t else NEW_TITLE


@dataclass
class Chat:
    id: str
    title: str
    created: float
    updated: float
    pinned: bool = False
    preview: str = ""
    count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ChatStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._seal_old_rows()

    def _seal_old_rows(self) -> None:
        """Titles and messages are encrypted at rest (DPAPI). Rows from before that are sealed once."""
        with self._lock:
            chats = self._db.execute("SELECT id, title FROM chats WHERE title NOT LIKE ?", (PREFIX + "%",)).fetchall()
            msgs = self._db.execute("SELECT id, text FROM chat_messages WHERE text NOT LIKE ?",
                                    (PREFIX + "%",)).fetchall()
            new_chats = [(seal(r["title"]), r["id"]) for r in chats]
            new_msgs = [(seal(r["text"]), r["id"]) for r in msgs]
            changed = any(t.startswith(PREFIX) for t, _ in new_chats + new_msgs)
            if not changed:
                return
            with self._db:
                self._db.executemany("UPDATE chats SET title = ? WHERE id = ?", new_chats)
                self._db.executemany("UPDATE chat_messages SET text = ? WHERE id = ?", new_msgs)
            self._db.execute("VACUUM")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- chats ------------------------------------------------------------------------------
    def create(self, title: str = NEW_TITLE) -> Chat:
        now = time.time()
        chat = Chat(uuid.uuid4().hex[:12], title, now, now)
        with self._lock, self._db:
            self._db.execute("INSERT INTO chats(id, title, created, updated) VALUES (?,?,?,?)",
                             (chat.id, seal(chat.title), now, now))
        return chat

    def get(self, chat_id: str) -> Chat | None:
        rows = self._chats("WHERE c.id = ?", (chat_id,))
        return rows[0] if rows else None

    def list(self, query: str = "", limit: int = 200) -> list[Chat]:
        if not query.strip():
            return self._chats("", (), limit)
        q = query.strip().lower()  # contents are encrypted, so the search runs here, not in SQL
        with self._lock:
            hits = {r["chat_id"] for r in self._db.execute("SELECT chat_id, text FROM chat_messages")
                    if q in unseal(r["text"]).lower()}
        return [c for c in self._chats("", (), 10_000) if c.id in hits or q in c.title.lower()][:limit]

    def _chats(self, where: str, args: tuple[Any, ...], limit: int = 200) -> List[Chat]:
        sql = (f"SELECT c.*, (SELECT text FROM chat_messages m WHERE m.chat_id = c.id ORDER BY m.id DESC LIMIT 1)"
               f" AS preview, (SELECT COUNT(*) FROM chat_messages m WHERE m.chat_id = c.id) AS count"
               f" FROM chats c {where} ORDER BY c.pinned DESC, c.updated DESC,"
               # Windows clocks tick coarsely, so equal `updated` values happen: newest message breaks the tie
               f" (SELECT COALESCE(MAX(m.id), 0) FROM chat_messages m WHERE m.chat_id = c.id) DESC LIMIT ?")
        with self._lock:
            rows = self._db.execute(sql, (*args, limit)).fetchall()
        return [Chat(r["id"], unseal(r["title"]), r["created"], r["updated"], bool(r["pinned"]),
                     unseal(r["preview"] or "")[:120], r["count"]) for r in rows]

    def update(self, chat_id: str, title: str | None = None, pinned: bool | None = None) -> Chat | None:
        with self._lock, self._db:
            if title is not None and title.strip():
                self._db.execute("UPDATE chats SET title = ? WHERE id = ?", (seal(title.strip()[:80]), chat_id))
            if pinned is not None:
                self._db.execute("UPDATE chats SET pinned = ? WHERE id = ?", (int(pinned), chat_id))
        return self.get(chat_id)

    def delete(self, chat_id: str) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM chats WHERE id = ?", (chat_id,)).rowcount > 0

    # --- messages ---------------------------------------------------------------------------
    def add(self, chat_id: str, role: str, text: str, meta: dict[str, Any] | None = None) -> int:
        now = time.time()
        with self._lock, self._db:
            cur = self._db.execute("INSERT INTO chat_messages(chat_id, role, text, meta, ts) VALUES (?,?,?,?,?)",
                                   (chat_id, role, seal(text), json.dumps(meta or {}), now))
            self._db.execute("UPDATE chats SET updated = ? WHERE id = ?", (now, chat_id))
            if role == "user" and not small_talk(text):  # the first real request names the chat ("hi" doesn't)
                row = self._db.execute("SELECT title FROM chats WHERE id = ?", (chat_id,)).fetchone()
                if row and unseal(row["title"]) == NEW_TITLE:
                    self._db.execute("UPDATE chats SET title = ? WHERE id = ?", (seal(title_from(text)), chat_id))
        return int(cur.lastrowid or 0)

    def messages(self, chat_id: str, limit: int = 500) -> List[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM (SELECT * FROM chat_messages WHERE chat_id = ? ORDER BY id DESC"
                                    " LIMIT ?) ORDER BY id", (chat_id, limit)).fetchall()
        return [{"id": r["id"], "role": r["role"], "text": unseal(r["text"]), "meta": json.loads(r["meta"]),
                 "ts": r["ts"]} for r in rows]

    def context_for(self, chat_id: str, turns: int = 6, max_chars: int = 1600, per_message: int = 300) -> str:
        """The recent conversation as a compact recap for the next request. Newest turns win when
        the budget is tight; long messages are clipped."""
        with self._lock:
            rows = self._db.execute("SELECT role, text FROM chat_messages WHERE chat_id = ? ORDER BY id DESC"
                                    " LIMIT ?", (chat_id, turns * 2)).fetchall()
        lines: list[str] = []
        used = 0
        for r in rows:  # newest first
            text = " ".join(unseal(r["text"]).split())
            if len(text) > per_message:
                text = text[:per_message] + "…"
            line = f"{'User' if r['role'] == 'user' else 'Delta'}: {text}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(reversed(lines))
