"""Meetings add-on: links read without AI, calendar events pre-filled, invites need approval."""

from datetime import datetime
from urllib.parse import parse_qs, urlparse

from delta.tools.base import Risk
from delta.tools.meetings import (
    ScheduleArgs,
    ScheduleMeeting,
    StartArgs,
    StartMeeting,
    calendar_url,
    find_meet_code,
    gmail_draft_url,
)


def test_meet_code_is_read_from_the_browser_title_and_old_ones_ignored():
    titles = ["Delta", "Meet - abc-defg-hij - Google Chrome", "Inbox"]
    assert find_meet_code(titles) == "abc-defg-hij"
    assert find_meet_code(titles, known={"abc-defg-hij"}) is None
    assert find_meet_code(["Meet - Google Chrome"]) is None  # still loading: no code yet


def test_calendar_event_is_prefilled_with_time_guests_and_link():
    url = calendar_url("Sprint sync", datetime(2026, 11, 3, 17, 0), 45, ["a@x.com", "b@y.com"], "Agenda",
                       "https://meet.google.com/abc-defg-hij")
    q = parse_qs(urlparse(url).query)
    assert q["action"] == ["TEMPLATE"] and q["text"] == ["Sprint sync"]
    assert q["dates"] == ["20261103T170000/20261103T174500"]  # local time, user's own zone
    assert q["add"] == ["a@x.com,b@y.com"] and q["location"] == ["https://meet.google.com/abc-defg-hij"]
    assert "Join: https://meet.google.com/abc-defg-hij" in q["details"][0]


def test_invite_draft_carries_the_link():
    q = parse_qs(urlparse(gmail_draft_url(["a@x.com"], "Meeting", "Join: https://meet.google.com/x")).query)
    assert q["to"] == ["a@x.com"] and q["body"] == ["Join: https://meet.google.com/x"]


def test_scheduling_emails_guests_so_it_always_asks_first():
    assert ScheduleMeeting.risk == Risk.EXTERNAL
    assert "no guests" in ScheduleMeeting().summary(ScheduleArgs(title="x", start="2026-11-03T17:00"))
    assert StartMeeting.risk == Risk.ACT


async def test_past_times_are_refused_before_anything_opens():
    r = await ScheduleMeeting().run(ScheduleArgs(title="x", start="2020-01-01T10:00"))
    assert not r.ok and "past" in r.output


async def test_zoom_and_teams_hand_over_honestly(monkeypatch):
    import delta.tools.meetings as m

    opened = []
    monkeypatch.setattr(m.os, "startfile", opened.append, raising=False)
    r = await StartMeeting().run(StartArgs(platform="zoom"))
    assert not r.ok and opened == ["https://zoom.us/start/videomeeting"] and "not readable" in r.output


def test_other_names_for_guests_and_length_are_not_dropped():
    a = ScheduleArgs.model_validate({"title": "x", "start": "2026-11-03T17:00", "duration_minutes": 45,
                                     "attendees": ["ravi@x.com"]})
    assert a.minutes == 45 and a.guests == ["ravi@x.com"]
    assert "emails them an invite" in ScheduleMeeting().summary(a)


def test_calendar_labels_and_field_text():
    from delta.tools.meetings import _current_start, field_date, field_time, time_label

    noon = datetime(2026, 11, 3, 12, 0)
    assert time_label(noon) == "12pm" and time_label(datetime(2026, 11, 3, 17, 30)) == "5:30pm"
    assert time_label(datetime(2026, 11, 3, 0, 0)) == "12am" and field_time(noon) == "12:00pm"
    assert field_date(noon) == "3 Nov 2026"
    assert _current_start("10am to 10:30am, Delta review, RAMU Bachu, Location: https://meet.google.com/x, "
                          "3 November 2026") == datetime(2026, 11, 3, 10, 0)


def test_edit_risk_and_summary():
    from delta.tools.meetings import EditArgs, EditMeeting

    t = EditMeeting()
    assert t.risk_for(EditArgs(title="x", cancel=True)) == Risk.DESTRUCTIVE
    a = EditArgs.model_validate({"title": "Delta review", "start": "2026-11-03T12:00", "duration_minutes": 60})
    assert t.risk_for(a) == Risk.WRITE
    assert t.summary(a) == "edit 'Delta review': move to 2026-11-03T12:00, make it 60 min"


async def test_edit_refuses_past_times_and_empty_changes():
    from delta.tools.meetings import EditArgs, EditMeeting

    assert "past" in (await EditMeeting().run(EditArgs(title="x", new_start="2020-01-01T10:00"))).output
    assert "Nothing to change" in (await EditMeeting().run(EditArgs(title="x"))).output
