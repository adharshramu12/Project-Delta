"""Sheets -> .xlsx with nothing but the standard library (zipfile + SpreadsheetML).

openpyxl is not needed for what a clean workbook needs: typed cells (numbers stay numbers, so sums and charts
work), a bold header row that stays in view, column widths from the content, a filter on the header, an optional
totals row with real SUM formulas, and safe formulas.

Safety: text is always stored as text (an inline string), so a cell that starts with = + - or @ can never run as a
formula. Only cells that pass `safe_formula` are written as formulas: arithmetic over cell references with a short
list of functions. Anything else (HYPERLINK, WEBSERVICE, DDE, references to other files) stays text.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
MAX_ROWS, MAX_COLS, MAX_SHEETS, MAX_CELL = 50_000, 60, 12, 8_000

SAFE_FUNCTIONS = frozenset({"SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "ROUND", "ROUNDUP", "ROUNDDOWN", "ABS",
                            "IF", "AND", "OR", "NOT", "SUMIF", "COUNTIF", "AVERAGEIF", "MEDIAN", "INT", "MOD"})
MAX_FORMULA = 400
# formula tokens: a number, a "string", a cell (A1, $B$2), a word (function name, TRUE, FALSE), or one operator
_TOKEN = re.compile(r'\s*(?:(?P<num>\d+(?:\.\d+)?)|(?P<str>"[^"]*")'
                    r"|(?P<cell>\$?[A-Za-z]{1,3}\$?\d{1,7})(?![A-Za-z0-9_.])"
                    r"|(?P<word>[A-Za-z]+)|(?P<op><>|<=|>=|[-+*/^(),:<>=%]))")
_NUMBER = re.compile(r"^-?(?:\d{1,15}|\d{1,3}(?:,\d{2,3})+)(?:\.\d{1,10})?$")
_PERCENT = re.compile(r"^-?\d{1,6}(?:\.\d{1,6})?\s?%$")
_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
_SHEET_NAME_BAD = re.compile(r"[\[\]:*?/\\]")
# columns that hold numbers nobody adds up: a totals row leaves them empty
_NOT_SUMMED = re.compile(r"(?:\b(?:year|yr|id|(?:s|sr|roll)\.?\s?no|number|phone|mobile|pin|code|roll|rank|age|date|"
                         r"month|day|rate|price|percent|percentage|share)\b|%)", re.I)

# cell styles (cellXfs index)
PLAIN, HEADER, NUM, DEC, PCT, TOTAL, TOTAL_NUM, TOTAL_DEC, PCT_WHOLE = range(9)


@dataclass
class Sheet:
    name: str
    columns: list[str]
    rows: list[list[Any]] = field(default_factory=list)
    totals: bool = False


def safe_formula(text: str) -> bool:
    """True only for a complete, well-formed formula made of numbers, "text", operators, cell references on this
    sheet and SAFE_FUNCTIONS. It is parsed, not pattern-matched: a formula Excel could not read (an open bracket, a
    dangling operator) would make Excel report the whole file as damaged, so it is kept as text instead."""
    if not text.startswith("=") or len(text) > MAX_FORMULA:
        return False
    tokens: list[tuple[str, str]] = []
    pos = 1
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        m = _TOKEN.match(text, pos)
        if m is None:
            return False
        kind = str(m.lastgroup)
        tokens.append((kind, m.group(kind)))
        pos = m.end()
    i = 0

    def peek() -> str:
        return tokens[i][1] if i < len(tokens) and tokens[i][0] == "op" else ""

    def primary(depth: int) -> bool:
        nonlocal i
        if i >= len(tokens) or depth > 30:
            return False
        kind, value = tokens[i]
        i += 1
        if kind in ("num", "str"):
            return True
        if kind == "cell":
            if peek() == ":":  # a range: A1:B9
                i += 1
                if i >= len(tokens) or tokens[i][0] != "cell":
                    return False
                i += 1
            return True
        if kind == "word":
            if value.upper() in ("TRUE", "FALSE") and peek() != "(":
                return True
            if value.upper() not in SAFE_FUNCTIONS or peek() != "(":
                return False
            i += 1
            while True:
                if not expr(depth + 1):
                    return False
                if peek() == ",":
                    i += 1
                    continue
                break
        elif value == "(":
            if not expr(depth + 1):
                return False
        else:
            return False
        if peek() != ")":
            return False
        i += 1
        return True

    def expr(depth: int) -> bool:
        nonlocal i
        while True:
            while peek() in ("-", "+"):  # unary signs
                i += 1
            if not primary(depth):
                return False
            while peek() == "%":
                i += 1
            if peek() in ("+", "-", "*", "/", "^", "=", "<>", "<=", ">=", "<", ">"):
                i += 1
                continue
            return True

    return bool(tokens) and expr(0) and i == len(tokens)


def col_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    out = ""
    n = index + 1
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _clean(text: str) -> str:
    return escape(_BAD.sub("", text[:MAX_CELL]))


def _typed(value: Any) -> tuple[str, Any]:
    """('n', number) | ('p', fraction) | ('f', formula) | ('s', text) | ('', None) for an empty cell."""
    if value is None or value == "":
        return "", None
    if isinstance(value, bool):
        return "s", "Yes" if value else "No"
    if isinstance(value, int | float):
        if value != value or value in (float("inf"), float("-inf")):
            return "s", str(value)
        return "n", value
    text = str(value).strip()
    if text.startswith("=") and safe_formula(text):
        return "f", text[1:]
    if _PERCENT.match(text):
        return "p", round(float(text.rstrip("% ").strip()) / 100, 10)
    # a number written as text ("1,200" or "42.50"); codes with a leading zero ("0123", phone numbers) stay text
    if _NUMBER.match(text) and not re.match(r"^-?0\d", text):
        plain = text.replace(",", "")
        return "n", float(plain) if "." in plain else int(plain)
    return "s", text


def _cell(ref: str, value: Any, total: bool = False) -> tuple[str, int]:
    """(cell xml, display width in characters)."""
    kind, v = _typed(value)
    if kind == "":
        return (f'<c r="{ref}" s="{TOTAL}"/>' if total else ""), 0
    if kind == "s":
        return (f'<c r="{ref}" s="{TOTAL if total else PLAIN}" t="inlineStr"><is><t xml:space="preserve">'
                f"{_clean(v)}</t></is></c>"), min(60, max(len(line) for line in str(v).split("\n")))
    if kind == "f":
        return f'<c r="{ref}" s="{TOTAL_DEC if total else DEC}"><f>{_clean(v)}</f></c>', 12
    if kind == "p":
        style = PCT_WHOLE if float(v * 100).is_integer() else PCT
        return f'<c r="{ref}" s="{style}"><v>{v!r}</v></c>', 8
    whole = isinstance(v, int) or float(v).is_integer()
    style = (TOTAL_NUM if whole else TOTAL_DEC) if total else (NUM if whole else DEC)
    return f'<c r="{ref}" s="{style}"><v>{v!r}</v></c>', len(str(v) if whole else f"{v:,.2f}")


def _sheet_xml(sheet: Sheet) -> str:
    cols = max(1, min(MAX_COLS, max([len(sheet.columns)] + [len(r) for r in sheet.rows] or [1])))
    header = (list(sheet.columns) + [""] * cols)[:cols]
    widths = [len(str(h)) + 2 for h in header]
    lines = [f'<row r="1" spans="1:{cols}" ht="20" customHeight="1">'
             + "".join(f'<c r="{col_letter(c)}1" s="{HEADER}" t="inlineStr"><is><t xml:space="preserve">'
                       f"{_clean(str(h))}</t></is></c>" for c, h in enumerate(header)) + "</row>"]
    numeric = [True] * cols
    whole = [True] * cols  # a totals cell is shown the way its column is: 42 or 1,234.50
    seen = [False] * cols
    body = sheet.rows[:MAX_ROWS]
    for r, row in enumerate(body, 2):
        cells = []
        for c in range(cols):
            value = row[c] if c < len(row) else None
            xml, width = _cell(f"{col_letter(c)}{r}", value)
            kind, v = _typed(value)
            if kind:
                seen[c] = True
                numeric[c] = numeric[c] and kind in ("n", "f")
                whole[c] = whole[c] and kind == "n" and float(v).is_integer()
            widths[c] = max(widths[c], width + 2)
            cells.append(xml)
        lines.append(f'<row r="{r}">{"".join(cells)}</row>')
    last = len(body) + 1
    if sheet.totals and body:
        r = last + 1
        cells = []
        for c in range(cols):
            ref = f"{col_letter(c)}{r}"
            if numeric[c] and seen[c] and not _NOT_SUMMED.search(str(header[c])):
                style = TOTAL_NUM if whole[c] else TOTAL_DEC
                cells.append(f'<c r="{ref}" s="{style}"><f>SUM({col_letter(c)}2:{col_letter(c)}{last})</f></c>')
            else:
                cells.append(_cell(ref, "Total" if c == 0 else None, total=True)[0])
        lines.append(f'<row r="{r}">{"".join(cells)}</row>')
    col_xml = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{min(60, max(8, w))}" customWidth="1"/>'
                      for i, w in enumerate(widths))
    end = f"{col_letter(cols - 1)}{last}"
    filt = f'<autoFilter ref="A1:{end}"/>' if body else ""
    return (f'{XML}<worksheet xmlns="{MAIN}"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" '
            f'topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            f'<sheetFormatPr defaultRowHeight="16"/><cols>{col_xml}</cols><sheetData>{"".join(lines)}</sheetData>'
            f"{filt}<pageMargins left=\"0.7\" right=\"0.7\" top=\"0.75\" bottom=\"0.75\" header=\"0.3\" "
            f'footer="0.3"/></worksheet>')


STYLES = (f'{XML}<styleSheet xmlns="{MAIN}">'
          '<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00"/></numFmts>'
          '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
          '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>'
          '<fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/>'
          '</fill><fill><patternFill patternType="solid"><fgColor rgb="FF1F2A44"/></patternFill></fill>'
          '<fill><patternFill patternType="solid"><fgColor rgb="FFF3F4F6"/></patternFill></fill></fills>'
          '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
          '<border><left/><right/><top style="thin"><color rgb="FF6B7280"/></top><bottom/><diagonal/></border>'
          "</borders>"
          '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
          '<cellXfs count="9">'
          '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">'
          '<alignment vertical="top" wrapText="1"/></xf>'
          '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" '
          'applyAlignment="1"><alignment vertical="center"/></xf>'
          '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
          '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
          '<xf numFmtId="10" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
          '<xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1"/>'
          '<xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1"/>'
          '<xf numFmtId="164" fontId="0" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyFill="1" '
          'applyBorder="1"/>'
          '<xf numFmtId="9" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
          "</cellXfs>"
          '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def sheet_names(sheets: list[Sheet]) -> list[str]:
    """Excel's rules: at most 31 characters, none of []:*?/\\, not empty, unique (case-insensitive)."""
    used: set[str] = set()
    out = []
    for i, s in enumerate(sheets, 1):
        base = " ".join(_SHEET_NAME_BAD.sub(" ", _BAD.sub("", s.name)).split()).strip("'")[:31] or f"Sheet{i}"
        name, n = base, 2
        while name.lower() in used:
            tail = f" ({n})"
            name, n = base[:31 - len(tail)] + tail, n + 1
        used.add(name.lower())
        out.append(name)
    return out


