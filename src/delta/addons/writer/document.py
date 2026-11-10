"""The document model, a small Markdown reader, layout planning from the content, and the save folder.

Content arrives as Markdown (from the user, or written by the model in Markdown because every model
writes it well) with optional front matter. It becomes a list of blocks; the layout (template, title
page, contents, fonts, margins, header/footer, page numbers) is then chosen from what the content
actually is: its kind, length, number of sections, tables and maths. Renderers only draw a Doc.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from delta.addons.writer.rules import fix_mechanical

Kind = Literal["report", "letter", "resume", "notes", "assignment", "invoice", "one_pager", "paper", "other"]
KINDS: tuple[str, ...] = ("report", "letter", "resume", "notes", "assignment", "invoice", "one_pager", "paper",
                          "other")
TEMPLATE_OF: dict[str, str] = {
    "report": "report", "assignment": "report", "paper": "report", "letter": "letter", "resume": "resume",
    "notes": "notes", "one_pager": "notes", "invoice": "notes", "other": "notes",
}
DOC_EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".md", ".html", ".tex"}
MATH = re.compile(r"\$\$.+?\$\$|(?<![\\$\w])\$(?=\S)[^$\n]{1,200}?(?<=\S)\$(?!\w)|\\\(.+?\\\)|\\\[.+?\\\]", re.S)
LATEX_HINT = re.compile(r"\\(?:frac|sum|int|sqrt|alpha|beta|gamma|theta|lambda|sigma|pi|infty|partial|cdot|leq|geq|"
                        r"mathbb|mathrm|begin\{(?:equation|align))")
PAGEBREAK = re.compile(r"^\s*(?:\\pagebreak|\\newpage|<!--\s*pagebreak\s*-->|\[pagebreak\])\s*$", re.I)


@dataclass
class Block:
    type: Literal["heading", "paragraph", "bullets", "numbered", "table", "callout", "quote", "code", "pagebreak",
                  "math"]
    text: str = ""
    level: int = 1
    items: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)  # first row is the header
    label: str = ""   # callout label (Note, Tip, Important, Warning)


@dataclass
class Layout:
    template: str = "notes"            # report | letter | resume | notes
    title_page: bool = False
    toc: bool = False
    page_size: str = "A4"              # A4 | Letter
    margins_mm: int = 20
    font: str = "sans"                 # sans | serif
    font_pt: float = 11
    header: str = ""                   # running header text ('' = none)
    page_numbers: bool = True
    landscape: bool = False


@dataclass
class Doc:
    kind: str
    title: str
    blocks: list[Block]
    subtitle: str = ""
    author: str = ""
    date: str = ""
    meta: dict[str, str] = field(default_factory=dict)  # letter: from/to/subject/signature; invoice: number/due...
    layout: Layout = field(default_factory=Layout)

    def words(self) -> int:
        n = 0
        for b in self.blocks:
            n += len(b.text.split()) + sum(len(i.split()) for i in b.items) + sum(len(c.split()) for r in b.rows
                                                                                 for c in r)
        return n

    def headings(self, max_level: int = 2) -> list[Block]:
        return [b for b in self.blocks if b.type == "heading" and b.level <= max_level]

    def has_math(self) -> bool:
        return any(b.type == "math" or MATH.search(b.text) or any(MATH.search(i) for i in b.items)
                   for b in self.blocks)


# --- Markdown reader ------------------------------------------------------------------------------------

_FRONT = re.compile(r"\A\s*---\s*\n(.*?)\n---\s*\n", re.S)
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_CALLOUT = re.compile(r"^(?:\*\*)?(note|tip|important|warning|caution|key point|summary)(?:\*\*)?\s*[:.]?(?:\*\*)?\s*",
                      re.I)


def _cells(line: str) -> list[str]:
    s = line.strip()
    s = s.removeprefix("|").removesuffix("|")
    return [c.strip() for c in s.split("|")]


def parse_markdown(text: str) -> tuple[dict[str, str], list[Block]]:
    """(front matter, blocks). Tolerant: anything it does not know becomes a paragraph."""
    meta: dict[str, str] = {}
    text = text.replace("\r\n", "\n").strip("\n")
    if m := _FRONT.match(text + "\n"):
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip().strip('"').strip("'")
        text = (text + "\n")[m.end():]
    lines = text.split("\n")
    blocks: list[Block] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append(Block("paragraph", " ".join(s.strip() for s in para)))
            para.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            flush()
            i += 1
            continue
        if PAGEBREAK.match(s):
            flush()
            blocks.append(Block("pagebreak"))
            i += 1
            continue
        if s.startswith("```"):
            flush()
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            blocks.append(Block("code", "\n".join(body)))
            i += 1
            continue
        if s.startswith("$$"):
            flush()
            body = [s]
            while not (len("\n".join(body)) > 2 and "\n".join(body).rstrip().endswith("$$")) and i + 1 < len(lines):
                i += 1
                body.append(lines[i])
            blocks.append(Block("math", "\n".join(body).strip().strip("$").strip()))
            i += 1
            continue
        if h := re.match(r"^(#{1,4})\s+(.*)$", s):
            flush()
            blocks.append(Block("heading", h.group(2).strip().rstrip("#").strip(), level=len(h.group(1))))
            i += 1
            continue
        if re.match(r"^-{3,}$|^\*{3,}$|^_{3,}$", s):  # a rule: just a section gap
            flush()
            i += 1
            continue
        if "|" in s and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush()
            rows = [_cells(s)]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            width = max(len(r) for r in rows)
            blocks.append(Block("table", rows=[r + [""] * (width - len(r)) for r in rows]))
            continue
        if re.match(r"^[-*+\u2022]\s+", s) or re.match(r"^\d+[.)]\s+", s):
            flush()
            numbered = bool(re.match(r"^\d+[.)]\s+", s))
            items: list[str] = []
            while i < len(lines):
                cur = lines[i].strip()
                item = re.match(r"^(?:[-*+\u2022]|\d+[.)])\s+(.*)$", cur)
                if item:
                    items.append(item.group(1).strip())
                elif cur and items and lines[i].startswith((" ", "\t")):
                    items[-1] += " " + cur  # continuation line
                else:
                    break
                i += 1
            blocks.append(Block("numbered" if numbered else "bullets", items=items))
            continue
        if s.startswith(">"):
            flush()
            body = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                body.append(lines[i].strip().lstrip(">").strip())
                i += 1
            joined = " ".join(b for b in body if b)
            if c := _CALLOUT.match(joined):
                blocks.append(Block("callout", joined[c.end():].strip(), label=c.group(1).title()))
            else:
                blocks.append(Block("quote", joined))
            continue
        para.append(line)
        i += 1
    flush()
    return meta, blocks


# --- writing rules inside documents -------------------------------------------------------------------


def _fix_prose(text: str) -> str:
    """Rules for prose, with maths kept exactly as written (it needs its backslashes)."""
    kept: list[str] = []

    def hold(m: re.Match[str]) -> str:
        kept.append(m.group(0))
        return f"\x01{len(kept) - 1}\x01"

    held = MATH.sub(hold, text)
    fixed = fix_mechanical(held, markdown=True)
    return re.sub("\x01(\\d+)\x01", lambda m: kept[int(m.group(1))], fixed)


def apply_rules(doc: Doc) -> Doc:
    """Indian English, no long dashes, no AI words, no emojis, in headings, paragraphs, lists and callouts.
    Code, maths and table numbers are left alone (table text cells get the same fixes)."""
    doc.title = _fix_prose(doc.title)
    doc.subtitle = _fix_prose(doc.subtitle)
    for b in doc.blocks:
        if b.type in ("heading", "paragraph", "callout", "quote"):
            b.text = _fix_prose(b.text)
        elif b.type in ("bullets", "numbered"):
            b.items = [_fix_prose(i) for i in b.items]
        elif b.type == "table":
            b.rows = [[_fix_prose(c) if re.search(r"[A-Za-z]{3,}", c) else c for c in r] for r in b.rows]
    return doc


# --- layout from content ------------------------------------------------------------------------------


def build_doc(kind: str, markdown: str, title: str = "", page_size: str = "A4", author: str = "",
              title_page: bool | None = None, header: str | None = None) -> Doc:
    kind = kind if kind in KINDS else "other"
    meta, blocks = parse_markdown(markdown)
    if not title:
        title = meta.pop("title", "")
    if not title and blocks and blocks[0].type == "heading" and blocks[0].level == 1:
        title = blocks.pop(0).text
    if not title:
        title = {"letter": "Letter", "resume": "Resume", "invoice": "Invoice"}.get(kind, "Document")
    # A first-level heading equal to the title is not repeated in the body.
    if blocks and blocks[0].type == "heading" and blocks[0].text.strip().lower() == title.strip().lower():
        blocks.pop(0)
    doc = Doc(kind, title.strip(), blocks, subtitle=meta.pop("subtitle", ""),
              author=meta.pop("author", author), date=meta.pop("date", ""), meta=meta)
    doc.layout = plan_layout(doc, page_size, title_page, header)
    return doc


def plan_layout(doc: Doc, page_size: str = "A4", title_page: bool | None = None, header: str | None = None) -> Layout:
    """Pick the layout from the content, not one fixed template."""
    template = TEMPLATE_OF.get(doc.kind, "notes")
    words = doc.words()
    sections = len(doc.headings(2))
    tables = [b for b in doc.blocks if b.type == "table"]
    widest = max((len(t.rows[0]) for t in tables if t.rows), default=0)
    est_pages = max(1, round(words / 450 + len(tables) * 0.3))
    lay = Layout(template=template, page_size="Letter" if page_size.lower() == "letter" else "A4")
    if template == "report":
        lay.font, lay.margins_mm = "serif", 22
        auto_title = doc.kind == "assignment" or words > 1200 or (sections >= 6 and words > 600)
        lay.title_page = auto_title if title_page is None else title_page
        lay.toc = lay.title_page and sections >= 5
        lay.header = doc.title if header is None and est_pages > 2 else (header or "")
    elif template == "letter":
        lay.font, lay.margins_mm, lay.page_numbers = "serif", 25, est_pages > 1
        lay.title_page = False
    elif template == "resume":
        lay.font, lay.margins_mm, lay.font_pt = "sans", 14, 10.5
        lay.page_numbers = est_pages > 1
        lay.title_page = False
    else:
        lay.font, lay.margins_mm = "sans", 18
        lay.title_page = bool(title_page)
        lay.page_numbers = est_pages > 1 or doc.kind == "invoice"
        if doc.kind == "one_pager":
            lay.margins_mm, lay.font_pt, lay.page_numbers = 16, 10.5, False
    if words > 2500 and template != "resume":
        lay.font_pt = min(lay.font_pt, 10.5)
    lay.landscape = widest >= 8
    if header is not None and template != "report":
        lay.header = header
    return lay


# --- where files go -----------------------------------------------------------------------------------


def documents_dir(setting: str = "auto", create: bool = True) -> Path:
    """<Documents>\\Delta by default (the real Documents folder, OneDrive-moved or not), or the configured path."""
    if setting and setting != "auto":
        path = Path(os.path.expandvars(os.path.expanduser(setting)))
    else:
        from delta.config import known_folder

        path = known_folder("Personal") / "Delta"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def slug(title: str, limit: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip()
    s = re.sub(r"[\s_-]+", " ", s).strip()
    s = re.sub(r'[<>:"/\\|?*]', "", s)
    return (s[:limit].strip() or "Document").replace(" ", "-")


def unique_stem(folder: Path, title: str, now: datetime | None = None) -> str:
    """A name never used yet in folder, for every extension: nothing is ever overwritten."""
    base = f"{slug(title)}-{(now or datetime.now()):%Y%m%d-%H%M}"
    stem, n = base, 2
    while any((folder / f"{stem}{ext}").exists() for ext in DOC_EXTS):
        stem, n = f"{base}-{n}", n + 1
    return stem


def list_documents(folder: Path, limit: int = 50) -> list[dict[str, object]]:
    """Newest first: the documents Delta made in this folder (top level only)."""
    if not folder.is_dir():
        return []
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in DOC_EXTS]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.name, "size": p.stat().st_size, "modified": datetime.fromtimestamp(p.stat().st_mtime)
             .isoformat(timespec="seconds"), "type": p.suffix.lower().lstrip(".")} for p in files[:limit]]
