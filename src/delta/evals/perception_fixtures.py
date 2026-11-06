"""Pictures for the perception evals and tests ONLY: drawn with Pillow at run time, so no binary fixture files.

    page_capture(mgr)    the fake browser's current page (delta.evals.fakebrowser) as a picture: one button-like box
                         per element, each candidate carrying the element's ref (a "dom" handle), exactly like a
                         capture of Delta's real browser
    text_capture(lines)  black text on white in a large font, for Windows OCR
"""

from __future__ import annotations

import sys
from typing import Any

from delta.perception.marks import Candidate, Capture

PAGE_SIZE = (900, 560)


def _font(size: int) -> Any:
    from PIL import ImageFont

    names = ["arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"] if sys.platform == "win32" else ["DejaVuSans.ttf"]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def page_capture(mgr: Any) -> Capture:
    """The fake page with its elements laid out top to bottom. Calls mgr.listing() so the refs are current."""
    from PIL import Image, ImageDraw

    mgr.listing()
    img = Image.new("RGB", PAGE_SIZE, (250, 250, 250))
    draw = ImageDraw.Draw(img)
    big, small = _font(28), _font(20)
    title = str(mgr.evidence().get("title") or "Page")
    draw.text((40, 24), title, fill=(20, 20, 20), font=big)
    out: list[Candidate] = []
    for i, (ref, el) in enumerate(sorted(mgr.refs.items())):
        box = (40, 90 + i * 70, 300, 48)
        x, y, w, h = box
        fill = (230, 236, 250) if el.role == "button" else (255, 255, 255)
        draw.rectangle([x, y, x + w, y + h], fill=fill, outline=(90, 90, 90), width=2)
        draw.text((x + 14, y + 12), el.name, fill=(10, 10, 10), font=small)
        out.append(Candidate(el.name, el.role, box, "dom", ref))
    return Capture(img, out, "dom", title, str(mgr.current_url))


def text_capture(lines: list[str], window: str = "Eval fixture") -> Capture:
    """Large black text on white, one line per entry: easy for any OCR language pack."""
    from PIL import Image, ImageDraw

    font = _font(40)
    img = Image.new("RGB", (1000, 120 + 90 * max(1, len(lines))), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((60, 60 + i * 90), line, fill=(0, 0, 0), font=font)
    return Capture(img, [], "ocr", window, origin=(100, 200))
