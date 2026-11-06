"""Chats: separate conversations, like Claude's sidebar. Stored on this PC in SQLite.

Each chat keeps its own messages. What a new request sees of its chat is a short, bounded recap of
the last few turns (`context_for`), never the whole history, so follow-ups ("send it to him",
"make it 5 pm instead") work while every request stays cheap. Memory, skills and approvals are
shared by all chats on purpose: what Delta learns in one chat it knows in every chat.
"""

from __future__ import annotations

import contextlib
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
from delta.system import sqlite_safe
from delta.system.vault import PREFIX, scrub, seal, seal_or_keep, unseal

# Long chats (context_for with a query): what a request sees of the turns that left the recent window. Built with
# no AI call, so it costs nothing to make and cannot invent anything: a short outline of the older requests and
# the few older lines that match the new request, quoted word for word with their turn number.
SUMMARY_HEADER = "Summary of older turns in this chat ([#n] = turn number):"
RECALL_HEADER = "Recalled from older turns (exact quotes):"
NO_GUESS = ("Older turns are known only from the lines above and your memory; the [#n] tags are for you, not the "
            "user. If something asked about is not there, say you don't have it. Never guess.")
SUMMARY_HEAD, SUMMARY_TAIL = 2, 5  # outline: the chat's first requests and its latest older ones
SUMMARY_LINE = 80
RECALL_MAX, RECALL_CHARS = 3, 240
SCAN_LIMIT = 2000  # older messages searched per request (newest first)

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL, text TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}', ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS chat_messages_chat ON chat_messages(chat_id, id);
CREATE TABLE IF NOT EXISTS chat_summaries (
    chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE, upto INTEGER NOT NULL, body TEXT NOT NULL);
"""

NEW_TITLE = "New chat"
# A Telegram conversation is stored as "Telegram <chat id>" so the bot finds it again (channels/telegram.py). The
# number is private: the dashboard (sidebar, window title) shows this instead.
TELEGRAM_TITLE = "Telegram chat"
_TELEGRAM_ID_TITLE = re.compile(r"^Telegram -?\d+$")
_FILLER = re.compile(r"^(?:please|pls|can you|could you|hey|hi|delta|ok|okay|now|just)[,\s]+", re.I)


def title_from(text: str, words: int = 7) -> str:
    """A chat title from its first message, with no AI call: "can you play seetharamam song" ->
    "Play seetharamam song"."""
    t = " ".join(text.split())
    while (s := _FILLER.sub("", t)) != t:
        t = s
    t = " ".join(t.split()[:words]).rstrip(".,!?:;")
    return (t[:1].upper() + t[1:])[:60] if t else NEW_TITLE


def shown_title(title: str) -> str:
    """The title people see: a Telegram chat's internal "Telegram 6057519957" becomes "Telegram chat"."""
    return TELEGRAM_TITLE if _TELEGRAM_ID_TITLE.match(title) else title


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
        """For the dashboard: `title` is the shown one (shown_title); the stored one stays on the object."""
        return asdict(self) | {"title": shown_title(self.title)}


class ChatStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite_safe.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)
        # Who a phone chat belongs to ("telegram:<user id>"); empty for the PC's own chats. Added to older files.
        if "owner" not in {r["name"] for r in self._db.execute("PRAGMA table_info(chats)")}:
            self._db.execute("ALTER TABLE chats ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
        self._db.execute("CREATE INDEX IF NOT EXISTS chats_owner ON chats(owner, updated)")
        self._db.commit()
        self._lock = threading.Lock()
        self._seal_old_rows()

    def _seal_old_rows(self) -> None:
        """Titles and messages are encrypted at rest (DPAPI). Rows from before that are sealed once."""
        with self._lock:
            chats = self._db.execute("SELECT id, title FROM chats WHERE title NOT LIKE ?", (PREFIX + "%",)).fetchall()
            msgs = self._db.execute("SELECT id, text FROM chat_messages WHERE text NOT LIKE ?",
                                    (PREFIX + "%",)).fetchall()
            new_chats = [(seal_or_keep(r["title"]), r["id"]) for r in chats]
            new_msgs = [(seal_or_keep(r["text"]), r["id"]) for r in msgs]
            if any(t.startswith(PREFIX) for t, _ in new_chats + new_msgs):
                with self._db:
                    self._db.executemany("UPDATE chats SET title = ? WHERE id = ?", new_chats)
                    self._db.executemany("UPDATE chat_messages SET text = ? WHERE id = ?", new_msgs)
            scrub(self._db)

    def close(self) -> None:
        with self._lock:
            scrub(self._db)
            self._db.close()

    # --- chats ------------------------------------------------------------------------------
    def create(self, title: str = NEW_TITLE, owner: str = "") -> Chat:
        now = time.time()
        chat = Chat(uuid.uuid4().hex[:12], title, now, now)
        with self._lock, self._db:
            self._db.execute("INSERT INTO chats(id, title, created, updated, owner) VALUES (?,?,?,?,?)",
                             (chat.id, seal(chat.title), now, now, owner))
        return chat

    # --- phone chats: one person's own conversations -----------------------------------------------------
    def owned(self, owner: str, limit: int = 200) -> List[Chat]:
        """The chats of one person on one channel ("telegram:42"), newest first. Never anyone else's."""
        return self._chats("WHERE c.owner = ?", (owner,), limit) if owner else []

    def owner_of(self, chat_id: str) -> str:
        with self._lock:
            row = self._db.execute("SELECT owner FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return str(row["owner"]) if row else ""

    def claim(self, chat_id: str, owner: str) -> bool:
        """Give an unowned chat (one made before owners existed) to `owner`. Never takes one someone owns."""
        with self._lock, self._db:
            return self._db.execute("UPDATE chats SET owner = ? WHERE id = ? AND owner = ''",
                                    (owner, chat_id)).rowcount > 0

    def touch(self, chat_id: str) -> None:
        """Make a chat the newest one (reopened from the phone, so the next message goes there)."""
        with self._lock, self._db:
            self._db.execute("UPDATE chats SET updated = ? WHERE id = ?", (time.time(), chat_id))

    def search(self, query: str, owner: str, limit: int = 5) -> List[dict[str, Any]]:
        """Messages in `owner`'s chats that match the words of `query`, best first, each with a short snippet,
        its chat and its turn number. Contents are encrypted, so matching runs here, never in SQL."""
        from delta.memory.keywords import keywords, overlap

        words = keywords(query) or [w for w in re.findall(r"\w{3,}", query.lower())][:12]
        if not owner or not words:
            return []
        need = _needed(words)
        with self._lock:
            rows = self._db.execute(
                "SELECT m.id, m.chat_id, m.role, m.text, m.ts FROM chat_messages m JOIN chats c ON c.id = m.chat_id"
                " WHERE c.owner = ? ORDER BY m.id DESC LIMIT ?", (owner, SCAN_LIMIT * 5)).fetchall()
        hits = []
        for r in rows:
            text = " ".join(unseal(r["text"]).split())
            if (score := overlap(words, text)) >= need:
                hits.append((score, r, text))
        hits.sort(key=lambda h: (-h[0], h[1]["role"] != "user", -h[1]["id"]))  # the user's own words first
        out: List[dict[str, Any]] = []
        for _score, r, text in hits[:limit]:
            out.append({"chat_id": r["chat_id"], "role": r["role"], "ts": r["ts"],
                        "turn": self._turn(r["chat_id"], r["id"]), "snippet": snippet(text, words, 160)})
        return out

    def _turn(self, chat_id: str, message_id: int) -> int:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) AS n FROM chat_messages WHERE chat_id = ? AND id <= ?",
                                   (chat_id, message_id)).fetchone()
        return int(row["n"])

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

    def context_for(self, chat_id: str, turns: int = 6, max_chars: int = 1600, per_message: int = 300,
                    query: str | None = None) -> str:
        """The recent conversation as a compact recap for the next request. Newest turns win when
        the budget is tight; long messages are clipped.

        With `query` (the new request) and a chat longer than the recent window, the turns that left the window
        are not simply lost: a short rolling outline of them comes first, then the few older lines that match the
        request, quoted word for word with their turn number, then a line telling the model that what is not
        shown is unknown, so it says so instead of guessing. No AI call, a few hundred characters at most."""
        with self._lock:
            rows = self._db.execute("SELECT id, role, text FROM chat_messages WHERE chat_id = ? ORDER BY id DESC"
                                    " LIMIT ?", (chat_id, turns * 2)).fetchall()
        lines: list[str] = []
        used = 0
        oldest_recent = 0  # the oldest message the recap shows: everything before it is "older"
        for r in rows:  # newest first
            text = " ".join(unseal(r["text"]).split())
            if len(text) > per_message:
                text = text[:per_message] + "…"
            line = f"{'User' if r['role'] == 'user' else 'Delta'}: {text}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
            oldest_recent = r["id"]
        recent = "\n".join(reversed(lines))
        if query is None or not oldest_recent:
            return recent
        with self._lock:
            older = self._db.execute("SELECT COUNT(*) AS n FROM chat_messages WHERE chat_id = ? AND id < ?",
                                     (chat_id, oldest_recent)).fetchone()["n"]
        if not older:
            return recent
        parts = [self._summary(chat_id, oldest_recent)]
        if recalled := self._recall(chat_id, oldest_recent, older, query):
            parts.append(RECALL_HEADER + "\n" + "\n".join(recalled))
        parts.append(NO_GUESS)
        return "\n\n".join(p for p in parts if p) + "\n\nRecent turns:\n" + recent

    def summary_upto(self, chat_id: str) -> int:
        """The last message id the rolling summary covers (0: none yet)."""
        with self._lock:
            row = self._db.execute("SELECT upto FROM chat_summaries WHERE chat_id = ?", (chat_id,)).fetchone()
        return int(row["upto"]) if row else 0

    def _summary(self, chat_id: str, before_id: int) -> str:
        """The outline of the older turns: the chat's first requests and its latest older ones, each with its
        turn number; the rest counted. Kept in the database and only extended with turns that newly left the
        recent window, so a long chat is not re-read for every request."""
        with self._lock:
            row = self._db.execute("SELECT upto, body FROM chat_summaries WHERE chat_id = ?", (chat_id,)).fetchone()
        state: dict[str, Any] = {"head": [], "tail": [], "skipped": 0}
        upto = 0
        if row:
            with contextlib.suppress(ValueError, TypeError):
                state = json.loads(unseal(row["body"]))
                upto = int(row["upto"])
        if before_id - 1 > upto:
            with self._lock:
                new = self._db.execute("SELECT id, role, text FROM chat_messages WHERE chat_id = ? AND id > ?"
                                       " AND id < ? ORDER BY id", (chat_id, upto, before_id)).fetchall()
                base = self._db.execute("SELECT COUNT(*) AS n FROM chat_messages WHERE chat_id = ? AND id <= ?",
                                        (chat_id, upto)).fetchone()["n"]
            for i, r in enumerate(new, start=base + 1):
                if r["role"] != "user":
                    continue
                text = " ".join(unseal(r["text"]).split())
                if small_talk(text):
                    continue
                item = [i, text[:SUMMARY_LINE] + ("…" if len(text) > SUMMARY_LINE else "")]
                if len(state["head"]) < SUMMARY_HEAD:
                    state["head"].append(item)
                    continue
                state["tail"].append(item)
                if len(state["tail"]) > SUMMARY_TAIL:
                    state["tail"].pop(0)
                    state["skipped"] += 1
            if new:
                upto = int(new[-1]["id"])
                with self._lock, self._db:
                    self._db.execute("INSERT INTO chat_summaries(chat_id, upto, body) VALUES (?,?,?) ON CONFLICT"
                                     "(chat_id) DO UPDATE SET upto = excluded.upto, body = excluded.body",
                                     (chat_id, upto, seal(json.dumps(state))))
        items = [f"- [#{n}] {t}" for n, t in state["head"]]
        if state["skipped"]:
            items.append(f"- ({state['skipped']} more turns)")
        items += [f"- [#{n}] {t}" for n, t in state["tail"]]
        return SUMMARY_HEADER + "\n" + "\n".join(items) if items else ""

    def _recall(self, chat_id: str, before_id: int, older: int, query: str) -> List[str]:
        """The older lines that match the request, word for word, best first, each tagged [#turn Role]."""
        from delta.memory.keywords import keywords, overlap

        words = keywords(query)
        if not words:
            return []
        need = _needed(words)
        with self._lock:
            rows = self._db.execute("SELECT id, role, text FROM chat_messages WHERE chat_id = ? AND id < ?"
                                    " ORDER BY id DESC LIMIT ?", (chat_id, before_id, SCAN_LIMIT)).fetchall()
        hits = []
        for pos, r in enumerate(rows):  # newest first: turn = older - pos
            text = " ".join(unseal(r["text"]).split())
            if (score := overlap(words, text)) >= need:
                hits.append((score, older - pos, r["role"], text))
        hits.sort(key=lambda h: (-h[0], -h[1]))
        return [f"[#{turn} {'User' if role == 'user' else 'Delta'}] {snippet(text, words, RECALL_CHARS)}"
                for _s, turn, role, text in sorted(hits[:RECALL_MAX], key=lambda h: h[1])]


def _needed(words: list[str]) -> int:
    """How many of the request's content words a line must share to count as about it: one for a short request,
    two once it has three or more (so one common word alone does not pull in unrelated lines)."""
    return 2 if len(words) >= 3 else 1


def snippet(text: str, words: list[str], width: int) -> str:
    """`text` cut to `width` characters around the first word of `words` it contains."""
    if len(text) <= width:
        return text
    low = text.lower()
    at = min((i for w in words if (i := low.find(w[:5])) >= 0), default=0)
    start = max(0, min(at - width // 3, len(text) - width))
    cut = text[start:start + width].strip()
    return ("…" if start else "") + cut + ("…" if start + width < len(text) else "")
