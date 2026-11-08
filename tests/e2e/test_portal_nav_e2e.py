"""Inside a college portal after signing in, in a real Chrome/Edge: a long sidebar that has to be scrolled, a closed
"Time Tables" group with "My Timetable" in it, then Academic Year and Semester dropdowns whose options are NOT sorted
(2027-2028 comes before 2008-2009, the current year is written "2026 - 2027") and whose semesters arrive a moment
after the year is chosen, a Search button and "cannot be blank" checks. Everything is a made-up local page
(tests/e2e/site/portal_*.html). Run with DELTA_E2E=1 pytest -m e2e."""

from __future__ import annotations

import os
from datetime import date

import pytest

from delta.tools.academic import academic_context
from delta.tools.browser import BrowserClick, BrowserManager, BrowserOpen, OpenArgs, RefClickArgs
from delta.tools.browser_actions import BrowserMenu, BrowserSelect, MenuArgs, SelectArgs

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DELTA_E2E") != "1", reason="real-browser e2e: set DELTA_E2E=1"),
]


async def test_menu_path_through_a_long_sidebar_and_a_closed_group(mgr: BrowserManager, site: str):
    assert (await BrowserOpen(mgr).run(OpenArgs(url=f"{site}/portal_home.html"))).ok
    page = await mgr.page()
    # "Time Tables" is far down a sidebar that scrolls on its own (not the page)
    assert await page.evaluate("document.getElementById('sidebar').scrollTop") == 0
    r = await BrowserMenu(mgr).run(MenuArgs(path="Time Tables > My Timetable"))
    assert r.ok, r.output
    assert page.url.endswith("/portal_timetable.html")
    # one entry alone: its closed group is opened first
    assert (await BrowserOpen(mgr).run(OpenArgs(url=f"{site}/portal_home.html"))).ok
    r = await BrowserMenu(mgr).run(MenuArgs(path="My Timetable"))
    assert r.ok, r.output
    assert page.url.endswith("/portal_timetable.html")
    # a name that is not there: a plain reason, with the menu entries it can see
    bad = await BrowserMenu(mgr).run(MenuArgs(path="Fee Receipts"))
    assert not bad.ok and "no menu entry called 'Fee Receipts'" in bad.output and "Dashboard" in bad.output


async def test_timetable_year_and_semester_by_text_not_position(mgr: BrowserManager, site: str):
    assert (await BrowserOpen(mgr).run(OpenArgs(url=f"{site}/portal_timetable.html"))).ok
    page = await mgr.page()
    # Search with nothing chosen: the portal's own check
    assert (await BrowserClick(mgr).run(RefClickArgs(text="Search"))).ok
    assert "Academic Year cannot be blank." in await page.inner_text("#msg")

    ctx = academic_context(date(2026, 11, 8))  # Nov 2026: 2026-2027, odd semester, no question needed
    assert ctx.sure and (ctx.year, ctx.semester) == ("2026-2027", "Odd")
    r = await BrowserSelect(mgr).run(SelectArgs(label="Academic Year", option=ctx.year))
    assert r.ok, r.output
    assert await page.input_value("#ay") == "2026-2027"  # "2026 - 2027", not the first or the newest option
    # the semesters arrive 0.4 s after the year: the select waits for them, and "Odd Sem" is not the first one
    r = await BrowserSelect(mgr).run(SelectArgs(label="Semester", option=ctx.semester_options[1]))  # "Odd Semester"
    assert r.ok, r.output
    assert await page.input_value("#sem") == "1"
    assert (await BrowserClick(mgr).run(RefClickArgs(text="Search"))).ok
    assert await page.inner_text("#result") == "Timetable for 2026 - 2027 Odd Sem"
    # a year the portal does not list: nothing is chosen, the options are named
    bad = await BrowserSelect(mgr).run(SelectArgs(label="Academic Year", option="2031-2032"))
    assert not bad.ok and "2027-2028" in bad.output
    assert await page.input_value("#ay") == "2026-2027"
