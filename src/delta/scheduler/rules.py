"""When a schedule runs.

Times are kept the way people think about them: "every day at 09:00" in a time zone, not a fixed UTC
instant, so a daily 9 AM run stays at 9 AM local time when daylight saving starts or ends. Only the
computed next run is stored in UTC.

Daylight saving rules, the same as most calendar apps:
- A time that does not exist on a given day (clocks jump from 02:00 to 03:00 and the run is at 02:30)
  runs at the first moment after the jump, which is 03:30 new time on that day.
- A time that happens twice (clocks go back from 02:00 to 01:00 and the run is at 01:30) runs once, at
  the first 01:30.
- "Every N minutes or hours" counts real elapsed time, so "every 2 hours" stays 2 hours apart.

Time zones: an IANA name ("Asia/Kolkata", "Europe/London") through the standard library's zoneinfo, or
"" for this PC's own time zone (what Windows is set to). If a named zone cannot be loaded, the PC's own
zone is used instead of failing.
"""

from __future__ import annotations

import time as _time
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from datetime import time as dtime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIN_INTERVAL_MINUTES = 5  # "every N minutes" below this would keep the PC busy all day
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
SEARCH_DAYS = 366 * 8 + 2  # long enough for "29 February" cron lines

RuleType = Literal["once", "daily", "weekly", "interval", "cron"]


class RuleError(ValueError):
    """A rule that cannot work (bad time, interval too short, broken cron line). The message is for people."""


@dataclass
class Rule:
    type: RuleType
    at: str = ""  # once: local wall time "YYYY-MM-DDTHH:MM"
    time: str = ""  # daily / weekly: "HH:MM" (24 hour)
    days: list[int] = field(default_factory=list)  # weekly: 0 = Monday ... 6 = Sunday
    minutes: int = 0  # interval
    anchor: str = ""  # interval: UTC ISO instant the count starts from
    expr: str = ""  # cron: "minute hour day-of-month month day-of-week"

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in ("", [], 0) or k == "type"}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Rule:
        known = {k: data[k] for k in ("type", "at", "time", "days", "minutes", "anchor", "expr") if k in data}
        rule = cls(**known)
        rule.validate()
        return rule

    def validate(self) -> None:
        if self.type == "once":
            _parse_wall(self.at)
        elif self.type in ("daily", "weekly"):
            _parse_hhmm(self.time)
            if self.type == "weekly" and (not self.days or any(d not in range(7) for d in self.days)):
                raise RuleError("A weekly schedule needs at least one day of the week.")
        elif self.type == "interval":
            if int(self.minutes) < MIN_INTERVAL_MINUTES:
                raise RuleError(f"The shortest repeat is every {MIN_INTERVAL_MINUTES} minutes.")
            if self.anchor:
                _parse_utc(self.anchor)
        elif self.type == "cron":
            Cron.parse(self.expr)
        else:
            raise RuleError(f"Unknown schedule type '{self.type}'.")


# --- time zones -------------------------------------------------------------------------------------

