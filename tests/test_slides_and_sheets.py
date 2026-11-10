"""make_presentation (.pptx) and make_spreadsheet (.xlsx): the outline reader and its limits, files that are valid
packages, the formula rules, and the tools (one model call for a deck, none for a workbook, delivery as documents)."""

import re
import xml.dom.minidom
import zipfile
from pathlib import Path

import pytest

from delta.addons.writer.document import list_documents
from delta.addons.writer.render_pptx import render_pptx
from delta.addons.writer.render_xlsx import Sheet, col_letter, render_xlsx, safe_formula, sheet_names
from delta.addons.writer.slides import MAX_POINTS, MAX_SLIDES, MAX_TABLE_COLS, MAX_TABLE_ROWS, parse_outline
from delta.addons.writer.tools import (
    MakePresentation,
    MakePresentationArgs,
    MakeSpreadsheet,
    MakeSpreadsheetArgs,
    SheetArgs,
    parse_table,
    writer_tools,
)
from delta.attachments.ooxml import read_pptx
from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.tools.base import Risk, ToolResult

OUTLINE = """# ARVA Talks Academy
Learn AI agents by building real projects

## Why we exist
- Most AI courses are **videos**, not practice
  - No portfolio at the end
- We teach by building
> Open with a student's story.

## Plans
| Plan | Weeks | Projects |
|---|---|---|
| Starter | 4 | 2 |
| Pro & <Team> | 12 | 9 |
> Compare, do not read aloud.

## Next steps
1. Visit the site
2. Join the next cohort
"""


class Cfg:
    def __init__(self, folder):
        self.output_dir = str(folder)
        self.model_task = "plan"


class FakeRouter:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    async def complete(self, messages, task="chat", **kw):
        self.calls += 1
        return type("R", (), {"content": self.text})()


class FakeExecutor:
    def __init__(self):
        self.calls = []

    async def execute(self, name, args, task_id=""):
        self.calls.append((name, args, task_id))
        return ToolResult(True, "sent", {"delivered": True})


class FakeEvents:
    def __init__(self):
        self.events = []

    async def publish(self, event, **data):
        self.events.append((event, data))


def well_formed(path: Path) -> dict[str, str]:
    """Every part parses as XML, and every relationship points at a part that exists."""
    with zipfile.ZipFile(path) as z:
        parts = {n: z.read(n).decode("utf-8") for n in z.namelist()}
    for name, text in parts.items():
        xml.dom.minidom.parseString(text)
        if name.endswith(".rels"):
            base = Path(name).parent.parent
            for target in re.findall(r'Target="([^"]+)"', text):
                resolved = Path(*[p for p in (base / target).as_posix().split("/")])
                stack: list[str] = []
                for piece in resolved.as_posix().split("/"):
                    stack.pop() if piece == ".." else stack.append(piece)
                assert "/".join(stack) in parts, f"{name} points at missing {target}"
    for override in re.findall(r'PartName="/([^"]+)"', parts["[Content_Types].xml"]):
        assert override in parts, f"content type for missing part {override}"
    return parts


# --- the outline ---------------------------------------------------------------------------------------


def test_outline_becomes_title_points_levels_tables_and_notes():
    deck = parse_outline(OUTLINE)
    assert deck.title == "ARVA Talks Academy" and deck.subtitle == "Learn AI agents by building real projects"
    why, plans, nxt = deck.slides
    assert why.points == [(0, "Most AI courses are **videos**, not practice"), (1, "No portfolio at the end"),
                          (0, "We teach by building")]
    assert why.notes == "Open with a student's story."
    assert plans.table[0] == ["Plan", "Weeks", "Projects"] and len(plans.table) == 3 and not plans.points
    assert nxt.points == [(0, "Visit the site"), (0, "Join the next cohort")]


def test_long_slides_continue_and_nothing_is_dropped_silently():
    points = "\n".join(f"- point {i}" for i in range(MAX_POINTS * 2 + 1))
    rows = "\n".join(f"| r{i} | {i} |" for i in range(MAX_TABLE_ROWS + 2))
    wide = "| " + " | ".join(f"c{i}" for i in range(MAX_TABLE_COLS + 3)) + " |"
    deck = parse_outline(f"# T\n## Many\n{points}\n## Table\n| a | b |\n|---|---|\n{rows}\n## Wide\n{wide}\n")
    titles = [s.title for s in deck.slides]
    assert titles == ["Many", "Many (2)", "Many (3)", "Table", "Table (2)", "Wide"]
    assert all(len(s.points) <= MAX_POINTS for s in deck.slides)
    assert deck.slides[4].table[0] == ["a", "b"]  # the header row is repeated on the continuation
    assert len(deck.slides[5].table[0]) == MAX_TABLE_COLS and "columns" in deck.notes[0]
    assert sum(len(s.points) for s in deck.slides) == MAX_POINTS * 2 + 1  # every point is still there
    huge = parse_outline("# T\n" + "\n".join(f"## S{i}\n- x" for i in range(MAX_SLIDES + 5)))
    assert len(huge.slides) == MAX_SLIDES and "only the first" in huge.notes[-1]


