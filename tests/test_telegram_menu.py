"""Telegram: the "/" menu matches the bot's handlers both ways, is set for private chats only when it changed, and
/new, /chats, /open, /find and typed "menu" work for the paired user only, never while remote control is locked."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("telegram")

from telegram.ext import CallbackQueryHandler, CommandHandler

from delta.channels import telegram_menu as tm
from delta.channels.menu import COMMANDS, telegram_menu
from delta.channels.telegram import HELP, TelegramChannel
from delta.core.state import TaskResult
from delta.memory.chats import NO_GUESS, RECALL_HEADER, ChatStore


class Chat:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_action(self, action: str) -> None:
        return None

    async def send_message(self, text: str, **kw: Any) -> None:
        self.sent.append(text)


class Message:
    def __init__(self, text: str = "") -> None:
        self.text, self.chat, self.replies, self.markups = text, Chat(), [], []

    async def reply_text(self, text: str, **kw: Any) -> None:
        self.replies.append(text)
        self.markups.append(kw.get("reply_markup"))


def make(tmp_path, locked: bool = False) -> tuple[TelegramChannel, list[Any]]:
    submitted: list[Any] = []
    guard = SimpleNamespace(locked=locked, locked_message=lambda: "Remote control is locked.")

    async def submit(req: Any) -> TaskResult:
        submitted.append(req)
        return TaskResult(req.id, "done", "Okay.")

    audit = SimpleNamespace(record=lambda *a, **k: None)
    c = SimpleNamespace(audit=audit, settings=SimpleNamespace(agent=SimpleNamespace(name="Delta")),
                        orchestrator=SimpleNamespace(attachments=None), chats=ChatStore(tmp_path / "chats.db"),
                        guard=guard, submit=submit)
    return TelegramChannel(c, "123456:TEST-TOKEN", ["42"]), submitted  # type: ignore[arg-type]


def update(uid: int, text: str = "") -> SimpleNamespace:
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid), message=Message(text), callback_query=None)


def ctx(*args: str) -> SimpleNamespace:
    return SimpleNamespace(args=list(args))


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    import delta.system.notify as notify

    monkeypatch.setattr(notify, "notify", lambda *a, **k: None)


def handlers(ch: TelegramChannel) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for group in ch.app.handlers.values():
        for h in group:
            if isinstance(h, CommandHandler):
                out.update({name: h for name in h.commands})
    return out


def test_menu_lists_exactly_what_the_bot_handles(tmp_path):
    ch, _ = make(tmp_path)
    registered = set(handlers(ch))
    listed = {c.name for c in COMMANDS}
    assert listed == registered, (listed - registered, registered - listed)
    assert HELP.count("\n/") == len(COMMANDS)
    patterns = [h.pattern.pattern for g in ch.app.handlers.values() for h in g if isinstance(h, CallbackQueryHandler)]
    assert tm.PATTERN in patterns


class FakeBot:
    def __init__(self, lists: dict[str, list[tuple[str, str]]] | None = None, fail: bool = False) -> None:
        self.lists = dict(lists or {})
        self.writes: list[tuple[str, str]] = []
        self.fail = fail
        self.button = SimpleNamespace(type="commands")

    @staticmethod
    def key(scope: Any) -> str:
        return f"{scope.type}:{getattr(scope, 'chat_id', '')}"

    async def get_my_commands(self, scope: Any = None) -> list[Any]:
        if self.fail:
            raise RuntimeError("network down")
        return [SimpleNamespace(command=n, description=d) for n, d in self.lists.get(self.key(scope), [])]

    async def set_my_commands(self, commands: list[Any], scope: Any = None) -> None:
        self.lists[self.key(scope)] = [(c.command, c.description) for c in commands]
        self.writes.append(("set", self.key(scope)))

    async def delete_my_commands(self, scope: Any = None) -> None:
        self.lists.pop(self.key(scope), None)
        self.writes.append(("delete", self.key(scope)))

    async def get_chat_menu_button(self) -> Any:
        return self.button

    async def set_chat_menu_button(self, menu_button: Any = None) -> None:
        self.button = menu_button
        self.writes.append(("button", menu_button.type))


async def test_sync_sets_private_menu_once_and_clears_stale_lists():
    old = [("ai", "See or switch the main AI"), ("help", "What I can do")]
    bot = FakeBot({"default:": old, "chat:42": old})
    bot.button = SimpleNamespace(type="web_app")
    assert await tm.sync_menu(bot, ["42"]) is True
    assert bot.lists == {"all_private_chats:": telegram_menu()}
    assert ("set", "all_private_chats:") in bot.writes and ("delete", "default:") in bot.writes
    assert ("delete", "chat:42") in bot.writes and ("button", "commands") in bot.writes
    bot.writes.clear()
    assert await tm.sync_menu(bot, ["42"]) is False and bot.writes == []  # idempotent: nothing rewritten


async def test_sync_failure_is_logged_not_raised():
    assert await tm.sync_menu(FakeBot(fail=True), ["42"]) is False


async def test_new_chats_open_find(tmp_path):
    ch, submitted = make(tmp_path)
    await ch._on_message(update(42, "My hotel wifi code is MANGO-7781"), None)
    first = submitted[-1].chat_id
    u = update(42)
    await tm.new_cmd(ch, u)
    assert u.message.replies == ["New chat started. What should I do?"]
    await ch._on_message(update(42, "play seetharamam songs"), None)
    assert submitted[-1].chat_id != first
    u = update(42)
    await tm.chats_cmd(ch, u)
    assert "1. Play seetharamam songs" in u.message.replies[0] and "2. Telegram chat" in u.message.replies[0]
    assert u.message.markups[0] is not None  # one Open button per chat
    u = update(42)
    await tm.find_cmd(ch, u, "wifi code")
    assert "MANGO-7781" in u.message.replies[0]
    u = update(42)
    await tm.open_cmd(ch, u, "2")
    assert "Back in: Telegram chat" in u.message.replies[0]
    await ch._on_message(update(42, "what was that code again?"), None)
    assert submitted[-1].chat_id == first  # continued in the reopened chat


async def test_open_button_cannot_reach_another_users_chat(tmp_path):
    ch, _ = make(tmp_path)
    theirs = ch.c.chats.create("Telegram 7", owner="telegram:7")
    ch.c.chats.add(theirs.id, "user", "private stuff")
    q = SimpleNamespace(data=f"cht:o:{theirs.id}", from_user=SimpleNamespace(id=42), message=Message())

    async def answer(*a: Any, **k: Any) -> None:
        return None

    q.answer = answer
    await tm.on_button(ch, SimpleNamespace(callback_query=q))
    assert "couldn't find" in q.message.chat.sent[0] and "private stuff" not in q.message.chat.sent[0]


async def test_strangers_and_a_locked_pc_get_nothing(tmp_path):
    ch, _ = make(tmp_path)
    ch.c.chats.add(ch.phone_chats.current("42"), "user", "wifi code MANGO-7781")
    u = update(99)
    await tm.find_cmd(ch, u, "wifi code")
    assert u.message.replies == []
    locked, _ = make(tmp_path / "l", locked=True)
    u = update(42)
    await tm.chats_cmd(locked, u)
    assert u.message.replies == ["Remote control is locked."]


async def test_typed_menu_needs_no_ai(tmp_path):
    ch, submitted = make(tmp_path)
    u = update(42, "menu")
    await ch._on_message(u, None)
    assert submitted == [] and u.message.replies == [HELP] and u.message.markups[0] is not None


async def test_long_chat_request_carries_recall(tmp_path):
    ch, submitted = make(tmp_path)
    await ch._on_message(update(42, "My hotel wifi code is MANGO-7781"), None)
    for i in range(10):
        await ch._on_message(update(42, f"open notepad and type shopping item {i}"), None)
    await ch._on_message(update(42, "what was the hotel wifi code?"), None)
    history = submitted[-1].history
    assert RECALL_HEADER in history and "MANGO-7781" in history and NO_GUESS in history
