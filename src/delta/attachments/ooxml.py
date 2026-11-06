"""Word (.docx) and PowerPoint (.pptx) text with the standard library: zipfile plus a hardened expat parser.

Safety, because these files come from anyone:
- Zip: at most MAX_ENTRIES entries; only the few XML parts that hold text are opened, read in chunks with a hard
  byte cap (a declared size can lie), and a part that inflates far beyond its compressed size is refused. Nothing
  is ever extracted to disk, so no name in the archive can write anywhere (no path traversal), and relationship
  targets are resolved inside the package only.
- XML: no DOCTYPE at all (Office never writes one), so no entity expansion (billion laughs) and no external
  entities (XXE); parameter entities off. This is what defusedxml does, without the dependency.
- Macros (vbaProject.bin), embedded objects (embeddings/, OLE) and ActiveX are never opened or run; the extract
  only says they were there.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.parsers import expat  # nosec B407: hardened below (no DOCTYPE, no entities, no external references)

MAX_ENTRIES = 5000
MAX_PART = 24 * 1024 * 1024       # one XML part, uncompressed
MAX_TOTAL = 96 * 1024 * 1024      # all parts read for one file
MAX_RATIO = 150                   # uncompressed / compressed, for parts over 1 MB
CHUNK = 64 * 1024
MAX_PART_CHARS = 400_000          # text kept per page / slide

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


class OfficeError(ValueError):
    """Plain words for the user."""


@dataclass
class Part:
    number: int
    title: str
    text: str


@dataclass
class OfficeDoc:
    parts: list[Part] = field(default_factory=list)
    paged: bool = False         # Word: page breaks were found (parts are pages, else sections)
    notes: list[str] = field(default_factory=list)  # macros, embedded objects, pictures, charts: what was skipped
    truncated: bool = False
    total: int = 0              # slides in the deck / parts found


class _Budget:
    def __init__(self) -> None:
        self.left = MAX_TOTAL


def _open_zip(path: Path) -> zipfile.ZipFile:
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise OfficeError("That file is damaged or not really an Office file.") from e
    if len(z.infolist()) > MAX_ENTRIES:
        z.close()
        raise OfficeError("That file has far too many parts inside to be a normal document.")
    return z


def _feed(z: zipfile.ZipFile, name: str, parser: expat.XMLParserType, budget: _Budget) -> bool:
    """Stream one part into the parser with byte caps. False when the part does not exist."""
    try:
        info = z.getinfo(name)
    except KeyError:
        return False
    if info.flag_bits & 0x1:
        raise OfficeError("That file is password protected. Remove the password and send it again.")
    if info.file_size > MAX_PART or (info.file_size > 1024 * 1024 and info.compress_size
                                      and info.file_size / info.compress_size > MAX_RATIO):
        raise OfficeError("A part of that file expands to far more data than a document should (it may be a zip "
                          "bomb), so it was not opened.")
    read = 0
    with z.open(info) as f:
        while block := f.read(CHUNK):
            read += len(block)
            budget.left -= len(block)
            if read > MAX_PART or budget.left < 0:
                raise OfficeError("That file expands to far more data than a document should, so reading stopped.")
            parser.Parse(block, False)
    parser.Parse(b"", True)
    return True


def _refuse(*_: object) -> None:
    raise OfficeError("That file has XML features Office never uses (a DOCTYPE or entities), so it was not read.")


def _parser(start: Callable[[str, dict[str, str]], None], end: Callable[[str], None],
            text: Callable[[str], None]) -> expat.XMLParserType:
    p = expat.ParserCreate(namespace_separator="}")
    p.StartDoctypeDeclHandler = _refuse
    p.EntityDeclHandler = _refuse
    p.UnparsedEntityDeclHandler = _refuse
    p.ExternalEntityRefHandler = lambda *_: 0  # never fetched: parsing stops with an error
    p.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    p.StartElementHandler = start
    p.EndElementHandler = end
    p.CharacterDataHandler = text
    p.buffer_text = True
    return p


def _parse(z: zipfile.ZipFile, name: str, budget: _Budget, start: Callable[[str, dict[str, str]], None],
           end: Callable[[str], None], text: Callable[[str], None]) -> bool:
    try:
        return _feed(z, name, _parser(start, end, text), budget)
    except expat.ExpatError as e:
        raise OfficeError(f"A part of that file is damaged ({posixpath.basename(name)}).") from e


def _local(tag: str) -> tuple[str, str]:
    ns, _, local = tag.rpartition("}")
    return ns, local


def _skipped(z: zipfile.ZipFile, prefix: str) -> list[str]:
    names = [i.filename.lower() for i in z.infolist()]
    notes = []
    if any(n.endswith("vbaproject.bin") for n in names):
        notes.append("It contains macros. They were not run.")
    if any(f"{prefix}/embeddings/" in n or "activex" in n for n in names):
        notes.append("It contains embedded objects. They were not opened.")
    return notes


# ---- Word -------------------------------------------------------------------------------------------------------
def read_docx(path: Path, max_parts: int = 300, part_chars: int = 3000) -> OfficeDoc:
    """Paragraphs with heading levels and tables as rows of cells. Parts are pages when Word stored page breaks,
    otherwise sections of about part_chars characters that start at headings where possible."""
    with _open_zip(path) as z:
        budget = _Budget()
        blocks: list[tuple[str, str]] = []  # (kind, text): kind = "h1".."h9", "p", "row", "page"
        state: dict[str, Any] = {"para": [], "style": "", "cells": [], "cell": [], "in_cell": 0, "text": False}

        def start(tag: str, attrs: dict[str, str]) -> None:
            ns, el = _local(tag)
            if ns != W_NS:
                return
            if el == "t" or el == "delText":
                state["text"] = el == "t"
            elif el == "tab":
                state["para"].append("\t")
            elif el in ("br", "cr"):
                if attrs.get(f"{W_NS}}}type") == "page":
                    _flush_para()
                    blocks.append(("page", ""))
                else:
                    state["para"].append("\n")
            elif el == "lastRenderedPageBreak":
                _flush_para(keep_open=True)
                blocks.append(("page", ""))
            elif el == "pStyle":
                state["style"] = attrs.get(f"{W_NS}}}val", "")
            elif el == "tc":
                state["in_cell"] += 1
                state["cell"] = []
            elif el == "tr":
                state["cells"] = []

        def _flush_para(keep_open: bool = False) -> None:
            text = "".join(state["para"]).strip()
            state["para"] = []
            if not text:
                return
            if state["in_cell"]:
                state["cell"].append(text)
                return
            m = re.match(r"(?i)^(?:heading|berschrift|titre|title)\s*(\d?)", state["style"] or "")
            level = (m.group(1) or "1") if m else ""
            blocks.append((f"h{level}" if level else "p", text))
            if not keep_open:
                state["style"] = ""

        def end(tag: str) -> None:
            ns, el = _local(tag)
            if ns != W_NS:
                return
            if el == "t":
                state["text"] = False
            elif el == "p":
                _flush_para()
            elif el == "tc":
                state["cells"].append(" ".join(state["cell"]))
                state["in_cell"] = max(0, state["in_cell"] - 1)
            elif el == "tr" and any(c.strip() for c in state["cells"]):
                blocks.append(("row", " | ".join(c.strip() for c in state["cells"])))

        def chars(data: str) -> None:
            if state["text"]:
                state["para"].append(data)

        if not _parse(z, "word/document.xml", budget, start, end, chars):
            raise OfficeError("That file has no document inside. It may not be a Word file.")
        doc = OfficeDoc(notes=_skipped(z, "word"))
        names = {i.filename for i in z.infolist()}
        if any(n.startswith("word/media/") for n in names):
            doc.notes.append("It has pictures. Only their text was read, not the pictures themselves.")
    return _docx_parts(blocks, doc, max_parts, part_chars)


def _docx_parts(blocks: list[tuple[str, str]], doc: OfficeDoc, max_parts: int, part_chars: int) -> OfficeDoc:
    doc.paged = any(k == "page" for k, _ in blocks)
    parts: list[Part] = []
    lines: list[str] = []
    title = ""
    size = 0

    def close() -> None:
        nonlocal lines, title, size
        text = "\n".join(lines).strip()
        if text or doc.paged:
            parts.append(Part(len(parts) + 1, title, text[:MAX_PART_CHARS]))
        lines, title, size = [], "", 0

    for kind, text in blocks:
        if kind == "page":
            if lines or doc.paged:
                close()
            continue
        if (not doc.paged and kind.startswith("h") and size > part_chars // 3) or (not doc.paged and size > part_chars):
            close()
        if kind.startswith("h"):
            level = int(kind[1:] or 1)
            title = title or text[:80]
            lines.append("#" * min(level, 6) + " " + text)
        else:
            lines.append(text)
        size += len(text) + 1
    close()
    parts = [p for p in parts if p.text] if not doc.paged else parts
    while doc.paged and parts and not parts[-1].text:
        parts.pop()
    for i, p in enumerate(parts, 1):
        p.number = i
        if not p.title:
            first = p.text.split("\n", 1)[0].lstrip("# ")
            p.title = first[:60]
    doc.total = len(parts)
    doc.truncated = len(parts) > max_parts
    doc.parts = parts[:max_parts]
    return doc


# ---- PowerPoint ---------------------------------------------------------------------------------------------------
def _rels(z: zipfile.ZipFile, part: str, budget: _Budget) -> dict[str, tuple[str, str]]:
    """Relationship id -> (type, target part) for one part, resolved inside the package (never outside it)."""
    folder, name = posixpath.split(part)
    rels_name = posixpath.join(folder, "_rels", name + ".rels")
    out: dict[str, tuple[str, str]] = {}

    def start(tag: str, attrs: dict[str, str]) -> None:
        if _local(tag)[1] != "Relationship" or attrs.get("TargetMode") == "External":
            return
        target = attrs.get("Target", "")
        full = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(folder, target))
        if full.startswith(("../", "/")) or full == ".." or ":" in full or "\\" in full:
            return  # outside the package: never followed
        out[attrs.get("Id", "")] = (attrs.get("Type", "").rsplit("/", 1)[-1], full)

    _parse(z, rels_name, budget, start, lambda _: None, lambda _: None)
    return out


def _slide_text(z: zipfile.ZipFile, name: str, budget: _Budget) -> tuple[str, list[str], list[str]]:
    """(title, lines, extras) of one slide or notes page. extras: pictures (alt text) and charts."""
    title: list[str] = []
    lines: list[str] = []
    extras: list[str] = []
    st: dict[str, Any] = {"ph": "", "para": [], "shape": [], "text": False, "row": None, "cell": [], "in_cell": 0}

    def start(tag: str, attrs: dict[str, str]) -> None:
        ns, el = _local(tag)
        if el == "sp" and ns == P_NS:
            st["ph"], st["shape"] = "", []
        elif el == "ph" and ns == P_NS:
            st["ph"] = attrs.get("type", "body")
        elif el == "t" and ns == A_NS:
            st["text"] = True
        elif el == "br" and ns == A_NS:
            st["para"].append(" ")
        elif el == "tr" and ns == A_NS:
            st["row"] = []
        elif el == "tc" and ns == A_NS:
            st["in_cell"] += 1
            st["cell"] = []
        elif el == "cNvPr" and ns == P_NS:
            st["last_descr"] = attrs.get("descr", "") or attrs.get("title", "")
        elif el == "pic" and ns == P_NS:
            st["pic"] = True
        elif el == "chart":
            extras.append("[chart]")

    def end(tag: str) -> None:
        ns, el = _local(tag)
        if el == "t" and ns == A_NS:
            st["text"] = False
        elif el == "p" and ns == A_NS:
            text = "".join(st["para"]).strip()
            st["para"] = []
            if not text:
                return
            if st["in_cell"]:
                st["cell"].append(text)
            else:
                st["shape"].append(text)
        elif el == "tc" and ns == A_NS:
            if st["row"] is not None:
                st["row"].append(" ".join(st["cell"]))
            st["in_cell"] = max(0, st["in_cell"] - 1)
        elif el == "tr" and ns == A_NS:
            if st["row"] and any(c.strip() for c in st["row"]):
                lines.append(" | ".join(c.strip() for c in st["row"]))
            st["row"] = None
        elif el == "sp" and ns == P_NS:
            if st["ph"] in ("title", "ctrTitle"):
                title.extend(st["shape"])
            elif st["ph"] not in ("sldNum", "dt", "ftr", "hdr"):
                lines.extend(st["shape"])
            st["shape"] = []
        elif el == "pic" and ns == P_NS:
            descr = (st.get("last_descr") or "").strip()
            extras.append(f"[picture: {descr[:120]}]" if descr else "[picture]")
            st["pic"] = False

    def chars(data: str) -> None:
        if st["text"]:
            st["para"].append(data)

    _parse(z, name, budget, start, end, chars)
    return " ".join(title).strip(), lines, extras


def read_pptx(path: Path, max_parts: int = 300) -> OfficeDoc:
    """One part per slide in the deck's order: title, text, tables as rows, picture alt text, speaker notes."""
    with _open_zip(path) as z:
        budget = _Budget()
        order: list[str] = []

        def start(tag: str, attrs: dict[str, str]) -> None:
            if _local(tag)[1] == "sldId":
                order.append(attrs.get(f"{R_NS}}}id", ""))

        if not _parse(z, "ppt/presentation.xml", budget, start, lambda _: None, lambda _: None):
            raise OfficeError("That file has no slides inside. It may not be a PowerPoint file.")
        rels = _rels(z, "ppt/presentation.xml", budget)
        slides = [rels[r][1] for r in order if r in rels and rels[r][1].startswith("ppt/slides/")]
        doc = OfficeDoc(notes=_skipped(z, "ppt"), total=len(slides))
        doc.truncated = len(slides) > max_parts
        pics = charts = 0
        for i, name in enumerate(slides[:max_parts], 1):
            title, lines, extras = _slide_text(z, name, budget)
            pics += sum(1 for e in extras if e.startswith("[picture"))
            charts += extras.count("[chart]")
            notes_text = ""
            for kind, target in _rels(z, name, budget).values():
                if kind == "notesSlide" and target.startswith("ppt/notesSlides/"):
                    _, nlines, _ = _slide_text(z, target, budget)
                    notes_text = " ".join(nlines).strip()
            body = "\n".join([*lines, *extras])
            if notes_text:
                body += f"\nSpeaker notes: {notes_text}"
            doc.parts.append(Part(i, title[:120], body.strip()[:MAX_PART_CHARS]))
        if pics or charts:
            doc.notes.append("Pictures and charts are listed by their description only; Delta did not look at them.")
    return doc
