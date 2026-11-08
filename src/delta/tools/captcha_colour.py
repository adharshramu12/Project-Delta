"""Bright coloured captchas: fat, rounded letters in one strong colour (hot pink, orange, sky blue) on white, both
cases, touching each other. Some college ERPs draw exactly this, and the general passes (delta.tools.captcha_ocr.
variants) read it badly: in grayscale the pink is a middling grey, the fat letters run into one another, a thin "i"
disappears between two round neighbours, and the captcha model itself is unsure of case for such a font.

What is done instead, for a picture that looks like this (is_bright_ink):

1. the ink is isolated by its colour, not its brightness: each pixel's distance from white in its strongest channel
   (pink on white becomes black on white, whatever the shade), cropped to the ink with a white border, and made
   ~96 px tall; a second layer is the same stretched 1.3 times sideways, which gives the model more steps between
   touching letters (a thin "i" between two round letters comes back);
2. both of ddddocr's bundled models (its default and its "beta" one, the same package) read both layers; the four
   readings vote WITHOUT case on the length most of them agree on (delta.tools.captcha_ocr.pick), then per letter;
3. each letter's case is then decided by its shape where a shape tells:
     - letters whose small and capital forms differ only in size (c o s u v w x z, and a e m n r here, whose small
       forms have no part above the middle line): measured against the height of the letters that are always tall
       (b d f h k l t, capitals, i with its dot); small when clearly shorter;
     - i: a dot above a separate stroke is a small i; a plain bar is a capital I;
     - y p g j q: a tail below the line the other letters stand on is the small letter;
   and for the rest (b/B, d/D, f/F, h/H, k/K, t/T ...) by the two models' odds, each shifted by how much it under-reads
   capitals on such fonts (MODEL_CASE_SHIFT, chosen on benchmark seeds that are not the reported ones);
4. a letter whose case was a close call, or readings that did not all agree, mark the answer as not sure, so Delta's
   usual order then asks Windows OCR and, if they still differ, makes its one small vision call.

Pure Pillow and numpy (both already Delta dependencies); nothing is sent anywhere, logged or kept.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from delta.tools.captcha_ocr import Hints, pick

LAYER_HEIGHT = 96
BORDER = 10
STRETCH = 1.3
# Letters whose small form is only a smaller copy of the capital, or (a e m n r) whose small form has nothing above
# the middle line while the capital reaches the top: their case is their height.
SIZE_CASE = frozenset("acemnorsuvwxz")
TAILS = frozenset("gjpqy")  # small forms hang below the line
# One upright bar: small i (with its dot), capital I, small l, digit 1. The models swap them freely on fat fonts, so
# they vote as one letter and its dot decides.
BARS = frozenset("il1|")
SMALL_RATIO = 0.88  # a size-case letter shorter than this share of the tall letters' height is small
CLOSE = 0.06  # within this of SMALL_RATIO the height says little: a close call
TOP_SHARE = 0.2  # a letter's top: the 20th percentile (from the top) of its middle columns' tops (glyphs)
# log10(capital odds / small odds) is shifted by this per model before deciding (> 0: capital). Chosen on
# delta.evals.captcha_bench kl_bubbly seeds 1000+ (the reported seeds are 0..199): both models lean to small letters
# on round fonts, the beta model much more.
MODEL_CASE_SHIFT = {"default": 0.8, "beta": 1.75}
CASE_MARGIN_LOG = 0.35  # a model-decided case closer than this to 0 is a close call
MIN_SURE_LEN = 4  # a shorter answer (with no length from the box) is never taken as sure


def _np() -> Any:
    import numpy as np

    return np


def ink_stats(img: Any) -> tuple[float, float, float]:
    """(share of near-white pixels, median saturation of the ink, median brightness of the ink), 0..1 each."""
    np = _np()
    a = np.asarray(img.convert("RGB"), dtype=np.int16)
    mx, mn = a.max(axis=2), a.min(axis=2)
    white = float(((mn > 225) & (mx - mn < 25)).mean())
    ink = (255 - a).max(axis=2) > 70
    if not ink.any():
        return white, 0.0, 0.0
    sat = ((mx - mn) / np.maximum(mx, 1))[ink]
    return white, float(np.median(sat)), float(np.median(mx[ink] / 255.0))


def is_bright_ink(img: Any) -> bool:
    """Bright, strongly coloured letters on a white picture (hot pink on white), not dark text on a pale picture and
    not digits on a coloured, speckled one."""
    try:
        white, sat, val = ink_stats(img)
    except Exception:
        return False
    return white > 0.45 and sat > 0.45 and val > 0.6


def ink_layers(img: Any) -> list[Any]:
    """The ink as black on white by colour, cropped and ~LAYER_HEIGHT tall: [as is, stretched STRETCH sideways]."""
    from PIL import Image, ImageOps

    np = _np()
    a = np.asarray(img.convert("RGB"), dtype=np.int16)
    gray = Image.fromarray((255 - (255 - a).max(axis=2)).astype("uint8"), "L")
    gray = gray.resize((max(1, round(gray.width * LAYER_HEIGHT / max(1, gray.height))), LAYER_HEIGHT),
                       Image.Resampling.LANCZOS)
    box = gray.point(lambda v: 255 if v < 200 else 0).getbbox()
    if box:
        gray = gray.crop(box)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    plain = ImageOps.expand(gray, BORDER, fill=255)
    wide = ImageOps.expand(gray.resize((max(1, round(gray.width * STRETCH)), gray.height),
                                       Image.Resampling.LANCZOS), BORDER, fill=255)
    return [plain, wide]


@dataclass
class Run:
    """One character the model read: its letter without case, the steps it spans and its capital / small odds."""

    ch: str
    a: int
    b: int
    up: float
    low: float
    raw: str = ""  # what the model put first in its own output here, with case (for the bars: i, I, l or 1)


@dataclass
class Reading:
    model: str  # default | beta
    layer: int
    runs: list[Run]
    steps: int

    @property
    def blind(self) -> str:
        """The letters without case, every upright bar (i I l 1) as one: "i"."""
        return "".join("i" if r.ch in BARS else r.ch for r in self.runs)


_ALLOWED = {
    "digits": lambda c: c.isdigit(),
    "letters": lambda c: c.isalpha(),
    "upper": lambda c: c.isalpha(),
    "lower": lambda c: c.isalpha(),
}


def runs_from(probs: Any, charset: list[str], hints: Hints | None = None) -> tuple[list[Run], int]:
    """The model's CTC output as runs, each letter chosen without its case (as delta.tools.captcha_model.decode_detail
    does), with where it sits (steps a..b of `steps`) so its shape can be measured."""
    np = _np()
    hints = hints or Hints()
    p = np.asarray(probs, dtype=np.float64)
    if p.ndim == 3:
        p = p[:, 0, :] if p.shape[1] == 1 else p[0]
    if p.ndim != 2 or p.shape[0] == 0 or p.shape[1] != len(charset):
        return [], 0
    ok = _ALLOWED.get(hints.charset, lambda c: True)
    groups: dict[str, list[int]] = {}
    index: dict[str, int] = {}
    for i, c in enumerate(charset):
        if i and len(c) == 1 and c.isascii() and c.isalnum():
            index[c] = i
            if ok(c):
                groups.setdefault(c.lower(), []).append(i)
    if not groups:
        return [], int(p.shape[0])
    keys = list(groups)
    merged = np.stack([p[:, 0], *(p[:, groups[k]].sum(axis=1) for k in keys)], axis=1)
    best = merged.argmax(axis=1).tolist()
    spans: list[list[int]] = []
    prev = 0
    for t, j in enumerate(best):
        if j and j == prev:
            spans[-1][2] = t
        elif j:
            spans.append([j, t, t])
        prev = j
    out = []
    for j, a, b in spans:
        ch = keys[j - 1]
        up = float(p[a:b + 1, index[ch.upper()]].sum()) if ch.upper() in index else 0.0
        low = float(p[a:b + 1, index[ch.lower()]].sum()) if ch.lower() in index else 0.0
        raw = max((c for c in (ch.upper(), ch.lower()) if c in index), key=lambda c: float(p[a:b + 1, index[c]].sum()),
                  default=ch)
        out.append(Run(ch, a, b, up, low, raw))
    return out, int(p.shape[0])


@dataclass
class Glyph:
    top: int  # the letter's top and bottom, read across its middle columns (see glyphs)
    bottom: int
    segments: list[tuple[int, int]] = field(default_factory=list)  # rows with ink in its middle, top to bottom
    dot: int = -1  # top row of a short blob sitting over a longer stroke in one of its columns (an i's dot), or -1

    @property
    def height(self) -> int:
        return self.bottom - self.top + 1


def glyphs(layer: Any, reading: Reading) -> list[Glyph | None]:
    """Each read letter's ink, measured in the middle of where it sits, never at its edges, where the neighbour it
    touches would count too. The model sees the layer 64 px tall, one step per ~8 columns, and its peak for a letter
    comes at the letter's right-hand end: a letter is taken to run from the previous letter's peak (or the first
    ink) to its own."""
    np = _np()
    ink = np.asarray(layer.convert("L")) < 128
    h, w = ink.shape
    if not reading.runs or not reading.steps or not h:
        return []
    w64 = max(1, int(w * 64 / h))
    per_step = w64 / reading.steps * (w / w64)
    peaks = [((r.a + r.b) / 2 + 0.5) * per_step for r in reading.runs]
    cols_with_ink = np.where(ink.any(axis=0))[0]
    first = float(cols_with_ink[0]) if len(cols_with_ink) else 0.0
    out: list[Glyph | None] = []
    for k, right in enumerate(peaks):
        left = peaks[k - 1] if k else min(first, right - 1)
        cx, half = (left + right) / 2, max(2.0, (right - left) * 0.3)
        cols = ink[:, max(0, int(cx - half)):min(w, int(cx + half) + 1)]
        rows = np.where(cols.any(axis=1))[0]
        filled = cols.any(axis=0)
        if not len(rows) or not filled.any():
            out.append(None)
            continue
        # per column, its first and last ink row; a high (not the highest) one of each, so a neighbour reaching into
        # the window's edge, or the narrow peak of an A, does not decide the letter's height alone
        tops = np.sort(cols[:, filled].argmax(axis=0))
        bottoms = np.sort(h - 1 - cols[::-1, filled].argmax(axis=0))
        top = int(tops[int(len(tops) * TOP_SHARE)])
        bottom = int(bottoms[min(len(bottoms) - 1, int(len(bottoms) * (1 - TOP_SHARE)))])
        segs: list[tuple[int, int]] = []
        start = last = int(rows[0])
        for y in rows[1:].tolist():
            if y > last + 1:
                segs.append((start, last))
                start = y
            last = y
        segs.append((start, last))
        out.append(Glyph(top, bottom, segs, _dot(ink[:, max(0, int(left)):min(w, int(right) + 2)])))
    return out


def missed_ink(layer: Any, reading: Reading) -> bool:
    """Ink the model did not account for: a stretch of letters after its last letter, or between two of its letters,
    wider than about one letter. Then a letter was most likely skipped (two touching letters read as one, a thin i
    lost between round ones) and the answer must not be taken as sure, even when every reading agrees."""
    np = _np()
    ink = np.asarray(layer.convert("L")) < 128
    h, w = ink.shape
    if not reading.runs or not reading.steps or not h:
        return True
    cols = np.where(ink.sum(axis=0) > max(1, h // 30))[0]
    if not len(cols):
        return False
    w64 = max(1, int(w * 64 / h))
    per_step = w64 / reading.steps * (w / w64)
    peaks = [((r.a + r.b) / 2 + 0.5) * per_step for r in reading.runs]
    edges = [float(cols[0]), *peaks]
    spans = sorted(b - a for a, b in pairwise(edges))
    letter = max(4.0, spans[len(spans) // 2])
    after = float(cols[-1]) - peaks[-1]
    widest = max(b - a for a, b in pairwise(edges))
    return after > 0.75 * letter or widest > 1.9 * letter


def _dot(cols: Any) -> int:
    """The top row of an i's dot in these columns: a short blob, a gap, then a stroke at least twice as long. -1."""
    np = _np()
    best = -1
    for c in range(cols.shape[1]):
        rows = np.where(cols[:, c])[0]
        if len(rows) < 4:
            continue
        gaps = np.where(np.diff(rows) > 1)[0]
        if not len(gaps):
            continue
        blob = int(rows[gaps[0]] - rows[0] + 1)
        rest = int(rows[-1] - rows[gaps[0] + 1] + 1)
        if rest >= 2 * blob and (best < 0 or rows[0] < best):
            best = int(rows[0])
    return best


