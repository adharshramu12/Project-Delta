"""Ask before acting when a request leaves out something only the user can decide.

A meeting at the wrong time is worse than a question, and models happily invent "tomorrow 10 am".
So scheduling requests are checked here, with no AI call: if the day or the time is missing, Delta
asks one short question with ready-made options instead of guessing. When both are given it goes
straight ahead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

SCHEDULING = re.compile(
    r"\b(schedule|book|set\s*up|arrange|plan|organi[sz]e|fix|put)\b.{0,60}?"
    r"\b(meeting|meet|call|zoom|teams|catch[\s-]?up|sync|standup|stand-up|interview|demo|review)\b", re.I)
MONTHS = r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|" \
         r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
DAY = re.compile(
    r"\b(today|tomorrow|tmrw|tonight|day after tomorrow|this (?:morning|afternoon|evening)|"
    r"(?:next |this |coming )?(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day)?|"
    rf"\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{MONTHS})|(?:{MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?|"
    r"\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?|\d{4}-\d{2}-\d{2}|in \d+ days?)\b", re.I)
TIME = re.compile(
    r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|\d{1,2}:\d{2}|noon|midnight|\d{1,2}\s*o'?clock|"
    r"in \d+ (?:minutes?|mins?|hours?|hrs?)|now|right now)(?![\w])", re.I)


@dataclass
class Question:
    text: str
    options: list[str] = field(default_factory=list)


def meeting_question(text: str, now: datetime | None = None) -> Question | None:
    """A question to ask first, or None when the request can go ahead as it is."""
    if not SCHEDULING.search(text):
        return None
    has_day, has_time = bool(DAY.search(text)), bool(TIME.search(text))
    if has_day and has_time:
        return None
    now = now or datetime.now()
    defaults = "It'll be 30 minutes with a Google Meet link unless you tell me otherwise."
    if not has_day and not has_time:
        return Question(f"When should I schedule it? You didn't mention a day or time. {defaults}",
                        _slots(now))
    if not has_time:
        return Question(f"What time should it start? You gave the day but not the time. {defaults}",
                        ["10:00 AM", "3:00 PM", "6:00 PM"])
    return Question(f"Which day should it be? You gave the time but not the day. {defaults}",
                    ["today", "tomorrow", f"{(now + timedelta(days=2)):%A}"])


EDITING = re.compile(
    r"\b(reschedule|move|shift|postpone|prepone|push|pull|change|update|edit)\b.{0,60}?"
    r"\b(meeting|meet|call|event|standup|stand-up|sync|review|interview|demo|appointment|it)\b", re.I)
MIDNIGHT = re.compile(r"\b12(?::00)?\s*(?:am|a\.m\.)", re.I)


def edit_question(text: str, now: datetime | None = None) -> Question | None:
    """For changing an existing meeting: ask when it should move to, if that is missing."""
    if not EDITING.search(text) or (SCHEDULING.search(text) and not EDITING.search(text.split(" to ")[0])):
        return None
    if re.search(r"\b(cancel|delete|remove|rename|title)\b", text, re.I):
        return None  # these need no new time
    if re.search(r"\b(longer|shorter|minutes?|mins?|hours?|hrs?)\b", text, re.I) and not TIME.search(text):
        return None  # a length change only
    if DAY.search(text) or TIME.search(text):
        return None
    return Question("When should I move it to? Pick a slot or type your own; it keeps its length and its Meet link.",
                    _slots(now or datetime.now()))


def ambiguity_question(text: str) -> Question | None:
    """'12 am' is midnight: almost always a slip for noon in a meeting request. Ask once."""
    if (SCHEDULING.search(text) or EDITING.search(text)) and MIDNIGHT.search(text) \
            and not re.search(r"\b(noon|midnight|12\s*pm)\b", text, re.I):
        return Question("Just to be sure: 12 AM is midnight. Did you mean 12 PM (noon)?",
                        ["12:00 PM (noon)", "12:00 AM (midnight)"])
    return None


def ask_first(text: str, now: datetime | None = None) -> Question | None:
    """The one question to ask before acting, if any (0 tokens)."""
    return ambiguity_question(text) or edit_question(text, now) or meeting_question(text, now)


def _slots(now: datetime) -> list[str]:
    """Three sensible upcoming slots, e.g. 'today 6:00 PM', 'tomorrow 10:00 AM', 'tomorrow 3:00 PM'."""
    nxt = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    first = f"today {nxt:%I:%M %p}".replace(" 0", " ") if nxt.date() == now.date() and nxt.hour <= 20 else None
    return [s for s in (first, "tomorrow 10:00 AM", "tomorrow 3:00 PM", None if first else "tomorrow 6:00 PM") if s]
