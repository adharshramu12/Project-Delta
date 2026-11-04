"""Emergency help: contacts stored encrypted, the alert always asks first (even in autopilot), messages
and the call go to the right people, and Delta knows when to switch into help mode."""

from pathlib import Path

import pytest

from delta.config import Boundaries
from delta.core.playbooks import playbooks_for
from delta.safety.policy import PolicyEngine, Verdict
from delta.tools.emergency import AlertArgs, AlertEmergencyContacts, Contact, EmergencyContacts


def store_with(tmp_path, *people):
    s = EmergencyContacts(tmp_path)
    s.save([Contact(name=n, whatsapp=w, relation=r) for n, w, r in people])
    return s


def test_contacts_are_saved_encrypted(tmp_path):
    from delta.system import vault

    s = store_with(tmp_path, ("Amma", "Amma Home", "mother"))
    assert [c.name for c in s.all()] == ["Amma"]
    if vault.available():
        assert b"Amma" not in (tmp_path / "emergency.json").read_bytes()


def test_always_asks_even_on_autopilot(tmp_path):
    tool = AlertEmergencyContacts(store_with(tmp_path, ("Amma", "Amma Home", "")))
    d = PolicyEngine(Boundaries(autonomy="autopilot")).evaluate(tool, AlertArgs(message="I feel dizzy, please call"))
    assert d.verdict == Verdict.ASK


async def test_sends_to_everyone_then_calls_the_first(tmp_path, monkeypatch):
    import delta.messaging.apps as apps

    log = []

    class FakeWA:
        def send(self, who, text):
            log.append(("send", who, text))

        def call(self, who):
            log.append(("call", who))

    monkeypatch.setattr(apps, "driver", lambda app: FakeWA())
    tool = AlertEmergencyContacts(store_with(tmp_path, ("Amma", "Amma Home", ""), ("Ravi", "+91 98xxxxxx", "")))
    args = AlertArgs(message="I fell down at home and can't get up. Please come.", call=True)
    assert "Amma" in tool.summary(args) and "CALL Amma" in tool.summary(args)
    r = await tool.run(args)
    assert r.ok and r.evidence["sent"] == ["Amma", "Ravi"] and r.evidence["called"] == "Amma"
    assert log[0][0] == "send" and "sent by Delta" in log[0][2] and log[-1] == ("call", "Amma Home")

    log.clear()
    r = await tool.run(AlertArgs(message="Please call me when you can.", contacts=["ravi"]))
    assert r.evidence["sent"] == ["Ravi"] and [x[0] for x in log] == ["send"]


async def test_no_contacts_tells_the_user_what_to_do(tmp_path):
    r = await AlertEmergencyContacts(EmergencyContacts(tmp_path)).run(AlertArgs(message="help, I'm unwell"))
    assert not r.ok and "Emergency contacts" in r.output and "112" in r.output


@pytest.mark.parametrize(("text", "help_mode"), [
    ("I'm not feeling well", True), ("i have chest pain and feel dizzy", True), ("send an SOS", True),
    ("I want to end my life", True), ("help me write a poem", False), ("explain accidental complexity", False),
    ("open notepad", False)])
def test_help_mode_only_when_it_sounds_like_trouble(text, help_mode):
    p = playbooks_for(text)
    assert ("MAY NEED HELP" in p) is help_mode
    if help_mode:
        assert "112" in p and "14416" in p


def test_any_message_during_a_help_request_is_confirmed_even_on_autopilot():
    """Live-test finding: the agent reached the contact with plain send_message and skipped the yes."""
    from delta.core.state import CURRENT_REQUEST, TaskRequest
    from delta.messaging.tools import SendArgs, SendMessage

    policy = PolicyEngine(Boundaries(autonomy="autopilot"))
    tool, args = SendMessage(), SendArgs(app="whatsapp", to="Ravi", text="please come, I'm unwell")
    token = CURRENT_REQUEST.set(TaskRequest("I'm not feeling well"))  # vague: Delta asks before reaching anyone
    try:
        assert policy.evaluate(tool, args).verdict == Verdict.ASK
    finally:
        CURRENT_REQUEST.reset(token)
    token = CURRENT_REQUEST.set(TaskRequest("I'm not feeling well, message Ravi now"))  # clear: that IS the yes
    try:
        assert policy.evaluate(tool, args).verdict == Verdict.ALLOW
        alert = AlertEmergencyContacts(EmergencyContacts(Path(".")))
        assert policy.evaluate(alert, AlertArgs(message="please come, unwell")).verdict == Verdict.ALLOW
    finally:
        CURRENT_REQUEST.reset(token)
    token = CURRENT_REQUEST.set(TaskRequest("send hi to Ravi on WhatsApp"))
    try:
        assert policy.evaluate(tool, args).verdict == Verdict.ALLOW  # everyday autopilot is unchanged
    finally:
        CURRENT_REQUEST.reset(token)