def test_plain_text_and_a_given_title_still_make_a_deck():
    deck = parse_outline("Just one thought with no headings", title="My talk")
    assert deck.title == "My talk" and deck.slides[0].points == [(0, "Just one thought with no headings")]
    assert parse_outline("").slides == []


# --- the .pptx file ------------------------------------------------------------------------------------


def test_pptx_is_a_valid_package_with_real_placeholders_tables_and_notes(tmp_path):
    out = render_pptx(parse_outline(OUTLINE), tmp_path / "deck.pptx")
    parts = well_formed(out)
    assert sum(1 for n in parts if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)) == 4  # title + 3
    assert 'type="ctrTitle"' in parts["ppt/slides/slide1.xml"]
    two = parts["ppt/slides/slide2.xml"]
    assert 'type="title"' in two and '<a:pPr lvl="1"/>' in two and 'b="1"' in two and "**" not in two
    assert "<a:tbl>" in parts["ppt/slides/slide3.xml"] and "Pro &amp; &lt;Team&gt;" in parts["ppt/slides/slide3.xml"]
    assert "Open with a student" in parts["ppt/notesSlides/notesSlide2.xml"]
    assert "ppt/notesSlides/notesSlide4.xml" not in parts  # no notes written, no notes part
    assert 'type="slidenum"' in two and 'type="slidenum"' not in parts["ppt/slides/slide1.xml"]
    assert not any("vbaProject" in n or n.endswith(".bin") for n in parts)


def test_delta_can_read_back_the_deck_it_made(tmp_path):
    out = render_pptx(parse_outline(OUTLINE), tmp_path / "deck.pptx")
    doc = read_pptx(out)
    text = "\n".join(f"{p.title}\n{p.text}" for p in doc.parts)
    assert doc.total == 4 and "ARVA Talks Academy" in text and "We teach by building" in text and "Starter" in text


def test_a_deck_without_notes_has_no_notes_master(tmp_path):
    parts = well_formed(render_pptx(parse_outline("# T\n## One\n- a\n"), tmp_path / "d.pptx"))
    assert not any("notes" in n for n in parts) and "notesMasterIdLst" not in parts["ppt/presentation.xml"]


def test_control_characters_never_break_the_file(tmp_path):
    well_formed(render_pptx(parse_outline("# T\x00\x0b\n## One\x01\n- a\x1f & b <c>\n> n\x02\n"), tmp_path / "d.pptx"))
    well_formed(render_xlsx([Sheet("S\x00", ["a\x01"], [["x\x0by", 1]])], tmp_path / "s.xlsx", "t\x00"))


# --- formulas and the .xlsx file -----------------------------------------------------------------------


@pytest.mark.parametrize("formula", ["=SUM(B2:B4)", "=B2*C2", "=ROUND($B$2/3,2)", '=IF(B2>10,"high","low")',
                                     "=1+1", "=AVERAGE(A1:A9)-MIN(A1:A9)", "= -B2 * 10%", "=(C2+D2)/2",
                                     "=IF(AND(B2>=1,C2<>0),TRUE,FALSE)", '=SUMIF(A2:A9,">5",B2:B9)', "=2^3"])
def test_arithmetic_formulas_are_allowed(formula):
    assert safe_formula(formula)


@pytest.mark.parametrize("formula", ['=HYPERLINK("http://x","click")', "=cmd|'/c calc'!A1", "=WEBSERVICE(A1)",
                                     "=[other.xlsx]Sheet1!A1", "=Sheet2!A1", "=EXEC(A1)", "=INDIRECT(A1)",
                                     '=IF(A1,"unbalanced)', "=SUM(A1:A9);B1", "=" + "1+" * 300 + "1",
                                     # not a complete formula: Excel would call the whole file damaged
                                     "=1+", "=SUM(B2:B4", "=SUM()", "=A1 B1", "=*2", "=(1+2))", "=SUM(A1:)", "=",
                                     "=TRUE(1)", "=A1:B2:C3", "=" + "(" * 40 + "1" + ")" * 40])
def test_anything_else_stays_text(formula):
    assert not safe_formula(formula)