def render_xlsx(sheets: list[Sheet], out: Path, title: str = "") -> Path:
    sheets = sheets[:MAX_SHEETS] or [Sheet("Sheet1", [""])]
    names = sheet_names(sheets)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    overrides = "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.'
                        'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                        for i in range(1, len(sheets) + 1))
    rels = "".join(f'<Relationship Id="rId{i}" Type="{REL}/worksheet" Target="worksheets/sheet{i}.xml"/>'
                   for i in range(1, len(sheets) + 1))
    book = "".join(f'<sheet name="{escape(n, {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>'
                   for i, n in enumerate(names, 1))
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   f'{XML}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                   'spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                   'spreadsheetml.styles+xml"/>'
                   '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.'
                   f'core-properties+xml"/>{overrides}</Types>')
        z.writestr("_rels/.rels",
                   f'{XML}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/>'
                   '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/'
                   'metadata/core-properties" Target="docProps/core.xml"/></Relationships>')
        z.writestr("docProps/core.xml",
                   f'{XML}<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
                   'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/'
                   'terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                   f"<dc:title>{_clean(title)}</dc:title><dc:creator>Delta</dc:creator>"
                   f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created></cp:coreProperties>')
        z.writestr("xl/workbook.xml",
                   f'{XML}<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets>{book}</sheets>'
                   '<calcPr calcId="0" fullCalcOnLoad="1"/></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   f'{XML}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}'
                   f'<Relationship Id="rId{len(sheets) + 1}" Type="{REL}/styles" Target="styles.xml"/></Relationships>')
        z.writestr("xl/styles.xml", STYLES)
        for i, s in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(s))
    return out
