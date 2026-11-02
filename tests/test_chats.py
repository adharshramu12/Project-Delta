from delta.core.state import TaskRequest
from delta.memory.chats import NEW_TITLE, ChatStore, title_from


def test_title_from_first_message():
    title = title_from("can you please play seetharamam song on youtube for me now")
    assert title == "Play seetharamam song on youtube for me"
    assert title_from("   ") == NEW_TITLE
    assert title_from("Schedule a meeting with Ravi tomorrow.") == "Schedule a meeting with Ravi tomorrow"


def test_chats_are_separate_and_titled(tmp_path):
    s = ChatStore(tmp_path / "c.db")
    a, b = s.create(), s.create()
    s.add(a.id, "user", "Schedule a meeting with Ravi at 5 pm")
    s.add(a.id, "delta", "Done, it's on your calendar.", {"status": "done"})
    s.add(b.id, "user", "Play Seetharamam on YouTube")
    assert s.get(a.id).title == "Schedule a meeting with Ravi at 5"
    assert [m["role"] for m in s.messages(a.id)] == ["user", "delta"]
    assert s.messages(a.id)[1]["meta"] == {"status": "done"}
    assert len(s.messages(b.id)) == 1
    assert {c.id for c in s.list("ravi")} == {a.id}  # search finds titles and message text
    s.add(a.id, "user", "rename should not happen again")
    assert s.get(a.id).title.startswith("Schedule")


def test_pin_rename_delete(tmp_path):
    s = ChatStore(tmp_path / "c.db")
    a, b = s.create(), s.create()
    s.add(a.id, "user", "first")
    s.add(b.id, "user", "second")  # b is newer
    assert s.list()[0].id == b.id
    s.update(a.id, pinned=True)
    assert s.list()[0].id == a.id  # pinned chats come first
    assert s.update(a.id, title="  Work  ").title == "Work"
    assert s.delete(a.id) and s.get(a.id) is None and s.messages(a.id) == []


def test_context_is_short_and_newest_first(tmp_path):
    s = ChatStore(tmp_path / "c.db")
    c = s.create()
    for i in range(20):
        s.add(c.id, "user", f"message {i} " + "x" * 400)
        s.add(c.id, "delta", f"answer {i}")
    ctx = s.context_for(c.id)
    assert len(ctx) <= 1600
    assert ctx.endswith("Delta: answer 19") and "message 0 " not in ctx
    assert "…" in ctx  # long messages are clipped


def test_request_carries_chat_context():
    assert TaskRequest("send it to him").with_context == "send it to him"
    r = TaskRequest("send it to him", history="User: write a note for Ravi\nDelta: Done.")
    assert r.with_context.endswith("Current request: send it to him") and "Ravi" in r.with_context


def test_greeting_does_not_name_the_chat(tmp_path):
    s = ChatStore(tmp_path / "c.db")
    c = s.create()
    s.add(c.id, "user", "hi")
    assert s.get(c.id).title == NEW_TITLE
    s.add(c.id, "user", "open notepad and write a shopping list")
    assert s.get(c.id).title == "Open notepad and write a shopping list"