def test_xlsx_cells_are_typed_and_text_can_never_run(tmp_path):
    rows = [["Laptops", 2026, 4, "65,000.50", "=C2*D2", "40%", '=HYPERLINK("http://evil")'],
            ["Chairs & <desks>", "2026", 10, 4200, "=C3*D3", "12.5%", "+91 98765 43210"],
            ["Wi-Fi", 2026, 1, 1499.99, "=C4*D4", "3%", "0123"], ["Ready", True, None, "", "@cmd", "-x", "=1+"]]
    out = render_xlsx([Sheet("Budget", ["Item", "Year", "Qty", "Rate", "Amount", "Share", "Note"], rows, totals=True)],
                      tmp_path / "b.xlsx", "Budget")
    sheet = well_formed(out)["xl/worksheets/sheet1.xml"]
    cells = dict(re.findall(r'<c r="([A-Z]+\d+)"[^>]*?(?:/>|>(.*?)</c>)', sheet))
    assert cells["D2"] == "<v>65000.5</v>" and cells["B3"] == "<v>2026</v>" and cells["F3"] == "<v>0.125</v>"
    assert cells["E2"] == "<f>C2*D2</f>"
    for ref in ("G2", "G3", "G4", "E5", "F5", "G5"):  # a blocked formula, a phone number, a code, @ - and a broken =
        assert "<is><t" in cells[ref] and "<f>" not in cells[ref], ref
    assert "0123" in cells["G4"] and "Yes" in cells["B5"]
    assert "Chairs &amp; &lt;desks&gt;" in sheet and 'state="frozen"' in sheet and '<autoFilter ref="A1:G5"/>' in sheet
    assert cells["A6"].count("Total") == 1 and cells["C6"] == "<f>SUM(C2:C5)</f>"
    assert "SUM(B2" not in sheet and "SUM(D2" not in sheet  # a Year or a Rate column is never added up


def test_sheet_names_follow_excels_rules():
    names = sheet_names([Sheet("Notes/[x]", []), Sheet("notes x", []), Sheet("", []), Sheet("a" * 50, [])])
    assert names == ["Notes x", "notes x (2)", "Sheet3", "a" * 31]
    assert [col_letter(i) for i in (0, 25, 26, 701, 702)] == ["A", "Z", "AA", "ZZ", "AAA"]


# --- the tools -----------------------------------------------------------------------------------------


async def test_one_model_call_makes_the_whole_deck_and_it_is_listed_as_a_document(tmp_path):
    router, events = FakeRouter(OUTLINE), FakeEvents()
    tool = MakePresentation(router, Cfg(tmp_path), events)
    token = CURRENT_REQUEST.set(TaskRequest("make a deck", channel="api"))
    try:
        r = await tool.run(MakePresentationArgs(content="ARVA Talks Academy teaches AI agents by building."))
    finally:
        CURRENT_REQUEST.reset(token)
    assert r.ok and router.calls == 1 and r.evidence["slides"] == 4 and r.evidence["verified"]
    path = Path(r.evidence["path"])
    assert path.suffix == ".pptx" and path.parent == tmp_path and "2. Why we exist" in r.output
    assert events.events[0][0] == "document.created" and events.events[0][1]["files"] == [path.name]
    assert [d["type"] for d in list_documents(tmp_path)] == ["pptx"]


async def test_writing_rules_apply_to_slides(tmp_path):
    outline = "# Plan\n## Goals\n- We will organize mocks — every week\n> Keep it short.\n"
    tool = MakePresentation(None, Cfg(tmp_path))
    r = await tool.run(MakePresentationArgs(content=outline, write_for_me=False))
    slide = well_formed(Path(r.evidence["path"]))["ppt/slides/slide2.xml"]
    assert r.ok and "organise" in slide and "—" not in slide


async def test_without_a_model_an_outline_is_still_built_and_nothing_makes_no_file(tmp_path):
    tool = MakePresentation(None, Cfg(tmp_path))
    r = await tool.run(MakePresentationArgs(content=OUTLINE))
    assert r.ok and "as given" in r.output and r.evidence["slides"] == 4
    empty = await tool.run(MakePresentationArgs(content="   ", write_for_me=False))
    assert not empty.ok and len(list(tmp_path.iterdir())) == 1


async def test_a_spreadsheet_needs_no_model_and_goes_back_to_telegram_through_send_file(tmp_path):
    tool = MakeSpreadsheet(None, Cfg(tmp_path))
    tool.executor = FakeExecutor()
    req = TaskRequest("make a sheet", channel="telegram", user="42")
    token = CURRENT_REQUEST.set(req)
    try:
        r = await tool.run(MakeSpreadsheetArgs(title="Costs", totals=True,
                                               table="Item,Amount\nRent,12000\nBad,=WEBSERVICE(A1)"))
    finally:
        CURRENT_REQUEST.reset(token)
    assert r.ok and r.evidence["delivered"] and r.evidence["sheets"] == ["Costs"] and r.evidence["rows"] == 2
    name, args, task_id = tool.executor.calls[0]
    assert name == "send_file" and task_id == req.id and args["path"].endswith(".xlsx")  # via the executor
    assert "kept as plain text" in r.output


