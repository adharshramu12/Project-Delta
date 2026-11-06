"""Natural language into a schedule, with no AI call (instant, free, and the same answer every time).

Understood, anywhere in the sentence:
  every day at 9 am / daily at 21:00 / every morning (9 AM) / every evening (6 PM) / every night (9 PM)
  every Monday 10 am / every Mon, Wed and Fri at 6 pm / every weekday at 8 / on weekends at 10 am
  every 30 minutes / every 2 hours / every hour / hourly / every half hour
  at 6 pm (today, or tomorrow when 6 PM has passed) / tomorrow at 7:30 am / tonight at 9
  on Friday at 5 pm / next Monday at 10 / on 20 Nov at 10:00 / on 2026-11-20 at 10:00
  in 20 minutes / in 2 hours
  cron 0 9 * * 1-5 <task>
"remind me ..." makes a reminder: a light run that only sends the message, with no AI and no tools.

Anything it cannot place raises ParseError with a plain question back to the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime

from delta.scheduler.rules import MIN_INTERVAL_MINUTES, Rule, RuleError, now_utc, to_local

Kind = str  # "task" | "reminder"


class ParseError(ValueError):
    """The sentence has no schedule Delta can be sure of. The message is a question for the user."""


@dataclass
class Parsed:
    rule: Rule
    prompt: str
    kind: Kind
    name: str


_DAY = r"(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
_DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
           "oct": 10, "nov": 11, "dec": 12}
_MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|" \
         r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DAY_LIST = rf"{_DAY}s?(?:\s*(?:,|and|&|/|\+)\s*{_DAY}s?)*"

_POLITE = re.compile(r"^(?:(?:please|pls|hey\s+delta|delta|ok(?:ay)?|can\s+you|could\s+you|would\s+you|"
                     r"i\s+want\s+you\s+to|"
                     r"schedule(?:\s+(?:a|this|the))?(?:\s+task)?|set\s+up(?:\s+a)?(?:\s+task)?|"
                     r"create(?:\s+a)?\s+(?:schedule|task|routine)|add(?:\s+a)?\s+(?:schedule|routine))[\s,:]+)+", re.I)
_REMIND = re.compile(r"^(?:remind\s+me|set\s+(?:a\s+)?reminder|reminder|add\s+(?:a\s+)?reminder)\b[\s,:]*"
                     r"(?:(?:to|that|about|of)\b\s*)?", re.I)
_REMIND_INNER = re.compile(r"^remind\s+me\s+(?:to|that|about|of)\b\s*", re.I)

_CRON = re.compile(r"\bcron\s+((?:\S+\s+){4}\S+)", re.I)
_INTERVAL = re.compile(r"\b(?:every|each)\s+(\d+)\s*(minutes?|mins?|m|hours?|hrs?|h)\b", re.I)
_INTERVAL_WORD = re.compile(r"\b(?:every\s+(?:single\s+)?(hour|minute|half\s+(?:an\s+)?hour)|(hourly))\b", re.I)
_WEEKLY = re.compile(rf"\b(?:every|each)\s+({_DAY_LIST})\b", re.I)
_WEEKLY_PLURAL = re.compile(rf"\bon\s+((?:{_DAY}s)(?:\s*(?:,|and|&|/)\s*{_DAY}s)*)\b", re.I)
_WEEKDAYS = re.compile(r"\b(?:every|each|on)\s+(week\s*days?|working\s+days?|weekends?)\b", re.I)
_DAILY = re.compile(r"\b(every\s*day|everyday|daily|each\s+day|every\s+(?:single\s+)?day|"
                    r"every\s+(morning|evening|night|afternoon))\b", re.I)
_PART_OF_DAY = {"morning": dtime(9), "afternoon": dtime(14), "evening": dtime(18), "night": dtime(21)}
_RELATIVE = re.compile(r"\bin\s+(\d+|an?|half\s+an?)\s*(minutes?|mins?|hours?|hrs?|days?)\b", re.I)
_DAYWORD = re.compile(r"\b(day\s+after\s+tomorrow|tomorrow(?:\s+(morning|afternoon|evening|night))?|tmrw|"
                      r"today|tonight|this\s+(morning|afternoon|evening))\b", re.I)
_ISO_DATE = re.compile(r"\b(?:on\s+)?(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DM_DATE = re.compile(rf"\b(?:on\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH})\b(?:\s+(\d{{4}}))?", re.I)
_MD_DATE = re.compile(rf"\b(?:on\s+)?({_MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", re.I)
_ONE_DAY = re.compile(rf"\b(?:on\s+|next\s+|this\s+|coming\s+)?({_DAY})\b", re.I)
_TIME = re.compile(
    r"(?<![\w:])(?:at\s+|@\s*)?(?:"
    r"(?P<h1>\d{1,2})(?:[:.](?P<m1>\d{2}))?\s*(?P<ap>a\.?\s?m\.?|p\.?\s?m\.?)(?![a-z])"
    r"|(?P<h2>\d{1,2}):(?P<m2>\d{2})(?!\d)"
    r"|(?:12\s*)?(?P<word>noon|midday|midnight)"
    r"|(?P<h3>\d{1,2})\s*o'?\s?clock"
    r")", re.I)
_BARE_AT = re.compile(r"\bat\s+(?P<h>\d{1,2})(?![\d:.]|\s*(?:%|minutes?|mins?|hours?|hrs?|days?|a\.?m|p\.?m))\b", re.I)


def _cut(text: str, m: re.Match[str]) -> str:
    return (text[:m.start()] + " " + text[m.end():]).strip()


def _day_index(word: str) -> int:
    return _DAY_INDEX[word.lower()[:3]]


def _clock(m: re.Match[str]) -> dtime:
    if m["word"]:
        return dtime(0) if m["word"].lower() == "midnight" else dtime(12)
    if m["h1"]:
        h, mi = int(m["h1"]), int(m["m1"] or 0)
        if not 1 <= h <= 12:
            raise ParseError(f"'{m.group(0).strip()}' is not a time I understand.")
        pm = m["ap"].lower().startswith("p")
        h = (h % 12) + (12 if pm else 0)
    elif m["h2"]:
        h, mi = int(m["h2"]), int(m["m2"])
    else:
        h, mi = int(m["h3"]), 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ParseError(f"'{m.group(0).strip()}' is not a time I understand.")
    return dtime(h, mi)


def _find_time(text: str) -> tuple[dtime | None, str]:
    m = _TIME.search(text)
    if m:
        return _clock(m), _cut(text, m)
    b = _BARE_AT.search(text)
    if b and 0 <= int(b["h"]) <= 23:
        return dtime(int(b["h"])), _cut(text, b)
    return None, text


def _clean_prompt(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip(" ,;:-–.")
    # leftovers of the schedule words, at either end only (never inside the task itself)
    lead = re.compile(r"^(?:and|then|to|at|on|every|each|from|starting|,|-|:)\b[\s,:]*", re.I)
    trail = re.compile(r"[\s,]*\b(?:at|on|every|each|starting|from|and|,)$", re.I)
    for _ in range(4):
        t = lead.sub("", t).strip(" ,;:-–.")
        t = trail.sub("", t).strip(" ,;:-–.")
    return t


def _name_for(prompt: str) -> str:
    words = prompt.split()
    name = " ".join(words[:7])
    return (name[:60] + ("…" if len(words) > 7 or len(name) > 60 else "")).strip()


def parse_schedule(text: str, tz: str = "", now: datetime | None = None) -> Parsed:
    """Read a schedule out of a sentence. `now` is a UTC instant (default: the current time)."""
    now = now or now_utc()
    local_now = to_local(now, tz)
    t = " ".join((text or "").split()).strip(" .!")
    t = _POLITE.sub("", t)
    kind: Kind = "task"
    if m := _REMIND.match(t):
        kind, t = "reminder", t[m.end():]
    t = _POLITE.sub("", t)  # "remind me, please, ..."

    rule: Rule | None = None
    if m := _CRON.search(t):
        rule, t = Rule("cron", expr=" ".join(m[1].split())), _cut(t, m)
    elif m := _INTERVAL.search(t):
        n, unit = int(m[1]), m[2].lower()
        minutes = n * 60 if unit.startswith("h") else n
        if minutes < MIN_INTERVAL_MINUTES:
            raise ParseError(f"The shortest repeat I allow is every {MIN_INTERVAL_MINUTES} minutes, "
                             "so the PC is not kept busy all day. Pick 5 minutes or more.")
        rule, t = Rule("interval", minutes=minutes), _cut(t, m)
    elif m := _INTERVAL_WORD.search(t):
        word = (m[1] or m[2]).lower()
        if word == "minute":
            raise ParseError(f"Every minute is too often. The shortest repeat is every {MIN_INTERVAL_MINUTES} minutes.")
        rule, t = Rule("interval", minutes=30 if word.startswith("half") else 60), _cut(t, m)

    if rule is not None and rule.type == "interval":
        rule.anchor = now.replace(second=0, microsecond=0).isoformat(timespec="seconds")
    if rule is None:
        rule, t = _recurring(t)
    if rule is None:
        rule, t = _once(t, tz, local_now)

    prompt = _clean_prompt(t)
    if m := _REMIND_INNER.match(prompt):  # "every day at 9 pm remind me to take my tablet"
        kind, prompt = "reminder", prompt[m.end():]
    prompt = _clean_prompt(prompt)
    if not prompt:
        raise ParseError("What should I do (or remind you about) at that time?")
    try:
        rule.validate()
    except RuleError as e:
        raise ParseError(str(e)) from e
    return Parsed(rule, prompt, kind, _name_for(prompt))


def _recurring(t: str) -> tuple[Rule | None, str]:
    days: list[int] | None = None
    default_time: dtime | None = None
    if m := _WEEKDAYS.search(t):
        word = m[1].lower()
        days = [5, 6] if word.startswith("weekend") else [0, 1, 2, 3, 4]
        t = _cut(t, m)
    elif m := (_WEEKLY.search(t) or _WEEKLY_PLURAL.search(t)):
        days = sorted({_day_index(w) for w in re.findall(_DAY, m[1], re.I)})
        t = _cut(t, m)
    elif m := _DAILY.search(t):
        days = list(range(7))
        if m[2]:
            default_time = _PART_OF_DAY[m[2].lower()]
        t = _cut(t, m)
    if days is None:
        return None, t
    clock, t = _find_time(t)
    clock = clock or default_time
    if clock is None:
        raise ParseError("What time should it run? For example: every day at 9 am.")
    hhmm = f"{clock.hour:02d}:{clock.minute:02d}"
    if len(days) == 7:
        return Rule("daily", time=hhmm), t
    return Rule("weekly", time=hhmm, days=days), t


def _once(t: str, tz: str, local_now: datetime) -> tuple[Rule, str]:
    if m := _RELATIVE.search(t):
        raw, unit = m[1].lower(), m[2].lower()
        n = 0.5 if raw.startswith("half") else 1 if raw in ("a", "an") else int(raw)
        delta = timedelta(days=n) if unit.startswith("d") else timedelta(hours=n) if unit.startswith("h") \
            else timedelta(minutes=n)
        when = (local_now + delta).replace(second=0, microsecond=0)
        if delta < timedelta(minutes=1):
            raise ParseError("That is too soon to schedule. Pick at least 1 minute from now.")
        return Rule("once", at=when.isoformat(timespec="minutes")), _cut(t, m)

    day: date | None = None
    default_time: dtime | None = None
    weekday_today = False  # "on Friday" said on a Friday: today if the time is still ahead, else next week
    if m := _DAYWORD.search(t):
        word = m[1].lower()
        if word.startswith("day after"):
            day = local_now.date() + timedelta(days=2)
        elif word.startswith(("tomorrow", "tmrw")):
            day = local_now.date() + timedelta(days=1)
            if m[2]:
                default_time = _PART_OF_DAY[m[2].lower()]
        else:
            day = local_now.date()
            part = "night" if word == "tonight" else (m[3] or "").lower()
            default_time = _PART_OF_DAY.get(part)
        t = _cut(t, m)
    elif m := _ISO_DATE.search(t):
        day = _safe_date(int(m[1]), int(m[2]), int(m[3]))
        t = _cut(t, m)
    elif m := (_DM_DATE.search(t) or _MD_DATE.search(t)):
        if m.re is _DM_DATE:
            d, mon, year = int(m[1]), _MONTHS[m[2].lower()[:3]], m[3]
        else:
            mon, d, year = _MONTHS[m[1].lower()[:3]], int(m[2]), m[3]
        day = _safe_date(int(year) if year else local_now.year, mon, d)
        if not year and day < local_now.date():
            day = _safe_date(local_now.year + 1, mon, d)
        t = _cut(t, m)
    elif m := _ONE_DAY.search(t):
        target = _day_index(m[1])
        ahead = (target - local_now.weekday()) % 7
        if m.group(0).lower().startswith("next"):
            ahead = ahead or 7
        weekday_today = ahead == 0
        day = local_now.date() + timedelta(days=ahead)
        t = _cut(t, m)

    clock, t = _find_time(t)
    clock = clock or default_time
    if clock is None:
        if day is None:
            raise ParseError("When should it run? For example: at 6 pm, tomorrow at 9 am, or every day at 9 am.")
        raise ParseError("What time should it run on that day? For example: at 9 am.")
    if day is None:
        day = local_now.date()
        if datetime.combine(day, clock) <= local_now:
            day += timedelta(days=1)  # "at 6 pm" after 6 PM means tomorrow
    when = datetime.combine(day, clock)
    if weekday_today and when <= local_now:
        when += timedelta(days=7)
    if when <= local_now:
        raise ParseError(f"{when:%d %b %Y} at {clock:%H:%M} has already passed. When should it run instead?")
    return Rule("once", at=when.isoformat(timespec="minutes")), t


def _safe_date(y: int, m: int, d: int) -> date:
    try:
        return date(y, m, d)
    except ValueError as e:
        raise ParseError(f"{d}/{m}/{y} is not a real date.") from e
