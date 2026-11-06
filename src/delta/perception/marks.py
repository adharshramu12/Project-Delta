"""Set-of-marks: numbered boxes over the clickable elements of a picture, and a compact legend.

The vision model answers with a mark number. Delta then clicks that element through its handle, never by
guessing pixels when a handle exists:

    dom  a numbered element of Delta's own browser page   -> browser_click ref=<n>
    uia  a Windows UI Automation element of a window      -> click_element on that very element
    xy   no handle (OCR text, a picture-only app)         -> click_at the centre of its box

Each resolved click is an ordinary tool call, so policy, approvals, Remote Guard, ExfilGuard and the taint
guard judge the real action exactly as if the model had asked for it directly.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

MAX_MARKS = 40     # marks drawn on one picture: more than this and the numbers crowd each other out
LABEL_CHARS = 40   # legend names are cut to this many characters
MIN_SIDE = 4       # boxes thinner than this (image pixels) are not clickable targets
DUP_IOU = 0.7      # two boxes overlapping this much are the same target: keep the first
PALETTE = [(230, 25, 75), (0, 130, 200), (60, 160, 60), (245, 130, 48), (145, 30, 180), (0, 128, 128),
           (128, 0, 0), (0, 0, 128)]
_MARK_ANSWER = re.compile(r"\bmark\s*(?:number|no\.?)?\s*[:#]?\s*(\d{1,3})\b", re.I)
_BARE_NUMBER = re.compile(r"^\W*\[?(\d{1,3})\]?\W*$")
_NONE = re.compile(r"\b(mark\s*:?\s*)?none\b", re.I)


@dataclass
class Candidate:
    """A clickable element found by a source, in the captured picture's own pixels."""

    label: str
    role: str
    box: tuple[int, int, int, int]  # x, y, width, height in capture pixels
    kind: str = "xy"                # dom | uia | xy
    handle: Any = None              # dom: the ref number; uia: the UI Automation element; xy: None
    in_view: bool = True


@dataclass
class Capture:
    """One captured view and its candidates. origin/scale turn capture pixels into screen pixels."""

    image: Any                       # PIL.Image
    candidates: list[Candidate]
    source: str                      # dom | uia | ocr | fixture
    window: str = ""                 # window title (desktop) or page title (browser)
    url: str = ""
    origin: tuple[int, int] = (0, 0)
    scale: float = 1.0               # screen pixels per capture pixel
    note: str = ""


@dataclass
class Mark:
    n: int
    label: str
    role: str
    box: tuple[int, int, int, int]   # in the SENT picture's pixels
    kind: str
    handle: Any = None
    screen: tuple[int, int] | None = None  # centre on screen (for an xy click)

    def line(self) -> str:
        name = self.label[:LABEL_CHARS] + ("…" if len(self.label) > LABEL_CHARS else "")
        return f'{self.n} {self.role} "{name}"' if name else f"{self.n} {self.role}"


@dataclass
class MarkSet:
    marks: dict[int, Mark]
    source: str
    window: str = ""
    url: str = ""
    created: float = field(default_factory=time.monotonic)

    def legend(self) -> str:
        return "\n".join(m.line() for m in self.marks.values())

    def resolve(self, n: int, task_id: str = "") -> tuple[str, dict[str, Any]] | str:
        """The ordinary tool call that clicks mark n, or why it cannot."""
        mark = self.marks.get(n)
        if mark is None:
            have = f"1 to {len(self.marks)}" if self.marks else "none"
            return f"there is no mark {n} in the latest marked picture (marks: {have}); call look_with_marks again"
        if mark.kind == "dom" and mark.handle is not None:
            return "browser_click", {"ref": int(mark.handle)}
        if mark.kind == "uia" and mark.handle is not None and mark.label:
            args: dict[str, Any] = {"name": mark.label, "handle": BOOK.handle_key(task_id, n)}
            if self.window:
                args["window"] = self.window
            return "click_element", args
        if mark.screen is not None:
            return "click_at", {"x": int(mark.screen[0]), "y": int(mark.screen[1]), "expect": mark.label}
        return f"mark {n} has no element handle and no screen position; call look_with_marks again"