def test_a_table_is_read_from_csv_tabs_semicolons_or_markdown():
    want = (["Item", "Qty", "Cost"], [["Pen", "2", "=ROUND(B2*1.5,2)"], ["Book, hard", "1", "300"]])
    assert parse_table('Item,Qty,Cost\nPen,2,=ROUND(B2*1.5,2)\n"Book, hard",1,300') == want  # a formula's own comma
    assert parse_table("Item\tQty\tCost\nPen\t2\t=ROUND(B2*1.5,2)\nBook, hard\t1\t300") == want
    markdown = "| Item | Qty | Cost |\n|---|:-:|--:|\n| Pen | 2 | =ROUND(B2*1.5,2) |\n| Book, hard | 1 | 300 |"
    assert parse_table(markdown) == want
    assert parse_table("Item;Qty\nPen;2\n\n") == (["Item", "Qty"], [["Pen", "2"]])
    assert parse_table("```csv\nItem,Qty\nPen,2\n```") == (["Item", "Qty"], [["Pen", "2"]])
    assert parse_table("  \n") == ([], [])


async def test_a_workbook_can_have_more_tabs_and_an_empty_table_makes_no_file(tmp_path):
    tool = MakeSpreadsheet(None, Cfg(tmp_path))
    people = SheetArgs(name="People", table="| Name | Role |\n|---|---|\n| Asha | Lead |")
    r = await tool.run(MakeSpreadsheetArgs(title="Plan", table="Task,Hours\nBuild,6\nTest,2.5", totals=True,
                                           more_sheets=[people]))
    assert r.ok and r.evidence["sheets"] == ["Plan", "People"] and r.evidence["rows"] == 3
    parts = well_formed(Path(r.evidence["path"]))
    assert "<f>SUM(B2:B3)</f>" in parts["xl/worksheets/sheet1.xml"] and "Asha" in parts["xl/worksheets/sheet2.xml"]
    empty = await tool.run(MakeSpreadsheetArgs(title="Nothing"))
    assert not empty.ok and "CSV" in empty.output and len(list(tmp_path.iterdir())) == 1


async def test_the_plan_survives_a_model_that_wraps_it_in_a_list():
    from types import SimpleNamespace

    from delta.core.planner import make_plan

    class Router:
        def __init__(self, reply):
            self.reply = reply

        async def complete(self, messages, **kw):
            return SimpleNamespace(content=self.reply)

    wrapped = ('[{"needs_tools": true, "goal": "Make a deck", '
               '"direct": {"tool": "make_presentation", "args": {"content": "x"}}}]')
    plan = await make_plan(Router(wrapped), "make a deck about x", ["make_presentation"], "", "Delta")
    assert plan.direct == {"tool": "make_presentation", "args": {"content": "x"}} and plan.goal == "Make a deck"
    for nonsense in ('[1, 2]', '"just text"', '[{"a": 1}, {"b": 2}]'):  # not a plan: act on the request itself
        plan = await make_plan(Router(nonsense), "make a deck about x", ["make_presentation"], "", "Delta")
        assert plan.needs_tools and plan.goal == "make a deck about x" and plan.direct is None


def test_both_tools_write_only_in_the_documents_folder_and_follow_the_writer_switch(tmp_path):
    deck, sheet = MakePresentation(None, Cfg(tmp_path)), MakeSpreadsheet(None, Cfg(tmp_path))
    assert deck.risk == Risk.WRITE and sheet.risk == Risk.WRITE
    assert deck.paths(MakePresentationArgs(content="x")) == [str(tmp_path)]
    assert sheet.paths(MakeSpreadsheetArgs(title="t", table="a\n1")) == [str(tmp_path)]
    names = [t.name for t in writer_tools(None, Cfg(tmp_path))]
    assert "make_presentation" in names and "make_spreadsheet" in names
    off = Cfg(tmp_path)
    off.enabled = False
    assert writer_tools(None, off) == []


def test_never_overwrites_an_existing_file(tmp_path):
    from delta.addons.writer.document import unique_stem

    first = unique_stem(tmp_path, "Budget")
    (tmp_path / f"{first}.xlsx").write_bytes(b"x")
    assert unique_stem(tmp_path, "Budget") != first