def _model_score(runs: list[tuple[str, Run]]) -> float:
    """Mean shifted log10(capital / small) over the readings of one letter (> 0: capital)."""
    scores = [math.log10((r.up + 1e-6) / (r.low + 1e-6)) + MODEL_CASE_SHIFT.get(m, 0.0) for m, r in runs]
    return sum(scores) / len(scores) if scores else -1.0


def decide_case(blind: str, shapes: list[Glyph | None], odds: list[list[tuple[str, Run]]],
                hints: Hints | None = None) -> tuple[str, bool]:
    """(the letters with their case, a close call somewhere). shapes and odds line up with `blind`."""
    hints = hints or Hints()
    if hints.charset in ("upper", "upper_digits"):
        return blind.upper(), False
    if hints.charset in ("lower", "lower_digits"):
        return blind.lower(), False
    tall = [g.height for ch, g in zip(blind, shapes, strict=False)
            if g is not None and ch.isalpha() and ch not in SIZE_CASE and ch not in TAILS and ch != "i"]
    sized = [g.height for ch, g in zip(blind, shapes, strict=False) if g is not None and ch in SIZE_CASE]
    ref = float(sorted(tall)[len(tall) // 2]) if tall else 0.0
    if not ref and sized and max(sized) * SMALL_RATIO > min(sized):
        ref = float(max(sized))  # no always-tall letter: the tallest size-case letter is a capital
    tall_tops = sorted(g.top for ch, g in zip(blind, shapes, strict=False)
                       if g is not None and ch.isalpha() and ch not in SIZE_CASE and ch not in TAILS and ch != "i")
    capline = tall_tops[len(tall_tops) // 2] if tall_tops else 0
    bottoms = [g.bottom for ch, g in zip(blind, shapes, strict=False)
               if g is not None and ch.isalpha() and ch not in TAILS]
    line = sorted(bottoms)[len(bottoms) // 2] if bottoms else 0
    out: list[str] = []
    doubt = False
    for i, ch in enumerate(blind):
        if not ch.isalpha():
            out.append(ch)
            continue
        g = shapes[i] if i < len(shapes) else None
        score = _model_score(odds[i] if i < len(odds) else [])
        by_model = ch.upper() if score > 0 else ch.lower()
        close_model = abs(score) < CASE_MARGIN_LOG
        if g is None:
            out.append(by_model)
            doubt = doubt or close_model
        elif ch in SIZE_CASE and ref:
            ratio = g.height / ref
            out.append(ch.lower() if ratio < SMALL_RATIO else ch.upper())
            doubt = doubt or abs(ratio - SMALL_RATIO) < CLOSE
        elif ch in SIZE_CASE:  # every letter the same size and nothing tall to compare with: the models decide all
            out.append(by_model)
            doubt = doubt or close_model
        elif ch == "i":  # an upright bar: a dot above it is a small i; otherwise a capital I or a small l
            # the dot sits about as high as the tall letters' tops; an e or s next to it has its short top stroke
            # lower down, at the small letters' height
            dot = g.dot >= 0 and (not ref or g.dot < capline + 0.2 * ref)
            if dot:
                out.append("i")
            else:
                said = [r.raw for _m, r in (odds[i] if i < len(odds) else [])]
                out.append("l" if said.count("l") > len(said) / 2 else "I")
        elif ch in TAILS and ref and bottoms:
            hangs = g.bottom - line > 0.15 * ref
            out.append(ch.lower() if hangs else ch.upper())
            doubt = doubt or abs((g.bottom - line) / ref - 0.15) < 0.05
        else:
            out.append(by_model)
            doubt = doubt or close_model
    return "".join(out), doubt


@dataclass
class ColourRead:
    """The combined answer and the per-reading texts (each in the answer's case where it agrees with the vote)."""

    answer: str
    readings: list[str]
    doubt: bool


def combine(readings: list[Reading], layers: list[Any], hints: Hints | None = None) -> ColourRead:
    """Vote without case, then decide each letter's case from its shape and both models' odds."""
    hints = hints or Hints()
    blinds = [r.blind for r in readings]
    blind_set = {"upper": "letters", "lower": "letters", "letters": "letters",
                 "digits": "digits"}.get(hints.charset, "")
    voted = pick([b.lower() for b in blinds], Hints(hints.min_len, hints.max_len, blind_set))
    if not voted:
        return ColourRead("", list(blinds), True)
    voted = voted.lower()
    agreeing = [r for r in readings if r.blind == voted]
    # shapes from the plain layer when a reading of it agrees (it is not stretched), else any agreeing reading
    shapes: list[Glyph | None] = []
    for r in sorted(agreeing, key=lambda r: r.layer):
        shapes = glyphs(layers[r.layer], r)
        if shapes:
            break
    odds: list[list[tuple[str, Run]]] = [[(r.model, r.runs[i]) for r in agreeing] for i in range(len(voted))]
    answer, doubt = decide_case(voted, shapes, odds, hints)
    texts = [answer if r.blind == voted else r.blind for r in readings]
    disagree = len(agreeing) < len(readings)
    plain = next((r for r in agreeing if r.layer == 0), agreeing[0] if agreeing else None)
    skipped = plain is None or missed_ink(layers[plain.layer], plain)
    short = not hints.min_len and len(answer) < MIN_SURE_LEN  # such captchas are 4 to 6 letters
    return ColourRead(answer, texts, doubt or disagree or skipped or short)


__all__ = ["ColourRead", "Glyph", "Reading", "Run", "combine", "decide_case", "glyphs", "ink_layers", "ink_stats",
           "is_bright_ink", "runs_from"]