class MarkBook:
    """The latest marks per task. A new look replaces the old marks; finished tasks are forgotten."""

    def __init__(self) -> None:
        self._sets: dict[str, MarkSet] = {}
        self._lock = threading.Lock()

    def put(self, task_id: str, marks: MarkSet) -> None:
        with self._lock:
            self._sets[task_id] = marks
            if len(self._sets) > 32:  # tasks that ended without forget(): keep the newest only
                for old in sorted(self._sets, key=lambda k: self._sets[k].created)[:-32]:
                    self._sets.pop(old, None)

    def get(self, task_id: str) -> MarkSet | None:
        with self._lock:
            return self._sets.get(task_id)

    def forget(self, task_id: str) -> None:
        with self._lock:
            self._sets.pop(task_id, None)

    @staticmethod
    def handle_key(task_id: str, n: int) -> str:
        return f"{task_id}#{n}"

    def handle(self, key: str) -> Any:
        """The UI Automation element behind a handle key, or None (unknown, or replaced by a newer look)."""
        task_id, _, n = key.rpartition("#")
        marks = self.get(task_id)
        if marks is None or not n.isdigit():
            return None
        mark = marks.marks.get(int(n))
        return mark.handle if mark is not None and mark.kind == "uia" else None


BOOK = MarkBook()


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0, min(ax2, bx2) - max(a[0], b[0]))
    ih = max(0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def _clip(box: tuple[int, int, int, int], size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    x, y, w, h = box
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(size[0], x + w), min(size[1], y + h)
    if x2 - x1 < MIN_SIDE or y2 - y1 < MIN_SIDE:
        return None
    return x1, y1, x2 - x1, y2 - y1


def select(candidates: list[Candidate], size: tuple[int, int], cap: int = MAX_MARKS) -> list[Candidate]:
    """The candidates worth a mark: visible in the picture, not duplicates, on-screen ones first, at most cap."""
    cap = max(1, min(int(cap), 99))
    ordered = [c for c in candidates if c.in_view] + [c for c in candidates if not c.in_view]
    kept: list[Candidate] = []
    for c in ordered:
        box = _clip(c.box, size)
        if box is None:
            continue
        if any(_iou(box, k.box) >= DUP_IOU for k in kept):
            continue
        kept.append(Candidate(c.label, c.role, box, c.kind, c.handle, c.in_view))
        if len(kept) >= cap:
            break
    return kept


def _font(size: int) -> Any:
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)  # Pillow 10.1+ with FreeType: a scalable default font
    except Exception:
        return ImageFont.load_default()


def mark_up(capture: Capture, max_side: int, cap: int = MAX_MARKS) -> tuple[Any, MarkSet]:
    """Downscale the capture and draw numbered boxes on its best candidates. Returns (picture, marks)."""
    from PIL import ImageDraw

    from delta.perception.images import downscale

    chosen = select(capture.candidates, capture.image.size, cap)
    small = downscale(capture.image, max_side)
    fx = small.width / max(1, capture.image.width)
    fy = small.height / max(1, capture.image.height)
    draw = ImageDraw.Draw(small)
    font = _font(max(11, min(18, round(small.width / 80))))
    marks: dict[int, Mark] = {}
    for i, c in enumerate(chosen, start=1):
        x, y, w, h = c.box
        box = (round(x * fx), round(y * fy), max(1, round(w * fx)), max(1, round(h * fy)))
        color = PALETTE[(i - 1) % len(PALETTE)]
        draw.rectangle([box[0], box[1], box[0] + box[2], box[1] + box[3]], outline=color, width=2)
        tag = str(i)
        left, top, right, bottom = draw.textbbox((0, 0), tag, font=font)
        tw, th = right - left + 6, bottom - top + 4
        tx = box[0]
        ty = box[1] - th if box[1] - th >= 0 else box[1]
        draw.rectangle([tx, ty, tx + tw, ty + th], fill=color)
        draw.text((tx + 3 - left, ty + 2 - top), tag, fill=(255, 255, 255), font=font)
        cx, cy = x + w / 2, y + h / 2
        screen = (round(capture.origin[0] + cx * capture.scale), round(capture.origin[1] + cy * capture.scale))
        marks[i] = Mark(i, c.label.strip(), c.role, box, c.kind, c.handle,
                        None if c.kind == "dom" else screen)
    return small, MarkSet(marks, capture.source, capture.window, capture.url)


def parse_answer(text: str) -> int | None:
    """The mark number in a vision model's answer ("MARK 3", "mark #3", or just "3"); None for NONE."""
    if not text:
        return None
    if found := _MARK_ANSWER.search(text):
        return int(found.group(1))
    first = text.strip().splitlines()[0] if text.strip() else ""
    if found := _BARE_NUMBER.match(first):
        return int(found.group(1))
    if _NONE.search(text):
        return None
    return None
