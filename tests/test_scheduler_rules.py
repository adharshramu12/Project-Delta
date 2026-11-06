"""Next-run maths and natural-language parsing for scheduled tasks: DST, month ends, cron, plain words."""

from datetime import UTC, datetime, timedelta

import pytest

from delta.scheduler.parse import ParseError, parse_schedule
from delta.scheduler.rules import Rule, RuleError, describe, fmt_when, get_zone, next_run, to_local, to_utc

NY, LONDON, IST = "America/New_York", "Europe/London", "Asia/Kolkata"


def need_zone(name: str) -> None:
    if get_zone(name) is None:
        pytest.skip(f"time zone data for {name} is not installed")


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


# --- DST ---------------------------------------------------------------------------------------------

def test_daily_run_keeps_its_local_time_when_dst_starts():
    need_zone(NY)
    rule = Rule("daily", time="09:00")
    before = next_run(rule, NY, utc(2026, 3, 7, 15, 0))  # Sat after 9 AM EST
    assert before == utc(2026, 3, 8, 13, 0)  # Sun 9 AM EDT (UTC-4)
    assert next_run(rule, NY, utc(2026, 3, 6, 15, 0)) == utc(2026, 3, 7, 14, 0)  # 9 AM EST (UTC-5)


def test_a_time_inside_the_dst_gap_runs_right_after_the_jump():
    need_zone(NY)
    rule = Rule("daily", time="02:30")  # 2:30 AM does not exist on 8 March 2026 in New York
    first = next_run(rule, NY, utc(2026, 3, 7, 12, 0))
    assert first == utc(2026, 3, 8, 7, 30)  # 03:30 EDT
    assert to_local(first, NY).hour == 3
    assert next_run(rule, NY, first) == utc(2026, 3, 9, 6, 30)  # back to 02:30, now EDT


def test_a_repeated_time_when_clocks_go_back_runs_once():
    need_zone(NY)
    rule = Rule("daily", time="01:30")  # 1 November 2026: 01:00-02:00 happens twice
    first = next_run(rule, NY, utc(2026, 10, 31, 12, 0))
    assert first == utc(2026, 11, 1, 5, 30)  # the first 01:30 (EDT)
    second = next_run(rule, NY, first)
    assert second == utc(2026, 11, 2, 6, 30)  # next day, not the second 01:30 an hour later


def test_dst_in_europe_and_weekly():
    need_zone(LONDON)
    rule = Rule("weekly", time="10:00", days=[0])  # Mondays
    assert next_run(rule, LONDON, utc(2026, 3, 27, 0, 0)) == utc(2026, 3, 30, 9, 0)  # BST after 29 March
    assert next_run(rule, LONDON, utc(2026, 3, 20, 0, 0)) == utc(2026, 3, 23, 10, 0)  # GMT


def test_every_n_hours_counts_real_time_across_dst():
    need_zone(NY)
    anchor = utc(2026, 3, 8, 5, 0)  # midnight EST
    rule = Rule("interval", minutes=60, anchor=anchor.isoformat())
    a = next_run(rule, NY, utc(2026, 3, 8, 6, 30))
    b = next_run(rule, NY, a)
    assert a == utc(2026, 3, 8, 7, 0) and b - a == timedelta(hours=1)


def test_india_has_no_dst_and_runs_at_the_same_utc_time():
    need_zone(IST)
    rule = Rule("daily", time="09:00")
    assert next_run(rule, IST, utc(2026, 11, 6, 6, 0)) == utc(2026, 11, 7, 3, 30)


def test_the_pcs_own_time_zone_round_trips():
    wall = datetime(2026, 7, 15, 9, 0)
    assert to_local(to_utc(wall, ""), "") == wall
    nxt = next_run(Rule("daily", time="09:00"), "", utc(2026, 7, 15, 0, 0))
    assert nxt is not None and to_local(nxt, "").time().hour == 9


