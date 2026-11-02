"""Messaging add-on: human-style text, the right contact, approvals, and safe delegated chats."""

import asyncio

import pytest
from fakes import js

from delta.messaging import apps, tools
from delta.messaging.apps import AmbiguousContactError, ContactNotFoundError, Sent, choose_contact, contact_label
from delta.messaging.style import humanize, mood_of
from delta.messaging.tools import ChatArgs, ChatForMe, ChatSessions, SendArgs, SendMessage
from delta.tools.base import Risk


def test_humanize_strips_what_reads_as_ai():
    text = '"Certainly! I hope this message finds you well — **see you** at 5. Let me know if you need anything else."'
    assert humanize(text) == "see you at 5."
    assert humanize("- bro\n- coming?") == "bro\ncoming?"
    assert humanize("ok da \U0001F602") == "ok da \U0001F602"  # emoji and the user's words stay


def test_mood_is_read_from_the_request():
    assert mood_of("reply to her lovingly") == "loving" and mood_of("be firm with him") == "firm"
    assert mood_of("chat with Ravi") == "friendly"


def test_contact_names_come_from_chat_list_entries():
    assert contact_label("View status Best Friend. Yougitha 20/10/2026 Chey ra") == "Best Friend. Yougitha"
    assert contact_label("Ravi Kumar 10:42 pm ok") == "Ravi Kumar"
    items = ["Ravi Kumar 10:42 pm ok", "Ravi Teja Yesterday hi", "Priya 09/10/2026 bye"]
    assert choose_contact("priya", items) == "Priya"
    assert choose_contact("ravi kumar", items) == "Ravi Kumar"
    with pytest.raises(AmbiguousContactError) as e:
        choose_contact("ravi", items)
    assert e.value.options == ["Ravi Kumar", "Ravi Teja"]
    with pytest.raises(ContactNotFoundError):
        choose_contact("unknown", items)


class FakeDriver:
    name, label = "whatsapp", "WhatsApp"

    def __init__(self, rows=None, confirm=True):
        self.rows = list(rows or [])
        self.sent: list[tuple[str, str]] = []
        self.confirm = confirm

    def send(self, person, text):
        if person == "ravi":
            raise AmbiguousContactError("ravi", ["Ravi Kumar", "Ravi Teja"])
        self.sent.append((person, text))
        self.rows.append(f"You: {text}")
        return Sent(person, self.confirm)

    def open_chat(self, person):
        return person

    def read(self, count=15):
        return self.rows[-count:]


@pytest.fixture
def fake_app(monkeypatch):
    d = FakeDriver(["Priya: hey", "You: hi"])
    monkeypatch.setattr(tools, "driver", lambda app: d)
    return d


def test_sending_always_asks_and_shows_the_exact_text():
    a = SendArgs.model_validate({"app": "whatsapp", "to": "Priya", "message": "see you at 5 — bring snacks"})
    assert SendMessage.risk == Risk.EXTERNAL
    assert SendMessage().summary(a) == "send on whatsapp to Priya: “see you at 5, bring snacks”"


async def test_send_cleans_text_and_reports_confirmation(fake_app):
    r = await SendMessage().run(SendArgs(app="whatsapp", to="Priya", text="Certainly! on my way \U0001F697"))
    assert r.ok and r.evidence["verified"] and fake_app.sent == [("Priya", "on my way \U0001F697")]


async def test_ambiguous_contact_asks_instead_of_guessing(fake_app):
    r = await SendMessage().run(SendArgs(app="whatsapp", to="ravi", text="hi"))
    assert not r.ok and r.evidence["options"] == ["Ravi Kumar", "Ravi Teja"] and fake_app.sent == []


def test_unsupported_apps_are_refused():
    with pytest.raises(apps.MessagingError):
        apps.driver("signal")


