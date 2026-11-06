"""One structured extract per file: numbered sections (pages, slides or parts) with a title and text, plus notes in
plain words (scanned pages, protection, macros not run). Cached on disk by the file's SHA-256, so asking about the
same file again, in any chat, costs no reading and no tokens beyond the extract itself."""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from delta.attachments.kinds import DOCX, IMAGE, PDF, PPTX, TEXT, AttachmentError

PART_CHARS = 3000      # Word sections and text-file parts
MAX_TEXT_BYTES = 8 * 1024 * 1024
VERSION = 1


@dataclass
class Section:
    number: int
    title: str
    text: str
    scanned: bool = False  # a PDF page with no text layer (a scan): read with OCR when asked for


@dataclass
class Extract:
    kind: str
    unit: str                      # page | slide | part | picture
    total: int                     # pages / slides / parts in the file (may exceed len(sections) when capped)
    sections: list[Section] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    title: str = ""
    encrypted: bool = False
    width: int = 0                 # pictures
    height: int = 0
    ocr: str = ""                  # pictures: "" not tried, "none" not available, "done"
    vision: dict[str, str] = field(default_factory=dict)  # question digest -> what a vision model said
    version: int = VERSION

    @property
    def chars(self) -> int:
        return sum(len(s.text) for s in self.sections)

    @property
    def scanned(self) -> list[int]:
        return [s.number for s in self.sections if s.scanned]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> Extract | None:
        try:
            data = json.loads(raw)
            if data.get("version") != VERSION:
                return None
            data["sections"] = [Section(**s) for s in data.get("sections") or []]
            return cls(**data)
        except (ValueError, TypeError):
            return None

    def summary(self) -> str:
        """One short line for the chip in the chat."""
        if self.kind == IMAGE:
            return f"{self.width}x{self.height}"
        n = self.total
        unit = self.unit + ("s" if n != 1 else "")
        extra = f", {len(self.scanned)} scanned" if self.scanned else ""
        return ("protected PDF" if self.encrypted and not self.sections else f"{n} {unit}{extra}")


# ---- readers ------------------------------------------------------------------------------------------------------
def read_text_file(path: Path, max_parts: int) -> Extract:
    raw = b""
    with path.open("rb") as f:
        raw = f.read(MAX_TEXT_BYTES + 1)
    cut = len(raw) > MAX_TEXT_BYTES
    raw = raw[:MAX_TEXT_BYTES]
    if raw.startswith(b"\xef\xbb\xbf"):
        text = raw[3:].decode("utf-8", "replace")
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", "replace")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", "replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = text.split("\n")
    csv_like = path.suffix.lower() in (".csv", ".tsv")
    header = lines[0] if csv_like and lines else ""
    sections: list[Section] = []
    buf: list[str] = []
    size = 0
    for line in lines[1:] if header else lines:
        if size + len(line) > PART_CHARS and buf:
            sections.append(Section(len(sections) + 1, "", "\n".join(([header] if header else []) + buf)))
            buf, size = [], 0
        buf.append(line[:PART_CHARS])
        size += len(line) + 1
    if buf or not sections:
        sections.append(Section(len(sections) + 1, "", "\n".join(([header] if header else []) + buf)))
    for s in sections:
        first = next((ln for ln in s.text.split("\n") if ln.strip()), "")
        s.title = first.lstrip("# ")[:60]
    ex = Extract(TEXT, "part", len(sections), sections[:max_parts])
    if header:
        ex.notes.append(f"Columns: {header[:300]}")
    if cut or len(sections) > max_parts:
        ex.notes.append("The file is longer than Delta reads; only the first part was read.")
    return ex


