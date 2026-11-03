"""Memories and chats are encrypted at rest with DPAPI: the database file holds no readable text."""

import sqlite3

import pytest

from delta.memory.chats import ChatStore
from delta.memory.store import MemoryStore
from delta.system import vault

pytestmark = pytest.mark.skipif(not vault.available(), reason="needs Windows DPAPI")


def test_memory_is_sealed_on_disk_and_still_searchable(tmp_path):
    db = tmp_path / "memory.db"
    m = MemoryStore(db)
    m.add("User's sister is called Priya and lives in Pune")
    m.add("User prefers short answers")
    assert [x.content for x in m.search("where does priya live")] == [
        "User's sister is called Priya and lives in Pune"]
    m.close()
    assert b"Priya" not in db.read_bytes()


def test_old_plain_rows_are_sealed_on_start(tmp_path):
    db = tmp_path / "memory.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE memories(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,"
                " source TEXT NOT NULL DEFAULT 'user', created TEXT NOT NULL, updated TEXT NOT NULL)")
    raw.execute("INSERT INTO memories(kind, content, created, updated)"
                " VALUES ('profile', 'User banks with HDFC', 'x', 'x')")
    raw.commit()
    raw.close()
    m = MemoryStore(db)
    assert [x.content for x in m.list()] == ["User banks with HDFC"]
    m.close()
    assert b"HDFC" not in db.read_bytes()


def test_chats_are_sealed_on_disk(tmp_path):
    db = tmp_path / "chats.db"
    s = ChatStore(db)
    chat = s.create()
    s.add(chat.id, "user", "plan a surprise party for Meera")
    assert s.get(chat.id).title.startswith("Plan a surprise party") or "surprise" in s.get(chat.id).title.lower()
    assert [c.id for c in s.list("meera")] == [chat.id]
    assert s.messages(chat.id)[0]["text"] == "plan a surprise party for Meera"
    s.close()
    wal = db.with_name("chats.db-wal")
    data = db.read_bytes() + (wal.read_bytes() if wal.exists() else b"")
    assert b"Meera" not in data


def test_no_plain_copy_survives_in_the_write_ahead_log(tmp_path):
    """The real-PC finding: after sealing, old plain pages sat in chats.db-wal. Opening the store must
    leave neither the main file nor the log holding the old text."""
    db = tmp_path / "chats.db"
    old = ChatStore(db)
    chat = old.create()
    old._db.execute("INSERT INTO chat_messages(chat_id, role, text, meta, ts) VALUES (?, 'user', ?, '{}', 0)",
                    (chat.id, "my secret plan for Diwali"))
    old._db.commit()  # a plain row, as written before encryption existed (lands in the WAL)
    s = ChatStore(db)  # second connection: seals it and scrubs while the first is still open
    assert any(m["text"] == "my secret plan for Diwali" for m in s.messages(chat.id))
    wal = db.with_name("chats.db-wal")
    # checked while Delta is still running: SQLite only tidies the log when the last connection closes
    data = db.read_bytes() + (wal.read_bytes() if wal.exists() else b"")
    assert b"Diwali" not in data
    old._db.close()
    s.close()
