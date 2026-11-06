"""Old chats from the phone and long chats without guessing (delta.memory.chats).

- Each phone chat belongs to one person on one channel (`owner`, e.g. "telegram:42"): listing, opening and
  searching only ever see that person's chats, never the dashboard's or another user's.
- A long chat keeps a short rolling summary of its older turns and, for each new request, quotes only the older
  lines that match it, each tagged with its turn number. When nothing matches, the recap says so and tells the
  model to say it does not have it, so it never fills the gap with a guess.
"""

import sqlite3

from delta.memory.chats import (
    NO_GUESS,
    RECALL_HEADER,
    SUMMARY_HEADER,
    ChatStore,
)


def long_chat(s: ChatStore, owner: str = "", turns: int = 40, early: tuple[str, str] | None = None) -> str:
    c = s.create(owner=owner)
    if early:
        s.add(c.id, "user", early[0])
        s.add(c.id, "delta", early[1])
    for i in range(turns):
        s.add(c.id, "user", f"Open notepad and type shopping item {i}: tomatoes, onions and rice " + "y" * 200)
        s.add(c.id, "delta", f"Done, typed item {i} in Notepad.")
    return c.id


# --- owners: whose chats a phone user can see -------------------------------------------------------------
def test_owned_chats_are_per_person(tmp_path):
    s = ChatStore(tmp_path / "c.db")
    mine = s.create(owner="telegram:42")
    other = s.create(owner="telegram:7")
    pc = s.create()  # a dashboard chat: nobody's on the phone
    for c in (mine, other, pc):
        s.add(c.id, "user", "plan the goa trip")
    assert [c.id for c in s.owned("telegram:42")] == [mine.id]
    assert s.owner_of(mine.id) == "telegram:42" and s.owner_of(pc.id) == ""
    assert {c.id for c in s.list()} == {mine.id, other.id, pc.id}  # the dashboard still sees every chat
    assert s.owned("") == []  # an empty owner never means "everything"


def test_owner_column_added_to_an_old_database(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
            pinned INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, role TEXT NOT NULL,
            text TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}', ts REAL NOT NULL);
        INSERT INTO chats VALUES ('abc123def456', 'Telegram 42', 1, 1, 0);
    """)
    db.commit()
    db.close()
    s = ChatStore(path)
    assert s.get("abc123def456").title == "Telegram 42" and s.owner_of("abc123def456") == ""
    assert s.claim("abc123def456", "telegram:42")
    assert [c.id for c in s.owned("telegram:42")] == ["abc123def456"]
    assert not s.claim("abc123def456", "telegram:7")  # an owned chat is never handed to someone else
    s.close()
    assert [c.id for c in ChatStore(path).owned("telegram:42")] == ["abc123def456"]  # survives a restart


def test_touch_makes_a_chat_the_newest(tmp_path):
    s = ChatStore(tmp_path / "c.db")
    a = s.create(owner="telegram:42")
    s.add(a.id, "user", "first chat")
    b = s.create(owner="telegram:42")
    s.add(b.id, "user", "second chat")
    assert s.owned("telegram:42")[0].id == b.id
    s.touch(a.id)
    assert s.owned("telegram:42")[0].id == a.id


def test_search_returns_snippets_from_own_chats_only(tmp_path):
    s = ChatStore(tmp_path / "c.db")
    mine = s.create(owner="telegram:42")
    s.add(mine.id, "user", "My hotel wifi code is MANGO-7781, keep it handy please")
    s.add(mine.id, "delta", "Noted the hotel wifi code.")
    theirs = s.create(owner="telegram:7")
    s.add(theirs.id, "user", "their wifi code is SECRET-1")
    pc = s.create()
    s.add(pc.id, "user", "office wifi code is PC-ONLY-9")
    hits = s.search("wifi code", owner="telegram:42")
    assert hits and all(h["chat_id"] == mine.id for h in hits)
    assert "MANGO-7781" in hits[0]["snippet"] and hits[0]["turn"] == 1
    text = str(hits)
    assert "SECRET-1" not in text and "PC-ONLY-9" not in text
    assert s.search("wifi", owner="") == []  # no owner, no search
    assert s.search("   ", owner="telegram:42") == []
    assert s.search("passport", owner="telegram:42") == []


# --- long chats: summary + recall, never a guess ----------------------------------------------------------
def test_short_chat_recap_is_unchanged(tmp_path):
    s = ChatStore(tmp_path / "c.db")
    c = s.create()
    s.add(c.id, "user", "write a note for Ravi")
    s.add(c.id, "delta", "Done.")
    want = "User: write a note for Ravi\nDelta: Done."
    assert s.context_for(c.id, query="send it to him") == s.context_for(c.id) == want


def test_early_fact_is_recalled_with_its_turn(tmp_path):
    s = ChatStore(tmp_path / "c.db")
    cid = long_chat(s, early=("My hotel wifi code is MANGO-7781, keep it handy", "Noted."))
    plain = s.context_for(cid)
    assert "MANGO-7781" not in plain  # the recent window alone has lost it
    ctx = s.context_for(cid, query="What was the hotel wifi code I gave you?")
    assert RECALL_HEADER in ctx and "[#1 User] My hotel wifi code is MANGO-7781" in ctx
    assert SUMMARY_HEADER in ctx and NO_GUESS in ctx
    assert len(ctx) <= len(plain) + 1400  # bounded: summary + a few quotes, never the whole chat
    assert ctx.rstrip().endswith("Delta: Done, typed item 39 in Notepad.")  # newest turns still last


def test_never_given_fact_is_not_invented(tmp_path):
    s = ChatStore(tmp_path / "c.db")
    cid = long_chat(s)
    ctx = s.context_for(cid, query="What is my locker number?")
    assert RECALL_HEADER not in ctx  # nothing matched: nothing is quoted
    assert NO_GUESS in ctx  # and the model is told to say it does not have it
    assert "locker" not in ctx.lower().replace("what is my locker number", "")


def test_rolling_summary_is_bounded_and_incremental(tmp_path):
    s = ChatStore(tmp_path / "c.db")
    cid = long_chat(s, turns=60, early=("Plan my Goa trip for December", "Sure."))
    first = s.context_for(cid, query="anything")
    summary = first.split(SUMMARY_HEADER, 1)[1].split("\n\n", 1)[0]
    lines = [x for x in summary.splitlines() if x.startswith("- [#")]
    assert 2 <= len(lines) <= 8 and lines[0].startswith("- [#1] Plan my Goa trip")  # the chat's start stays
    assert "more turns" in summary  # the middle is counted, not listed
    upto = s.summary_upto(cid)
    s.add(cid, "user", "one more request about the trip budget")
    s.add(cid, "delta", "Okay.")
    s.context_for(cid, query="budget")
    assert s.summary_upto(cid) > upto  # extended with only the turns that left the window
    s.delete(cid)
    assert s.summary_upto(cid) == 0  # deleted with its chat
