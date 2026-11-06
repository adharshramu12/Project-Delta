"""The phone menu (delta.channels.menu): one command list for Telegram's "/" menu, /help and the typed WhatsApp
menu; old chats from the phone (/new, /chats, /open, /find), each person seeing only their own."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

from delta.channels.menu import COMMANDS, GROUPS, PhoneChats, handle_text, help_text, telegram_menu, text_command
from delta.memory.chats import ChatStore


def container(tmp_path, locked: bool = False) -> Any:
    guard = SimpleNamespace(locked=locked, locked_message=lambda: "Remote control is locked.")
    return SimpleNamespace(chats=ChatStore(tmp_path / "chats.db"), guard=guard,
                           governor=SimpleNamespace(status=lambda: {"health": "ok", "ram_pct": 40, "gpu_temp_c": 50}),
                           router=SimpleNamespace(usable=lambda: ["groq"]))


# --- the list itself -----------------------------------------------------------------------------------------
def test_menu_is_clean():
    names = [c.name for c in COMMANDS]
    assert len(names) == len(set(names)) <= 100  # Telegram allows 100
    for c in COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", c.name)
        assert 3 <= len(c.desc) <= 48 and c.desc.isascii()  # short, no emojis
        assert c.group in GROUPS
    assert [g for g in GROUPS if any(c.group == g for c in COMMANDS)] == list(GROUPS)
    order = [GROUPS.index(c.group) for c in COMMANDS]
    assert order == sorted(order)  # the "/" menu reads group by group
    assert telegram_menu() == [(c.name, c.desc) for c in COMMANDS]


def test_help_is_grouped_and_complete():
    text = help_text("telegram")
    for group in GROUPS:
        assert f"<b>{group}</b>" in text
    for c in COMMANDS:
        assert f"/{c.name}" in text
    assert text.isascii() or all(ord(ch) < 0x2000 for ch in text)  # no emojis
    assert "/browser" in text and "/signin" in text


def test_whatsapp_menu_lists_only_typed_commands():
    text = help_text("whatsapp")
    assert text.startswith("*Delta menu*") and "<b>" not in text and "/" not in text.replace("PC/", "")
    for c in COMMANDS:
        assert (f"\n{c.name}" in text) == (c.text and c.name != "help"), c.name
    assert "\nmenu - This list" in text and "*Chats*" in text and "open 2 - " in text


@pytest.mark.parametrize(("typed", "want"), [
    ("menu", ("help", "")), ("Help", ("help", "")), ("/help", ("help", "")), ("commands", ("help", "")),
    ("chats", ("chats", "")), ("open 2", ("open", "2")), ("/open a1b2c3", ("open", "a1b2c3")),
    ("find wifi code", ("find", "wifi code")), ("/find me a flight", ("find", "me a flight")),
    ("new", ("new", "")), ("stop", ("stop", "")), ("status", ("status", "")),
    ("open notepad", None), ("stop the music", None), ("find me a flight to goa", None),
    ("find the biggest files", None), ("new folder on the desktop", None), ("help me write an email", None),
    ("play ramadasu songs", None), ("", None),
])
def test_typed_commands_vs_requests(typed, want):
    assert text_command(typed) == want


# --- old chats -------------------------------------------------------------------------------------------------
def test_current_chat_adopts_the_old_telegram_chat(tmp_path):
    c = container(tmp_path)
    old = c.chats.create("Telegram 42")  # how Telegram chats were stored before owners
    c.chats.add(old.id, "user", "plan my goa trip")
    pc = c.chats.create("Telegram 42 notes")
    tg = PhoneChats(c, "telegram")
    assert tg.current("42") == old.id and c.chats.owner_of(old.id) == "telegram:42"
    assert c.chats.owner_of(pc.id) == ""
    assert tg.current("7") not in (old.id, pc.id)  # another paired user gets their own chat


def test_new_chats_list_open_find(tmp_path):
    c = container(tmp_path)
    tg = PhoneChats(c, "telegram")
    first = tg.current("42")
    c.chats.add(first, "user", "My hotel wifi code is MANGO-7781")
    c.chats.add(first, "delta", "Noted.")
    second = tg.new("42")
    assert second != first and tg.new("42") == second  # an empty new chat is reused
    c.chats.add(second, "user", "Play Seetharamam songs on YouTube")
    assert tg.current("42") == second
    text, buttons = tg.listing("42", "telegram")
    assert "1. Play Seetharamam songs on YouTube" in text and "(current)" in text
    assert "2. Telegram chat" in text and "Telegram 42" not in text  # the id never shows
    assert [cid for _label, cid in buttons] == [second, first]
    reply = tg.open("42", "2", "telegram")
    assert "Back in: Telegram chat" in reply and "MANGO-7781" in reply
    assert tg.current("42") == first  # the next message continues the old chat
    assert "Back in" in tg.open("42", second[:6], "telegram")
    text, buttons = tg.find("42", "wifi code", "telegram")
    assert "MANGO-7781" in text and buttons == [("Open: Telegram chat", first)]
    assert "Nothing about" in tg.find("42", "passport", "telegram")[0]


def test_never_someone_elses_chat(tmp_path):
    c = container(tmp_path)
    tg = PhoneChats(c, "telegram")
    theirs = tg.current("7")
    c.chats.add(theirs, "user", "my bank pin hint is BLUE-42")
    pc = c.chats.create()
    c.chats.add(pc.id, "user", "office wifi PC-ONLY-9")
    mine = tg.current("42")
    c.chats.add(mine, "user", "hello there, plan a trip")
    assert "BLUE-42" not in tg.find("42", "bank pin hint", "telegram")[0]
    assert "PC-ONLY-9" not in tg.find("42", "office wifi", "telegram")[0]
    assert "couldn't find" in tg.open("42", theirs[:6], "telegram")
    assert "couldn't find" in tg.open("42", pc.id[:6], "telegram")
    assert "couldn't find" in tg.reopen("42", theirs, "telegram")  # even a forged button
    assert tg.current("42") == mine
    assert all(cid != theirs for _l, cid in tg.listing("42", "telegram")[1])


async def test_whatsapp_text_commands(tmp_path):
    c = container(tmp_path)
    wa = PhoneChats(c, "whatsapp")
    chat = wa.current("919800000000")
    c.chats.add(chat, "user", "My hotel wifi code is MANGO-7781")
    menu = await handle_text(c, wa, "919800000000", "menu")
    assert menu and menu.startswith("*Delta menu*")
    assert "1. WhatsApp chat" in (await handle_text(c, wa, "919800000000", "chats") or "")
    assert "MANGO-7781" in (await handle_text(c, wa, "919800000000", "find wifi code") or "")
    assert "Back in" in (await handle_text(c, wa, "919800000000", "open 1") or "")
    assert await handle_text(c, wa, "919800000000", "New") == "New chat started. What should I do?"
    assert "groq" in (await handle_text(c, wa, "919800000000", "status") or "")
    assert await handle_text(c, wa, "919800000000", "open notepad") is None  # a request for the AI


async def test_locked_pc_answers_no_chat_commands(tmp_path):
    c = container(tmp_path, locked=True)
    wa = PhoneChats(c, "whatsapp")
    assert await handle_text(c, wa, "1", "chats") == "Remote control is locked."
    assert await handle_text(c, wa, "1", "find wifi") == "Remote control is locked."