def read_document(path: Path, kind: str, max_pages: int) -> Extract:
    """The text extract of a PDF, Word, PowerPoint or text file (pictures are read with OCR by the service)."""
    if kind == PDF:
        from delta.attachments.pdf import PdfError, read_pdf

        try:
            doc = read_pdf(path, max_pages)
        except PdfError as e:
            raise AttachmentError(str(e)) from e
        ex = Extract(PDF, "page", doc.page_count, title=doc.title, encrypted=doc.encrypted)
        ex.sections = [Section(p.number, _first_line(p.text), p.text, p.scanned or not p.text.strip())
                       for p in doc.pages]
        if doc.encrypted:
            ex.notes.append("This PDF is protected (encrypted), so its text could not be read directly.")
        if doc.truncated:
            ex.notes.append(f"Only the first {len(doc.pages)} of {doc.page_count} pages were read.")
        return ex
    if kind in (DOCX, PPTX):
        from delta.attachments.ooxml import OfficeError, read_docx, read_pptx

        try:
            office = read_docx(path, max_pages, PART_CHARS) if kind == DOCX else read_pptx(path, max_pages)
        except OfficeError as e:
            raise AttachmentError(str(e)) from e
        unit = "slide" if kind == PPTX else ("page" if office.paged else "part")
        ex = Extract(kind, unit, office.total or len(office.parts), notes=list(office.notes))
        ex.sections = [Section(p.number, p.title, p.text) for p in office.parts]
        if office.truncated:
            ex.notes.append(f"Only the first {len(office.parts)} {unit}s were read.")
        return ex
    if kind == TEXT:
        return read_text_file(path, max_pages)
    raise AttachmentError("That kind of file can't be read.")


def _first_line(text: str) -> str:
    """A PDF page's heading, when its first real line looks like one (short, starts like a title, does not run on
    into the next line). Pages whose text starts mid-sentence get no title rather than a misleading one."""
    for line in text.split("\n"):
        line = line.strip()
        if len(line) <= 2:
            continue
        if len(line) <= 50 and (line[0].isupper() or line[0].isdigit()) and not line.endswith((",", "-", ";", ":")):
            return line
        return ""
    return ""


def parse_pages(spec: str, total: int, limit: int = 30) -> list[int]:
    """'3', '2-5', '1,4,7-8', 'last', 'all' -> page numbers within 1..total (at most `limit`)."""
    spec = (spec or "").strip().lower().replace(" ", "")
    if not spec or total <= 0:
        return []
    if spec in ("all", "*"):
        return list(range(1, min(total, limit) + 1))
    out: list[int] = []
    for part in spec.split(","):
        part = part.replace("last", str(total))
        m = re.fullmatch(r"(\d+)(?:[-:](\d+))?", part)
        if not m:
            continue
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if b < a:
            a, b = b, a
        for n in range(max(1, a), min(total, b) + 1):
            if n not in out:
                out.append(n)
            if len(out) >= limit:
                return out
    return out


# ---- cache --------------------------------------------------------------------------------------------------------
class ExtractCache:
    """<data>/attachments/.cache/<sha256>.json, oldest dropped past max_mb. Reading a hit refreshes its age."""

    def __init__(self, folder: Path, max_mb: float = 50) -> None:
        self.folder = folder
        self.max_bytes = int(max_mb * 1024 * 1024)
        self._lock = threading.Lock()
        self.hits = 0

    def _path(self, sha: str) -> Path | None:
        return self.folder / f"{sha}.json" if re.fullmatch(r"[a-f0-9]{64}", sha or "") else None

    def get(self, sha: str) -> Extract | None:
        path = self._path(sha)
        if path is None or not path.is_file():
            return None
        try:
            ex = Extract.from_json(path.read_text(encoding="utf-8"))
        except OSError:
            return None
        if ex is not None:
            self.hits += 1
            with contextlib.suppress(OSError):
                os.utime(path)
        return ex

    def put(self, sha: str, ex: Extract) -> None:
        path = self._path(sha)
        if path is None:
            return
        with self._lock:
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(ex.to_json(), encoding="utf-8")
                os.replace(tmp, path)
                self._prune()
            except OSError:
                pass  # a cache that cannot be written only costs a re-read

    def _prune(self) -> None:
        files: list[tuple[float, int, Path]] = []
        for p in self.folder.glob("*.json"):
            with contextlib.suppress(OSError):
                st = p.stat()
                files.append((st.st_mtime, st.st_size, p))
        total = sum(f[1] for f in files)
        for _, size, p in sorted(files):
            if total <= self.max_bytes:
                break
            with contextlib.suppress(OSError):
                p.unlink()
                total -= size

    def forget_older_than(self, days: float) -> None:
        if days <= 0 or not self.folder.is_dir():
            return
        cutoff = time.time() - days * 86400
        for p in self.folder.glob("*.json"):
            with contextlib.suppress(OSError):
                if p.stat().st_mtime < cutoff:
                    p.unlink()


def as_dict(ex: Extract) -> dict[str, Any]:
    return asdict(ex)
