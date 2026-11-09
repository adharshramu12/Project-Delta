"""The student's academic context (year and semester from today's date and Settings > Student) and choosing dropdown
options by their text, never by their position (college portals list years unsorted). Everything here is made up."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from delta.tools.academic import AcademicContextTool, academic_context, from_settings, parse_year_label, year_start
from delta.tools.option_match import match_option, norm, years

# --- academic year and semester --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("day", "year", "sem", "sure"), [
    (date(2026, 11, 8), "2026-2027", "Odd", True),
    (date(2026, 7, 1), "2026-2027", "Odd", True),
    (date(2026, 12, 31), "2026-2027", "Odd", True),
    (date(2027, 1, 10), "2026-2027", "Even", True),
    (date(2027, 4, 30), "2026-2027", "Even", True),
    (date(2027, 5, 15), "2026-2027", "Even", False),  # exams at most colleges, a summer term at some: ask once
    (date(2027, 6, 10), "2027-2028", "Summer", False),  # the changeover: ask once
])
def test_year_and_semester_from_the_date(day, year, sem, sure):
    ctx = academic_context(day)
    assert (ctx.year, ctx.semester, ctx.sure) == (year, sem, sure)
    assert ctx.year_of_study == 0 and ctx.semester_number == 0  # nothing about the student: no number guessed


def test_year_of_study_moves_on_each_academic_year():
    # said "year 4" in 2026-2027: still 4 in Feb 2027, 5 would be next year (and is out of range for a B.Tech, kept)
    ctx = academic_context(date(2026, 11, 8), year_of_study=4, year_of_study_in="2026-2027")
    assert (ctx.year_of_study, ctx.semester_number) == (4, 7)
    assert "Semester 7" in ctx.semester_options and ctx.semester_options[0] == "Odd Sem"
    assert academic_context(date(2027, 2, 1), year_of_study=4, year_of_study_in="2026-2027").semester_number == 8
    assert academic_context(date(2027, 8, 1), year_of_study=3, year_of_study_in="2026-27").year_of_study == 4
    # from the year they joined (a July start)
    assert academic_context(date(2026, 11, 8), join_year=2023).year_of_study == 4
    assert academic_context(date(2026, 11, 8), join_year=2030).year_of_study == 0  # not yet joined: unknown
    assert academic_context(date(2027, 6, 2), year_of_study=2, year_of_study_in="2026-2027").semester_number == 0


def test_small_helpers():
    assert year_start(date(2026, 5, 31)) == 2025 and year_start(date(2026, 6, 1)) == 2026
    assert parse_year_label("AY 2026-27") == 2026 and parse_year_label("nothing") == 0


async def test_the_tool_reads_settings_and_says_when_to_ask():
    settings = SimpleNamespace(student=SimpleNamespace(join_year=0, year_of_study=4, year_of_study_in="2026-2027"))
    ctx = from_settings(settings, date(2026, 11, 8))
    assert ctx.line() == "Academic year 2026-2027, Odd semester, year 4 of study, semester 7"
    out = await AcademicContextTool(SimpleNamespace(student=None)).run(AcademicContextTool.Args())
    assert out.ok and "never by its place" in out.output
    assert "year of study is not set" in out.output


# --- options by text -------------------------------------------------------------------------------------------------

# Unsorted, as real portals list them: a newer year before an old one, a placeholder first, odd spacing.
YEARS = ["--Select--", "2027-2028", "2008-2009", "2026 - 2027 ", "2025-2026", "2016-2017"]
SEMS = ["Select Semester", "Even Sem", "Odd Sem", "Summer Term"]


def test_years_are_matched_by_text_whatever_the_order():
    assert match_option(YEARS, "2026-2027")[0] == 3
    assert match_option(YEARS, "2026-27")[0] == 3
    assert match_option(YEARS, "AY 2026-27")[0] == 3
    assert match_option(YEARS, "2026/2027")[0] == 3
    assert match_option(list(reversed(YEARS)), "2026-2027")[0] == 2  # the same option, wherever it is
    assert match_option(YEARS, "2030-2031") == (None, [])
    assert match_option(YEARS, "2026")[0] is None  # a bare year is not an academic year: never a guess


def test_semesters_and_wording():
    assert match_option(SEMS, "Odd Sem")[0] == 2
    assert match_option(SEMS, "odd semester")[0] == 2
    assert match_option(SEMS, "ODD")[0] == 2
    assert match_option(SEMS, "summer")[0] == 3
    assert match_option(["Semester 1", "Semester 7", "Semester 8"], "Semester 7")[0] == 1
    # "sem" alone fits two options: ambiguous, nothing chosen, both named
    idx, hits = match_option(SEMS, "sem")
    assert idx is None and set(hits) == {1, 2}
    # a placeholder is never chosen for a real request, only when it is asked for
    assert match_option(SEMS, "select")[0] == 0
    assert match_option(["--Select--", "Odd"], "--Select--")[0] == 0


def test_normalising():
    assert norm("  2026 \u2013 2027 : ") == "2026-2027"
    assert years("2026-27 and 2008-2009") == {"2026-2027", "2008-2009"}
    assert years("2026-2030") == set()


# --- Settings > Student ----------------------------------------------------------------------------------------------

def test_settings_student_year_is_saved_with_the_year_it_was_said_in(tmp_path, monkeypatch):
    from engine_helpers import no_registry
    from fastapi.testclient import TestClient

    from delta.api.server import create_app, load_token
    from delta.bootstrap import build
    from delta.config import Settings

    no_registry(monkeypatch, tmp_path)
    import delta.tools.user_browser as ub

    monkeypatch.setattr(ub, "default_browser", lambda prefer="auto": None)  # not what this test is about
    c = build(Settings(data_dir=tmp_path))
    token = load_token(tmp_path)
    with TestClient(create_app(c, token)) as tc:
        tc.headers["Authorization"] = f"Bearer {token}"
        _student_checks(tc, c, tmp_path)


def _student_checks(tc: Any, c: Any, tmp_path: Any) -> None:
    got = tc.get("/api/settings").json()["student"]
    assert got["join_year"] == 0 and got["year_of_study"] == 0 and got["now"].startswith("Academic year ")
    assert tc.put("/api/settings", json={"student": {"join_year": 1200}}).status_code == 400
    assert tc.put("/api/settings", json={"student": {"year_of_study": 9}}).status_code == 422
    ok = tc.put("/api/settings", json={"student": {"year_of_study": 3}})
    assert ok.status_code == 200
    from delta.tools.academic import year_label, year_start

    assert c.settings.student.year_of_study == 3
    assert c.settings.student.year_of_study_in == year_label(year_start(date.today()))
    assert "year 3 of study" in tc.get("/api/settings").json()["student"]["now"]
    from delta.config import read_user_settings

    assert read_user_settings(tmp_path)["student"]["year_of_study"] == 3  # the user's own settings file


def test_a_menu_path_asks_like_its_riskiest_click():
    from delta.tools.base import Risk
    from delta.tools.browser_actions import MENU_SPLIT, menu_risk

    assert MENU_SPLIT.split("Time Tables > My Timetable") == ["Time Tables", "My Timetable"]
    assert MENU_SPLIT.split("Academics › Attendance") == ["Academics", "Attendance"]
    assert menu_risk("Time Tables > My Timetable") == Risk.ACT
    assert menu_risk("Account > Delete account") != Risk.ACT