def test_an_unknown_zone_falls_back_to_the_pc_instead_of_crashing():
    assert get_zone("Mars/Olympus") is None
    assert next_run(Rule("daily", time="09:00"), "Mars/Olympus", utc(2026, 7, 15)) is not None


# --- month ends, cron ------------------------------------------------------------------------------------

def test_cron_on_the_31st_skips_short_months():
    need_zone(IST)
    rule = Rule("cron", expr="0 9 31 * *")
    assert to_local(next_run(rule, IST, utc(2026, 1, 31, 6, 0)), IST) == datetime(2026, 3, 31, 9, 0)
    assert to_local(next_run(rule, IST, utc(2026, 3, 31, 6, 0)), IST) == datetime(2026, 5, 31, 9, 0)


def test_cron_on_29_february_waits_for_a_leap_year():
    need_zone(IST)
    nxt = next_run(Rule("cron", expr="0 0 29 2 *"), IST, utc(2026, 3, 1))
    assert to_local(nxt, IST) == datetime(2028, 2, 29, 0, 0)


def test_cron_weekdays_steps_lists_and_names():
    need_zone(IST)
    weekdays = Rule("cron", expr="30 8 * * mon-fri")
    fri = utc(2026, 11, 6, 6, 0)  # Friday, after 08:30 IST
    assert to_local(next_run(weekdays, IST, fri), IST) == datetime(2026, 11, 9, 8, 30)  # Monday
    every15 = Rule("cron", expr="*/15 9-10 * * *")
    assert to_local(next_run(every15, IST, utc(2026, 11, 6, 3, 50)), IST) == datetime(2026, 11, 6, 9, 30)
    sunday7 = Rule("cron", expr="0 7 * * 7")  # 7 = Sunday too
    assert to_local(next_run(sunday7, IST, fri), IST).weekday() == 6


def test_cron_day_of_month_or_day_of_week():
    need_zone(IST)
    rule = Rule("cron", expr="0 9 1 * mon")  # the 1st OR any Monday (standard cron)
    assert to_local(next_run(rule, IST, utc(2026, 11, 6, 6, 0)), IST) == datetime(2026, 11, 9, 9, 0)


def test_daily_at_month_and_year_end():
    need_zone(IST)
    rule = Rule("daily", time="23:59")
    assert to_local(next_run(rule, IST, utc(2026, 12, 31, 18, 0)), IST) == datetime(2026, 12, 31, 23, 59)
    assert to_local(next_run(rule, IST, utc(2026, 12, 31, 18, 31)), IST) == datetime(2027, 1, 1, 23, 59)


def test_once_in_the_past_never_runs_and_bad_rules_are_refused():
    assert next_run(Rule("once", at="2026-01-01T09:00"), "", utc(2026, 6, 1)) is None
    with pytest.raises(RuleError):
        Rule("interval", minutes=4).validate()
    with pytest.raises(RuleError):
        Rule("cron", expr="61 * * * *").validate()
    with pytest.raises(RuleError):
        Rule("cron", expr="* * *").validate()
    with pytest.raises(RuleError):
        Rule("weekly", time="09:00").validate()
    with pytest.raises(RuleError):
        Rule("daily", time="25:00").validate()


def test_rules_survive_a_round_trip_and_read_in_plain_words():
    r = Rule("weekly", time="18:00", days=[0, 2, 4])
    assert Rule.from_dict(r.to_dict()) == r
    assert describe(r, "") == "every Monday, Wednesday and Friday at 6:00 PM"
    assert describe(Rule("weekly", time="08:00", days=[0, 1, 2, 3, 4]), "") == "every weekday at 8:00 AM"
    assert describe(Rule("interval", minutes=120), "") == "every 2 hours"
    assert describe(Rule("daily", time="00:00"), "") == "every day at 12:00 AM"
    need_zone(IST)
    assert fmt_when(utc(2026, 11, 7, 3, 30), IST) == "Sat 7 Nov 2026, 9:00 AM"


# --- natural language ----------------------------------------------------------------------------------

