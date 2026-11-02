"""Fast mouse work: several steps in one call, scroll where you point, safe approvals."""

import pytest

from delta.tools import desktop
from delta.tools.base import Risk
from delta.tools.desktop import ActionsArgs, DesktopController, DoActions, Scroll, ScrollArgs, Step


class FakeMouse:
    def __init__(self):
        self.log = []
        self.pos = (10, 10)

    def click(self, x, y, clicks=1, button="left"):
        self.pos = (x, y)
        self.log.append(("click", x, y, clicks, button))

    def moveTo(self, x, y):
        self.pos = (x, y)
        self.log.append(("move", x, y))

    def dragTo(self, x, y, duration=0, button="left"):
        self.log.append(("drag", self.pos, (x, y)))
        self.pos = (x, y)

    def scroll(self, n):
        self.log.append(("scroll", n))

    def hscroll(self, n):
        self.log.append(("hscroll", n))

    def hotkey(self, *keys):
        self.log.append(("keys", keys))

    def position(self):
        return self.pos


@pytest.fixture
def mouse(monkeypatch):
    m = FakeMouse()
    monkeypatch.setattr(desktop, "_pg", lambda: m)
    monkeypatch.setattr(desktop, "_window_evidence", lambda: {"foreground_window": "YouTube"})
    return m


def ctl(m):
    c = DesktopController()
    c.position = m.position
    return c


async def test_one_call_runs_a_whole_menu_sequence(mouse):
    tool = DoActions(ctl(mouse))
    r = await tool.run(ActionsArgs(steps=[
        Step(action="click", x=100, y=40), Step(action="wait", seconds=0.1),
        Step(action="right_click", x=300, y=200), Step(action="double_click", x=5, y=6),
        Step(action="drag", x=10, y=10, to_x=200, to_y=10), Step(action="scroll", amount=-3, x=640, y=400),
        Step(action="keys", keys=["ctrl", "a"])]))
    assert r.ok and r.evidence["steps_done"] == 7
    assert ("click", 100, 40, 1, "left") in mouse.log and ("click", 300, 200, 1, "right") in mouse.log
    assert ("click", 5, 6, 2, "left") in mouse.log and ("drag", (10, 10), (200, 10)) in mouse.log
    assert mouse.log[-3:] == [("move", 640, 400), ("scroll", -360), ("keys", ("ctrl", "a"))]


async def test_bad_steps_are_rejected_before_the_mouse_moves(mouse):
    r = await DoActions(ctl(mouse)).run(ActionsArgs(steps=[
        Step(action="click", x=1, y=1), Step(action="drag", x=1, y=1)]))
    assert not r.ok and "step 2" in r.output and mouse.log == []


def test_batch_approval_is_as_strict_as_its_riskiest_step():
    t = DoActions.__new__(DoActions)
    assert t.risk_for(ActionsArgs(steps=[Step(action="click", x=1, y=1)])) == Risk.ACT
    assert t.risk_for(ActionsArgs(steps=[Step(action="click", x=1, y=1),
                                         Step(action="type", text="hi\n")])) == Risk.WRITE
    assert t.risk_for(ActionsArgs(steps=[Step(action="keys", keys=["shift", "delete"])])) == Risk.DESTRUCTIVE
    assert "drag (1,2)->(3,4)" in t.summary(ActionsArgs(steps=[Step(action="drag", x=1, y=2, to_x=3, to_y=4)]))


async def test_scroll_goes_where_you_point_and_sideways(mouse):
    s = Scroll(ctl(mouse))
    assert (await s.run(ScrollArgs(amount=-5, x=900, y=500))).ok
    assert mouse.log == [("move", 900, 500), ("scroll", -600)]
    await s.run(ScrollArgs(amount=2, horizontal=True))
    assert mouse.log[-1] == ("hscroll", 240)


def test_enter_inside_a_chat_app_counts_as_sending_a_message(monkeypatch):
    from delta.tools.desktop import KeysArgs, PressKeys, TypeArgs, TypeText

    monkeypatch.setattr(desktop, "_title_at", lambda x, y: "WhatsApp")
    monkeypatch.setattr(desktop, "_title_of", lambda window: "WhatsApp")
    batch = ActionsArgs(steps=[Step(action="click", x=1202, y=973), Step(action="type", text="test from Delta"),
                               Step(action="keys", keys=["enter"])])
    t = DoActions.__new__(DoActions)
    assert t.risk_for(batch) == Risk.EXTERNAL
    assert t.summary(batch) == "SEND a message in WhatsApp: “test from Delta” (by clicking and typing)"
    typer = TypeText.__new__(TypeText)
    assert typer.risk_for(TypeArgs(text="hi\n")) == Risk.EXTERNAL
    assert PressKeys.__new__(PressKeys).risk_for(KeysArgs(keys=["enter"])) == Risk.EXTERNAL


def test_enter_elsewhere_stays_a_normal_write(monkeypatch):
    monkeypatch.setattr(desktop, "_title_at", lambda x, y: "Untitled - Notepad")
    monkeypatch.setattr(desktop, "_title_of", lambda window: "Untitled - Notepad")
    batch = ActionsArgs(steps=[Step(action="click", x=1, y=1), Step(action="keys", keys=["enter"])])
    assert DoActions.__new__(DoActions).risk_for(batch) == Risk.WRITE


def test_chat_apps_are_recognised_by_title():
    from delta.tools.desktop import messaging_app

    assert messaging_app("WhatsApp") == "WhatsApp" and messaging_app("Telegram (5201)") == "Telegram"
    assert messaging_app("Slack (DM) - Ravi - Slack") == "Slack" and messaging_app("Notepad") is None
