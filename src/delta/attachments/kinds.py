"""What an attached file is, decided by its first bytes (a renamed file is still caught), and the plain-words
messages for files Delta cannot read."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

IMAGE, PDF, DOCX, PPTX, TEXT = "image", "pdf", "docx", "pptx", "text"
KINDS = (IMAGE, PDF, DOCX, PPTX, TEXT)
LABELS = {IMAGE: "Picture", PDF: "PDF", DOCX: "Word document", PPTX: "PowerPoint", TEXT: "Text file"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".heif"}
TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json"}
SUPPORTED = "pictures (JPG, PNG, WebP, GIF), PDF, Word (.docx), PowerPoint (.pptx) and text files (.txt, .md, .csv)"
TELEGRAM_MAX_MB = 20  # the Telegram Bot API cannot download bigger files

_UNSUPPORTED = {
    ".doc": "Old Word files (.doc) can't be read. Save it as .docx in Word and send it again.",
    ".ppt": "Old PowerPoint files (.ppt) can't be read. Save it as .pptx and send it again.",
    ".xls": "Excel files can't be read yet. Save the sheet as .csv and send that instead.",
    ".xlsx": "Excel files can't be read yet. Save the sheet as .csv and send that instead.",
    ".zip": "Zip files are not opened, for safety. Send the files inside one by one.",
    ".exe": "Programs are never opened or run.",
    ".docm": "Word files with macros (.docm) are not opened, for safety. Save it as .docx and send it again.",
    ".pptm": "PowerPoint files with macros (.pptm) are not opened, for safety. Save it as .pptx and send it again.",
}


HEIC_HELP = ("iPhone HEIC photos can't be opened on this PC yet. Send it as a normal photo (Telegram turns it into a "
             "JPG), or set the iPhone to Camera > Formats > Most Compatible, or install HEIC support for Delta with "
             "`uv pip install pillow-heif` and send it again.")
HEIF_BRANDS = (b"ftypheic", b"ftypheix", b"ftypheim", b"ftypheis", b"ftyphevc", b"ftyphevx", b"ftypmif1",
               b"ftypmsf1")


def _heif_ok() -> bool:
    from delta.attachments import images  # late: images imports this module

    return bool(images.heif_support())


class AttachmentError(ValueError):
    """A file Delta will not take or cannot read. The message is for people: plain words, what to do instead."""


def too_big(size_bytes: int, max_mb: float) -> str:
    return (f"That file is {size_bytes / 1048576:.1f} MB. The limit is {max_mb:g} MB. "
            "Send a smaller file, or only the pages you need.")


def safe_name(name: str, fallback: str = "file") -> str:
    """A display name with no folders, control characters or Windows-reserved parts. Never used as a path on its
    own: files are stored under a random id."""
    base = re.split(r"[\\/]", str(name or ""))[-1]
    base = "".join(ch for ch in unicodedata.normalize("NFC", base) if unicodedata.category(ch)[0] != "C")
    base = re.sub(r'[<>:"|?*]', "_", base).strip(" .")
    stem = base.split(".")[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
    if not base or stem in reserved:
        base = fallback + (Path(base).suffix if base else "")
    if len(base) > 120:
        suffix = Path(base).suffix[:10]
        base = base[: 120 - len(suffix)] + suffix
    return base


def detect(head: bytes, name: str) -> str:
    """The kind of a file from its first bytes and its name; AttachmentError (plain words) when unsupported."""
    ext = Path(name or "").suffix.lower()
    if ext in _UNSUPPORTED:
        raise AttachmentError(_UNSUPPORTED[ext])
    if not head:
        raise AttachmentError("That file is empty.")
    if b"%PDF-" in head[:1024]:
        return PDF
    if head.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM")) or (
            head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return IMAGE
    if head[4:12] in HEIF_BRANDS:
        if _heif_ok():
            return IMAGE
        raise AttachmentError(HEIC_HELP)
    if head.startswith(b"PK\x03\x04"):
        if ext == ".docx":
            return DOCX
        if ext == ".pptx":
            return PPTX
        raise AttachmentError(f"That looks like a zip-based file Delta can't read. It reads {SUPPORTED}.")
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        raise AttachmentError("That is an old Office file (.doc, .ppt or .xls). Save it as .docx or .pptx and send "
                              "it again.")
    if head.startswith(b"MZ"):
        raise AttachmentError(_UNSUPPORTED[".exe"])
    if ext in TEXT_EXT:
        if head.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" not in head[:8192]:
            return TEXT  # UTF-8, the PC's own code page, or UTF-16 (Notepad's "Unicode")
        raise AttachmentError(f"That file says it is {ext} text but it holds binary data.")
    if ext in (".heic", ".heif") and not _heif_ok():
        raise AttachmentError(HEIC_HELP)
    if ext in IMAGE_EXT or ext in {".pdf", ".docx", ".pptx"}:
        raise AttachmentError(f"That file says it is {ext} but its contents are not. It may be damaged.")
    raise AttachmentError(f"Delta can't read that kind of file. It reads {SUPPORTED}.")


def mime_for(kind: str, name: str) -> str:
    ext = Path(name).suffix.lower()
    if kind == IMAGE:
        return {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
                ".heic": "image/heic", ".heif": "image/heif"}.get(
            ext, "image/jpeg")
    return {PDF: "application/pdf", TEXT: "text/plain",
            DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            PPTX: "application/vnd.openxmlformats-officedocument.presentationml.presentation"}.get(
        kind, "application/octet-stream")


# What Delta is asked when a file arrives with no words (a photo without a caption, a file dropped in the chat).
DEFAULT_REQUEST = "Look at the attached file and tell me briefly what it is and what matters in it."