NOW = utc(2026, 11, 6, 6, 0)  # Friday 6 Nov 2026, 11:30 AM in India


def p(text: str):
    need_zone(IST)
    return parse_schedule(text, IST, NOW)


def test_the_three_examples_from_the_brief():
    a = p("every day at 9 am check my attendance on the ERP")
    assert (a.kind, a.rule.type, a.rule.time, a.prompt) == ("task", "daily", "09:00", "check my attendance on the ERP")
    b = p("remind me at 6 pm to call Ravi")
    assert (b.kind, b.rule.type, b.rule.at, b.prompt) == ("reminder", "once", "2026-11-06T18:00", "call Ravi")
    c = p("every Monday 10 am summarise my downloads folder")
    assert (c.rule.type, c.rule.days, c.rule.time) == ("weekly", [0], "10:00")
    assert c.prompt == "summarise my downloads folder"


@pytest.mark.parametrize(("text", "kind", "rtype", "field", "value", "prompt"), [
    ("every 30 minutes check my mail", "task", "interval", "minutes", 30, "check my mail"),
    ("every 2 hours back up my notes", "task", "interval", "minutes", 120, "back up my notes"),
    ("hourly check disk space", "task", "interval", "minutes", 60, "check disk space"),
    ("tomorrow at 7:30 am open zoom", "task", "once", "at", "2026-11-07T07:30", "open zoom"),
    ("remind me in 20 minutes to drink water", "reminder", "once", "at", "2026-11-06T11:50", "drink water"),
    ("every weekday at 8 send me the news", "task", "weekly", "days", [0, 1, 2, 3, 4], "send me the news"),
    ("on weekends at 10 am clean downloads", "task", "weekly", "days", [5, 6], "clean downloads"),
    ("every mon, wed and fri at 6pm log my gym", "task", "weekly", "days", [0, 2, 4], "log my gym"),
    ("on 20 Nov at 10:00 renew my passport", "task", "once", "at", "2026-11-20T10:00", "renew my passport"),
    ("please remind me on 2026-12-25 at 9 to wish everyone", "reminder", "once", "at", "2026-12-25T09:00",
     "wish everyone"),
    ("cron 0 9 * * 1-5 check the ERP", "task", "cron", "expr", "0 9 * * 1-5", "check the ERP"),
    ("check my attendance on the ERP every day at 9am", "task", "daily", "time", "09:00",
     "check my attendance on the ERP"),
    ("every day at 9 pm remind me to take my tablet", "reminder", "daily", "time", "21:00", "take my tablet"),
    ("remind me tonight to call amma", "reminder", "once", "at", "2026-11-06T21:00", "call amma"),
    ("at 10 am water the plants", "task", "once", "at", "2026-11-07T10:00", "water the plants"),  # 10 AM passed
    ("next monday at 10 call the bank", "task", "once", "at", "2026-11-09T10:00", "call the bank"),
    ("on Friday at 5 pm submit the report", "task", "once", "at", "2026-11-06T17:00", "submit the report"),
    ("every morning summarise my emails", "task", "daily", "time", "09:00", "summarise my emails"),
    ("remind me at 12 noon to eat lunch", "reminder", "once", "at", "2026-11-06T12:00", "eat lunch"),
])
def test_natural_language_cases(text, kind, rtype, field, value, prompt):
    r = p(text)
    assert (r.kind, r.rule.type, getattr(r.rule, field), r.prompt) == (kind, rtype, value, prompt)


@pytest.mark.parametrize("text", [
    "every 2 minutes ping google",  # below the 5 minute floor
    "every minute check",
    "remind me to call Ravi",  # no time
    "every day check stuff",  # repeat without a time
    "every day at 9 am",  # no task
    "on 2026-01-02 at 9 do it",  # in the past
    "on 31 Feb at 9 do it",
])
def test_unclear_requests_get_a_question_not_a_guess(text):
    with pytest.raises(ParseError):
        p(text)
