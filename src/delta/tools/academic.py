"""The student's academic context, for requests inside a college portal ("show my timetable", "my attendance"): which
academic year and semester "now" is, so Delta picks them in the portal's dropdowns without asking.

Indian colleges run the academic year from about June/July to May/June: November 2026 is in 2026-2027. Semesters:

  Odd     about July to December (semesters 1, 3, 5, 7)
  Even    about January to May (semesters 2, 4, 6, 8)
  Summer  a short term in the break (May / June), at some colleges only

June is the changeover (last year's summer term or this year's start), and May is exams for one and a summer term for
another, so in those months the answer is marked not sure and the user is asked once. Everything else follows from
today's date alone. The student's year of study (Settings > Student: the year they joined, or their year of study
now) only adds the semester NUMBER (year 4 in an odd semester is semester 7), which some portals list instead of
"Odd". Nothing here is sent anywhere; the settings stay in the user's own settings file.

Portals list years in no useful order (2027-2028 can come before 2008-2009) and write them in many ways (2026-27,
2026 - 2027, AY 2026-27): option_variants gives the ways to look for, and delta.tools.option_match picks the option
by its text, never by its position in the list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import BaseModel

from delta.tools.base import Risk, Tool, ToolResult

YEAR_START_MONTH = 6  # June: from here on it is the new academic year (its first weeks are the unsure changeover)


def year_start(day: date) -> int:
    """The calendar year the academic year containing `day` began in (Nov 2026 -> 2026, Feb 2027 -> 2026)."""
    return day.year if day.month >= YEAR_START_MONTH else day.year - 1


def year_label(start: int) -> str:
    return f"{start}-{start + 1}"


def parse_year_label(text: str) -> int:
    """'2026-2027', '2026-27', '2026' -> 2026; 0 when it is not an academic year."""
    import re

    m = re.search(r"(20\d\d|19\d\d)", text or "")
    return int(m[1]) if m else 0


@dataclass
class AcademicContext:
    year: str  # "2026-2027"
    start: int  # 2026
    semester: str  # Odd | Even | Summer
    sure: bool
    why: str = ""
    year_of_study: int = 0  # 0: not known
    semester_number: int = 0  # 0: not known (no year of study, or a summer term)
    year_options: list[str] = field(default_factory=list)  # ways a portal may write the year
    semester_options: list[str] = field(default_factory=list)  # ways a portal may write the semester

    def line(self) -> str:
        parts = [f"Academic year {self.year}", f"{self.semester} semester"]
        if self.year_of_study:
            parts.append(f"year {self.year_of_study} of study")
        if self.semester_number:
            parts.append(f"semester {self.semester_number}")
        return ", ".join(parts)


def semester_for(day: date) -> tuple[str, bool, str]:
    """(Odd | Even | Summer, sure, why) for a date."""
    m = day.month
    if 7 <= m <= 12:
        return "Odd", True, "July to December is the odd semester"
    if 1 <= m <= 4:
        return "Even", True, "January to April is the even semester"
    if m == 5:
        return "Even", False, ("May is the end of the even semester at most colleges and a summer term at some; "
                               "ask the user which one if the portal has both")
    return "Summer", False, ("June is the changeover: a summer term at some colleges, the start of the new year at "
                             "others; ask the user which one")


def year_options(start: int) -> list[str]:
    end = start + 1
    return [f"{start}-{end}", f"{start}-{str(end)[2:]}", f"{start} - {end}", f"{start}/{end}", f"AY {start}-{end}"]


def semester_options(semester: str, number: int = 0) -> list[str]:
    base = {"Odd": ["Odd Sem", "Odd Semester", "Odd"], "Even": ["Even Sem", "Even Semester", "Even"],
            "Summer": ["Summer Term", "Summer Semester", "Summer"]}[semester]
    if number:
        base = [*base, f"Semester {number}", f"Sem {number}", f"Sem-{number}", str(number)]
    return base


def academic_context(today: date, join_year: int = 0, year_of_study: int = 0, year_of_study_in: str = "") -> \
        AcademicContext:
    """The academic year and semester on `today`, and (when the student told Delta) their year of study and semester
    number. join_year: the calendar year they started (a July start). year_of_study + year_of_study_in: their year of
    study and the academic year they said it in; it moves on by one each academic year after that."""
    start = year_start(today)
    sem, sure, why = semester_for(today)
    yos = 0
    if 1 <= year_of_study <= 8:
        said_in = parse_year_label(year_of_study_in) or start
        yos = year_of_study + (start - said_in)
    elif join_year and 1950 < join_year <= start:
        yos = start - join_year + 1
    if not 1 <= yos <= 8:
        yos = 0
    number = 2 * (yos - 1) + (1 if sem == "Odd" else 2) if yos and sem != "Summer" else 0
    return AcademicContext(year_label(start), start, sem, sure, why, yos, number, year_options(start),
                           semester_options(sem, number))


def from_settings(settings: Any, today: date | None = None) -> AcademicContext:
    st = getattr(settings, "student", None)
    return academic_context(today or date.today(), int(getattr(st, "join_year", 0) or 0),
                            int(getattr(st, "year_of_study", 0) or 0), str(getattr(st, "year_of_study_in", "") or ""))


class AcademicArgs(BaseModel):
    pass


class AcademicContextTool(Tool[AcademicArgs]):
    """The tool the planner calls before picking a year or semester in a college portal."""

    name = "academic_context"
    description = ("The student's current academic year and semester (Indian academic year, June to May; odd "
                   "semester July-December, even January-May) from today's date and Settings > Student. Call it "
                   "before choosing an academic year or semester in a college portal; choose the options by their "
                   "text with browser_select. If it says not sure, ask the user once.")
    risk = Risk.READ
    Args = AcademicArgs

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def summary(self, args: AcademicArgs) -> str:
        return "work out the current academic year and semester"

    async def run(self, args: AcademicArgs) -> ToolResult:
        ctx = from_settings(self.settings)
        text = (f"{ctx.line()}. In the portal choose the academic year '{ctx.year}' (it may be written "
                f"{', '.join(repr(o) for o in ctx.year_options[1:3])}) and the semester '{ctx.semester_options[0]}'"
                + (f" (or 'Semester {ctx.semester_number}' if the list uses numbers)" if ctx.semester_number else "")
                + ". Pick by the option's text, never by its place in the list.")
        if not ctx.sure:
            text += f" NOT SURE: {ctx.why}."
        if not ctx.year_of_study:
            text += (" The student's year of study is not set (Settings > Student); if the portal asks for a "
                     "semester number, ask the user once.")
        return ToolResult(True, text, {"year": ctx.year, "semester": ctx.semester, "sure": ctx.sure,
                                       "year_of_study": ctx.year_of_study, "semester_number": ctx.semester_number})


__all__ = ["AcademicContext", "AcademicContextTool", "academic_context", "from_settings", "parse_year_label",
           "semester_for", "semester_options", "year_label", "year_options", "year_start"]
