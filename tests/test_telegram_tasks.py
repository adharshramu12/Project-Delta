"""Telegram: interrupted tasks are announced once, Resume needs the PIN and asks about a cut-off step."""

import pytest
from engine_helpers import FakeContainer, Rig, no_registry

from delta.channels import telegram_tasks
from delta.core.state import TaskRequest, TaskResult
from delta.engine.states import TaskState


@pytest.fixture(autouse=True)
def _folders(tmp_path, monkeypatch):
    no_registry(monkeypatch, tmp_path)


class Guard:
    def __init__(self, unlocked):
        self.locked, self.has_pin, self.unlocked = False, True, unlocked

    def is_unlocked(self, user):
        return self.unlocked

    def locked_message(self):
        return "locked"


class Chat:
    def __init__(self):
        self.sent = []

    async def send_message(self, text, **kw):
        self.sent.append((text, kw.get("reply_markup")))


class Message:
    def __init__(self):
        self.chat = Chat()


class Query:
    def __init__(self, data, uid=42):
        self.data, self.message = data, Message()
        self.from_user = type("U", (), {"id": uid})()

    async def answer(self):
        pass

    async def edit_message_reply_markup(self, reply_markup=None):
        pass


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class Channel:
    name = "telegram"

    def __init__(self, c):
        self.c, self.allowed = c, ["42"]
        self.app = type("App", (), {"bot": Bot()})()
        self.replies = []

    @staticmethod
    def _buttons(rows):
        return rows

    async def _locked(self, say):
        return False

    async def _send(self, message, text, kb=None):
        self.replies.append(text)


def setup(tmp_path, unlocked=True, finished=True):
    rig = Rig(tmp_path, {})
    c = FakeContainer(rig, Guard(unlocked))
    c.live_channels = {}
    rig.engine.begin(TaskRequest("send the report", channel="telegram", user="42", id="tg1"), "ask-risky")
    args = {"path": str(tmp_path / "r.txt"), "content": "x", "append": False}
    step = rig.engine.step_started("tg1", "write_file", args, rig.registry.get("write_file"))
    if finished:
        from delta.tools.base import ToolResult

        rig.engine.step_finished("tg1", step, "write_file", args, ToolResult(True, "ok"), 0.1)
    rig.engine.recover()
    submitted = []

    async def submit(req, autonomy_cap=None):
        submitted.append((req, autonomy_cap))
        return TaskResult(req.id, "done", "Finished the report.")

    c.submit = submit
    ch = Channel(c)
    c.live_channels["telegram"] = ch
    return rig, c, ch, submitted


async def test_announced_once_to_the_owner(tmp_path):
    _rig, c, ch, _ = setup(tmp_path)
    assert await telegram_tasks.announce_interrupted(c) == 1
    assert ch.app.bot.sent[0][0] == 42 and "interrupted" in ch.app.bot.sent[0][1]
    assert await telegram_tasks.announce_interrupted(c) == 0


async def test_resume_needs_the_pin(tmp_path):
    rig, _c, ch, submitted = setup(tmp_path, unlocked=False)
    q = Query("tsk:r:tg1")
    await telegram_tasks.on_button(ch, type("Update", (), {"callback_query": q})())
    assert "/unlock" in q.message.chat.sent[-1][0] and not submitted
    assert rig.store.get("tg1")["state"] == TaskState.INTERRUPTED


async def test_resume_with_pin_runs_capped(tmp_path):
    _rig, _c, ch, submitted = setup(tmp_path)
    await telegram_tasks.on_button(ch, type("Update", (), {"callback_query": Query("tsk:r:tg1")})())
    assert submitted and submitted[0][0].id == "tg1" and submitted[0][1] == "ask-risky"
    assert submitted[0][0].channel == "telegram" and ch.replies


async def test_cut_off_step_is_asked_first(tmp_path):
    _rig, _c, ch, submitted = setup(tmp_path, finished=False)
    q = Query("tsk:r:tg1")
    await telegram_tasks.on_button(ch, type("Update", (), {"callback_query": q})())
    text, buttons = q.message.chat.sent[-1]
    assert "may or may not have happened" in text and [b[0][1] for b in buttons] == ["tsk:y:tg1", "tsk:n:tg1"]
    assert not submitted
    await telegram_tasks.on_button(ch, type("Update", (), {"callback_query": Query("tsk:y:tg1")})())
    assert submitted


async def test_discard_and_strangers(tmp_path):
    rig, _c, ch, submitted = setup(tmp_path)
    await telegram_tasks.on_button(ch, type("Update", (), {"callback_query": Query("tsk:d:tg1", uid=7)})())
    assert rig.store.get("tg1")["state"] == TaskState.INTERRUPTED  # not an allowed user: nothing happens
    await telegram_tasks.on_button(ch, type("Update", (), {"callback_query": Query("tsk:d:tg1")})())
    assert rig.store.get("tg1")["state"] == TaskState.CANCELLED and not submitted