def get_zone(name: str) -> tzinfo | None:
    """The named zone, or None for this PC's own zone (also when the name cannot be loaded)."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def zone_ok(name: str) -> bool:
    return not name or get_zone(name) is not None


def zone_label(name: str) -> str:
    if name and get_zone(name) is not None:
        return name
    return "this PC's time zone"


def to_utc(wall: datetime, tz: str) -> datetime:
    """A local wall-clock time in `tz` as a UTC instant. Times inside a DST gap move forward by the gap;
    repeated times take their first occurrence (see the module notes)."""
    wall = wall.replace(tzinfo=None, fold=0)
    zone = get_zone(tz)
    if zone is None:
        # The PC's own rules (Windows settings): mktime picks DST itself (tm_isdst=-1).
        secs = _time.mktime((*wall.timetuple()[:8], -1))
        return datetime.fromtimestamp(secs, UTC)
    return wall.replace(tzinfo=zone).astimezone(UTC)


def to_local(instant: datetime, tz: str) -> datetime:
    """A UTC instant as naive local wall-clock time in `tz`."""
    zone = get_zone(tz)
    aware = instant if instant.tzinfo else instant.replace(tzinfo=UTC)
    local = aware.astimezone(zone) if zone is not None else aware.astimezone()
    return local.replace(tzinfo=None)


def now_utc() -> datetime:
    return datetime.now(UTC)


def _parse_hhmm(value: str) -> dtime:
    try:
        h, m = value.split(":")
        return dtime(int(h), int(m))
    except (ValueError, AttributeError) as e:
        raise RuleError(f"'{value}' is not a time like 09:30.") from e


def _parse_wall(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None, second=0, microsecond=0)
    except (ValueError, TypeError) as e:
        raise RuleError(f"'{value}' is not a date and time.") from e


def _parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def iso(instant: datetime | None) -> str | None:
    return instant.astimezone(UTC).isoformat(timespec="seconds") if instant else None


def from_iso(value: str | None) -> datetime | None:
    return _parse_utc(value) if value else None


# --- cron -------------------------------------------------------------------------------------------

_CRON_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
    "nov": 11, "dec": 12, "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}
_CRON_FIELDS = [("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12),
                ("day of week", 0, 7)]


@dataclass(frozen=True)
class Cron:
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    doms: frozenset[int]
    months: frozenset[int]
    dows: frozenset[int]  # cron numbering: 0 = Sunday
    dom_any: bool
    dow_any: bool

    @classmethod
    def parse(cls, expr: str) -> Cron:
        parts = (expr or "").split()
        if len(parts) != 5:
            raise RuleError("A cron line has 5 parts: minute hour day-of-month month day-of-week, "
                            "e.g. '0 9 * * 1-5' for 9 AM on weekdays.")
        sets = [_cron_field(p, name, lo, hi) for p, (name, lo, hi) in zip(parts, _CRON_FIELDS, strict=True)]
        dows = frozenset(0 if d == 7 else d for d in sets[4])
        return cls(tuple(sorted(sets[0])), tuple(sorted(sets[1])), frozenset(sets[2]), frozenset(sets[3]), dows,
                   parts[2] == "*", parts[4] == "*")

    def day_matches(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom_ok = d.day in self.doms
        dow_ok = ((d.weekday() + 1) % 7) in self.dows
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow_ok
        if self.dow_any:
            return dom_ok
        return dom_ok or dow_ok  # both restricted: either one matches (standard cron)


def _cron_field(text: str, name: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for item in text.lower().split(","):
        base, _, step_s = item.partition("/")
        try:
            step = int(step_s) if step_s else 1
            if base == "*":
                start, end = lo, hi
            elif "-" in base:
                a, b = base.split("-", 1)
                start, end = _cron_value(a, lo), _cron_value(b, lo)
            else:
                start = _cron_value(base, lo)
                end = hi if step_s else start
        except ValueError as e:
            raise RuleError(f"Cron {name} '{text}' is not valid.") from e
        if step < 1 or start < lo or end > hi or start > end:
            raise RuleError(f"Cron {name} '{text}' is out of range ({lo}-{hi}).")
        out.update(range(start, end + 1, step))
    return out


def _cron_value(text: str, lo: int) -> int:
    return _CRON_NAMES[text] if text in _CRON_NAMES else int(text)


# --- next run -----------------------------------------------------------------------------------------

def next_run(rule: Rule, tz: str, after: datetime) -> datetime | None:
    """The first run strictly after `after` (UTC), or None when the schedule will never run again."""
    after = after if after.tzinfo else after.replace(tzinfo=UTC)
    if rule.type == "once":
        at = to_utc(_parse_wall(rule.at), tz)
        return at if at > after else None
    if rule.type == "interval":
        step = timedelta(minutes=int(rule.minutes))
        anchor = _parse_utc(rule.anchor) if rule.anchor else after
        if after < anchor:
            return anchor
        k = int((after - anchor) / step) + 1
        return anchor + k * step
    if rule.type == "cron":
        cron = Cron.parse(rule.expr)
        times = [dtime(h, m) for h in cron.hours for m in cron.minutes]
        return _scan(tz, after, cron.day_matches, times)
    t = _parse_hhmm(rule.time)
    if rule.type == "daily":
        return _scan(tz, after, lambda d: True, [t])
    days = set(rule.days)
    return _scan(tz, after, lambda d: d.weekday() in days, [t])


def _scan(tz: str, after: datetime, day_ok: Any, times: list[dtime]) -> datetime | None:
    start = to_local(after, tz).date() - timedelta(days=1)  # a DST shift can move a run across midnight
    for i in range(SEARCH_DAYS):
        d = start + timedelta(days=i)
        if not day_ok(d):
            continue
        for t in times:
            instant = to_utc(datetime.combine(d, t), tz)
            if instant > after:
                return instant
    return None


# --- plain words ----------------------------------------------------------------------------------------

def fmt_clock(t: dtime) -> str:
    hour = t.hour % 12 or 12
    return f"{hour}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def fmt_when(instant: datetime | None, tz: str) -> str:
    """'Fri 7 Nov 2026, 9:00 AM' in the schedule's own zone."""
    if instant is None:
        return "never (finished)"
    local = to_local(instant, tz)
    return f"{DAY_NAMES[local.weekday()][:3]} {local.day} {MONTH_NAMES[local.month - 1]} {local.year}, " \
           f"{fmt_clock(local.time())}"


def _days_words(days: list[int]) -> str:
    s = sorted(set(days))
    if s == [0, 1, 2, 3, 4]:
        return "weekday"
    if s == [5, 6]:
        return "Saturday and Sunday"
    if len(s) == 7:
        return "day"
    names = [DAY_NAMES[d] for d in s]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def describe(rule: Rule, tz: str) -> str:
    """'every day at 9:00 AM', 'every Monday at 10:00 AM', 'every 30 minutes', 'once on ...'."""
    if rule.type == "once":
        wall = _parse_wall(rule.at)
        return f"once on {DAY_NAMES[wall.weekday()][:3]} {wall.day} {MONTH_NAMES[wall.month - 1]} {wall.year} " \
               f"at {fmt_clock(wall.time())}"
    if rule.type == "daily":
        return f"every day at {fmt_clock(_parse_hhmm(rule.time))}"
    if rule.type == "weekly":
        return f"every {_days_words(rule.days)} at {fmt_clock(_parse_hhmm(rule.time))}"
    if rule.type == "interval":
        m = int(rule.minutes)
        if m % 60 == 0:
            h = m // 60
            return "every hour" if h == 1 else f"every {h} hours"
        return f"every {m} minutes"
    return f"on the cron schedule '{rule.expr}'"