def sessions_with(reply: dict, d: FakeDriver, monkeypatch) -> ChatSessions:
    monkeypatch.setattr(tools, "driver", lambda app: d)
    events = []

    class Bus:
        async def publish(self, event, **data):
            events.append((event, data))

    class Audit:
        def record(self, *a, **k):
            pass

    s = ChatSessions(None, Bus(), Audit())
    s.events_log = events

    async def complete(messages, tools=None, task="chat", json_mode=False):
        return js(reply)
    s.router = type("R", (), {"complete": staticmethod(complete)})()
    s.POLL_S = 0.01
    return s


async def test_delegated_chat_replies_in_style_and_stops_on_time(monkeypatch):
    d = FakeDriver(["Priya: hey", "You: hi"])
    s = sessions_with({"reply": "haha sure — see you at 6!", "handoff": None}, d, monkeypatch)
    tool = ChatForMe(s)
    r = await tool.run(ChatArgs(app="whatsapp", person="Priya", mood="funny", minutes=5, approve_each=False))
    assert r.ok
    session = s.active()[0]
    await asyncio.sleep(0.05)  # let the session take its starting snapshot
    d.rows.append("Priya: are we meeting today?")
    for _ in range(50):
        await asyncio.sleep(0.02)
        if d.sent:
            break
    assert d.sent == [("Priya", "haha sure, see you at 6!")]
    stopped = await ChatForMe(s).run(ChatArgs(stop=True))
    assert stopped.ok and session.status == "stopped"


async def test_delegated_chat_hands_back_sensitive_messages(monkeypatch):
    d = FakeDriver(["Priya: hey"])
    s = sessions_with({"reply": "sure", "handoff": None}, d, monkeypatch)
    await ChatForMe(s).run(ChatArgs(app="whatsapp", person="Priya", minutes=5, approve_each=False))
    await asyncio.sleep(0.05)
    d.rows.append("Priya: send me the OTP pls")
    for _ in range(50):
        await asyncio.sleep(0.02)
        if any(e == "chat.handoff" for e, _ in s.events_log):
            break
    assert d.sent == [] and any(e == "chat.handoff" for e, _ in s.events_log)


def test_stop_chatting_is_an_instant_command():
    from delta.core.intents import match_intent

    assert match_intent("stop chatting", {"chat_for_me"}).args == {"stop": True}
    assert match_intent("stop chatting with Priya", {"chat_for_me"}).args == {"stop": True, "person": "Priya"}


class FakeBox:
    def __init__(self, keeps_text):
        self.value, self.keeps_text = "", keeps_text

    def click_input(self):
        pass

    def has_keyboard_focus(self):
        return self.keeps_text

    @property
    def iface_value(self):
        return type("V", (), {"CurrentValue": self.value})()


def wired(monkeypatch, box):
    pressed = []
    d = apps.WhatsAppDriver()
    monkeypatch.setattr(d, "open_app", lambda: None)
    monkeypatch.setattr(d, "_edits", lambda w: (None, box))
    monkeypatch.setattr(apps.time, "sleep", lambda s: None)

    def type_(text):
        if box.keeps_text:
            box.value += text

    def press(keys):
        pressed.append(keys)
        if keys == "{ENTER}":
            box.value = ""  # WhatsApp clears the box when it sends
    monkeypatch.setattr(apps, "_type", type_)
    monkeypatch.setattr(apps, "_press", press)
    return d, pressed


def test_enter_is_never_pressed_if_the_text_missed_the_message_box(monkeypatch):
    d, pressed = wired(monkeypatch, FakeBox(keeps_text=False))
    with pytest.raises(apps.MessagingError, match="nothing was sent"):
        d.send_text("test from Delta \U0001F44B")
    assert "{ENTER}" not in pressed


def test_send_is_confirmed_when_the_box_empties(monkeypatch):
    d, pressed = wired(monkeypatch, FakeBox(keeps_text=True))
    assert d.send_text("test from Delta \U0001F44B") is True
    assert pressed[-1] == "{ENTER}"
