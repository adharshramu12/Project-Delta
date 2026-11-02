"""Ask before scheduling when the day or time is missing; go straight ahead when both are given."""

from datetime import datetime

import pytest

from delta.core.clarify import meeting_question

NOW = datetime(2026, 11, 2, 14, 10)


@pytest.mark.parametrize("text", [
    "Schedule a meeting with ravi@x.com tomorrow at 5 pm",
    "book a call on Friday 10:30",
    "set up a zoom meeting on 5 Nov at noon",
    "schedule a standup 2026-11-04 9am",
])
def test_complete_requests_go_straight_ahead(text):
    assert meeting_question(text, NOW) is None


def test_missing_day_and_time_asks_with_ready_options():
    q = meeting_question("Schedule a meeting with Ravi", NOW)
    assert q and "didn't mention a day or time" in q.text and "30 minutes" in q.text
    assert q.options == ["today 3:00 PM", "tomorrow 10:00 AM", "tomorrow 3:00 PM"]


def test_late_in_the_day_offers_tomorrow_only():
    q = meeting_question("schedule a call", datetime(2026, 11, 2, 21, 30))
    assert q and all(o.startswith("tomorrow") for o in q.options)


def test_only_the_missing_part_is_asked():
    assert "what time" in meeting_question("schedule a meeting tomorrow", NOW).text.lower()
    q = meeting_question("schedule a meeting at 4 pm", NOW)
    assert "which day" in q.text.lower() and q.options[:2] == ["today", "tomorrow"]


def test_other_requests_are_not_questioned():
    for text in ["start a Google Meet", "play a song", "what is in my calendar", "meet me in the folder"]:
        assert meeting_question(text, NOW) is None


async def test_agent_asks_without_any_ai_call(tmp_path):
    from fakes import FakeProvider

    from delta.config import AgentConfig, MemoryConfig, Settings
    from delta.core.events import EventBus
    from delta.core.orchestrator import Orchestrator
    from delta.core.state import TaskRequest
    from delta.llm.router import ModelRouter
    from delta.safety.audit import AuditLog

    calls = []
    router = ModelRouter({"x": FakeProvider("x", handler=lambda t, m: calls.append(t))}, {"plan": ["x"]})

    class Exec:
        registry = type("R", (), {"names": lambda self: []})()

        def forget_task(self, _):
            pass

    s = Settings(agent=AgentConfig(), memory=MemoryConfig(enabled=False), data_dir=tmp_path)
    orch = Orchestrator(s, router, Exec(), None, None, EventBus(), AuditLog(tmp_path / "a.jsonl"))
    r = await orch.run(TaskRequest("Schedule a meeting with Ravi"))
    assert r.status == "needs_user" and r.options and r.asked_about == "Schedule a meeting with Ravi"
    assert calls == []


def test_reschedule_without_a_new_time_asks_when():
    from delta.core.clarify import ask_first

    q = ask_first("reschedule my Delta review meeting", NOW)
    assert q and "move it to" in q.text and q.options
    assert ask_first("move the Delta review meeting to 3 pm", NOW) is None
    assert ask_first("move the Delta review meeting to tomorrow", NOW) is None
    assert ask_first("cancel the Delta review meeting", NOW) is None
    assert ask_first("make the Delta review meeting 1 hour longer", NOW) is None


def test_twelve_am_is_double_checked_once():
    from delta.core.clarify import ask_first

    q = ask_first("move the Delta review meeting to 12 am", NOW)
    assert q and "midnight" in q.text and q.options[0] == "12:00 PM (noon)"
    assert ask_first("move the Delta review meeting to 12 am (12:00 PM (noon))", NOW) is None
    assert ask_first("schedule a standup tomorrow at 12 pm", NOW) is None
