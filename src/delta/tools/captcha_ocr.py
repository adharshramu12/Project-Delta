"""Reading a simple text captcha on this PC: cleaning the picture up for Windows OCR, then turning what OCR read into
the one answer that fits the captcha box. Pure Pillow (already a Delta dependency), nothing to install.

Why several passes: a college-ERP captcha is a few characters with lines and dots drawn over them. Windows OCR reads
clean text well and noisy text badly, and no single clean-up suits every site. So the picture is cleaned five ways
(grayscale and stretched, Otsu threshold, median denoise, thin lines removed, only the darkest ink kept), each enlarged
so a 40-pixel captcha becomes about 120 pixels tall, and the five are stacked into ONE picture with white space
between them. Windows OCR runs once on the stack (each PowerShell start costs seconds on a slow laptop), and each line
it returns is matched to its pass by where it sits.

Then the box itself says what fits: its maxlength / minlength, a pattern such as [0-9]{5}, inputmode="numeric".
Characters that do not fit are dropped, and the usual OCR confusions are fixed when the box says what kind of
characters it takes (a digits-only box reads O as 0, l / I as 1, S as 5; a letters-only box the reverse). The passes
then vote, character by character, among the readings of the most common length (the box's length first).

Nothing here sends anything anywhere, logs anything or keeps the picture.
"""

from __future__ import annotations

import io
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

ANSWER = re.compile(r"^[A-Za-z0-9]{3,10}$")  # what a simple text captcha answer looks like
BAND_HEIGHT = 120  # each pass is enlarged to this height (Windows OCR reads 20-200 px text best)
GAP = 60  # white space between passes, so OCR never joins two passes into one line
MARGIN = 24
MAX_BAND_WIDTH = 900

# OCR's usual mix-ups, fixed only when the box says which kind of characters it takes.
TO_DIGIT = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "U": "0", "l": "1", "I": "1", "i": "1", "|": "1",
                          "!": "1", "L": "1", "j": "1", "J": "1", "Z": "2", "z": "2", "S": "5", "s": "5", "$": "5",
                          "B": "8", "G": "6", "b": "6", "g": "9", "q": "9", "T": "7", "A": "4", "E": "3"})
TO_LETTER = str.maketrans({"0": "O", "1": "I", "5": "S", "2": "Z", "8": "B", "6": "G", "9": "g", "7": "T", "4": "A",
                           "3": "E", "|": "I", "!": "I", "$": "S"})


@dataclass(frozen=True)
class Hints:
    """What the captcha box accepts, from its own attributes. Unknown = no limit."""

    min_len: int = 0
    max_len: int = 0
    charset: str = ""  # "" (any letter or digit) | digits | letters | upper | lower | upper_digits | lower_digits

    @property
    def exact(self) -> int:
        return self.max_len if self.max_len and self.min_len == self.max_len else 0

    def describe(self) -> str:
        """For the vision model's prompt: '5 characters, digits only'."""
        parts = []
        if self.exact:
            parts.append(f"{self.exact} characters")
        elif self.max_len:
            parts.append(f"at most {self.max_len} characters")
        words = {"digits": "digits only", "letters": "letters only", "upper": "capital letters only",
                 "lower": "small letters only", "upper_digits": "capital letters and digits",
                 "lower_digits": "small letters and digits"}
        if self.charset in words:
            parts.append(words[self.charset])
        return ", ".join(parts)


NO_HINTS = Hints()


def _int(v: Any) -> int:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return 0
    return n if 0 < n <= 32 else 0


def hints_from(attrs: dict[str, Any] | None) -> Hints:
    """Hints from the box's attributes: maxlength, minlength, pattern, inputmode, type. Anything odd is ignored (a
    hint only narrows what is accepted; a wrong hint must never stop a read, so unknown patterns mean 'any')."""
    a = {str(k).lower(): v for k, v in (attrs or {}).items() if v is not None}
    max_len, min_len = _int(a.get("maxlength")), _int(a.get("minlength"))
    charset = ""
    pattern = str(a.get("pattern") or "").strip()
    if pattern and len(pattern) <= 80:
        body = pattern.strip("^$")
        if m := re.search(r"\{(\d+)(?:,(\d*))?\}$", body):
            lo = _int(m[1])
            hi = _int(m[2]) if m[2] else (lo if m[2] is None else 0)
            min_len = min_len or lo
            max_len = max_len or hi
        classes = "".join(re.findall(r"\[([^\]]*)\]", body)) + re.sub(r"\[[^\]]*\]", "", body)
        digits = "0-9" in classes or "\\d" in classes
        upper, lower = "A-Z" in classes, "a-z" in classes
        if "\\w" in classes or "." in re.sub(r"\\.", "", re.sub(r"\[[^\]]*\]", "", body)):
            charset = ""
        elif digits and not upper and not lower:
            charset = "digits"
        elif upper and lower and not digits:
            charset = "letters"
        elif upper and not lower:
            charset = "upper_digits" if digits else "upper"
        elif lower and not upper:
            charset = "lower_digits" if digits else "lower"
    if not charset and (str(a.get("inputmode") or "").lower() == "numeric" or str(a.get("type") or "").lower()
                        == "number"):
        charset = "digits"
    if max_len and min_len > max_len:
        min_len = 0
    return Hints(min_len, max_len, charset)


def fit(text: str, hints: Hints | None = None) -> str | None:
    """One OCR reading made to fit the box, or None when it cannot be a captcha answer."""
    hints = hints or NO_HINTS
    raw = re.sub(r"\s+", "", text or "")
    if hints.charset == "digits":
        raw = raw.translate(TO_DIGIT)
    elif hints.charset in ("letters", "upper", "lower"):
        raw = raw.translate(TO_LETTER)
    if hints.charset in ("upper", "upper_digits"):
        raw = raw.upper()
    elif hints.charset in ("lower", "lower_digits"):
        raw = raw.lower()
    keep = {"digits": r"[^0-9]", "letters": r"[^A-Za-z]", "upper": r"[^A-Z]", "lower": r"[^a-z]",
            "upper_digits": r"[^A-Z0-9]", "lower_digits": r"[^a-z0-9]"}.get(hints.charset, r"[^A-Za-z0-9]")
    token = re.sub(keep, "", raw)
    if hints.max_len and len(token) > hints.max_len:
        return None  # more characters than the box takes: a misread (noise taken for letters), not a guess to trim
    if hints.min_len and len(token) < hints.min_len:
        return None
    return token if ANSWER.match(token) else None


def pick(readings: list[str], hints: Hints | None = None) -> str | None:
    """The answer the passes agree on: readings that fit, the most common length (the box's exact length first), then
    a vote per character (ties go to the earlier, cleaner pass). None when no reading fits."""
    hints = hints or NO_HINTS
    fitted = [f for r in readings if (f := fit(r, hints))]
    if not fitted:
        return None
    lengths = Counter(len(f) for f in fitted)
    if hints.exact and hints.exact in lengths:
        size = hints.exact
    else:
        size = max(lengths, key=lambda n: (lengths[n], -[len(f) for f in fitted].index(n)))
    same = [f for f in fitted if len(f) == size]
    out = []
    for i in range(size):
        votes = Counter(f[i] for f in same)
        best = max(votes.values())
        out.append(next(f[i] for f in same if votes[f[i]] == best))
    return "".join(out)


# --------------------------------------------------------------------------------- the picture

def _otsu(gray: Any) -> int:
    """Otsu's threshold of an L picture (pure Python over its 256-bin histogram)."""
    hist = gray.histogram()[:256]
    total = sum(hist)
    if not total:
        return 128
    sum_all = sum(i * h for i, h in enumerate(hist))
    w_b = sum_b = 0.0
    best, best_t = -1.0, 128
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b, m_f = sum_b / w_b, (sum_all - sum_b) / w_f
        between = w_b * w_f * (m_b - m_f) ** 2
        if between > best:
            best, best_t = between, t
    return best_t


def _percentile(gray: Any, p: float) -> int:
    hist = gray.histogram()[:256]
    want, seen = sum(hist) * p, 0
    for i, h in enumerate(hist):
        seen += h
        if seen >= want:
            return i
    return 255


def _binary(gray: Any, t: int) -> Any:
    return gray.point(lambda v: 0 if v <= t else 255, mode="L")


def variants(picture: Any) -> list[Any]:
    """The captcha cleaned five ways, each an L picture BAND_HEIGHT tall with dark text on white:
    0 grayscale + contrast, 1 Otsu threshold, 2 median denoise + threshold, 3 thin lines removed, 4 darkest ink only."""
    from PIL import Image, ImageFilter, ImageOps

    img = picture.convert("RGB")
    gray = ImageOps.autocontrast(img.convert("L"), cutoff=1)
    hist = gray.histogram()
    if sum(i * h for i, h in enumerate(hist)) / max(1, sum(hist)) < 110:  # light text on a dark picture
        gray = ImageOps.invert(gray)
    scale = BAND_HEIGHT / max(1, gray.height)
    width = max(1, min(MAX_BAND_WIDTH, round(gray.width * scale)))
    big = gray.resize((width, BAND_HEIGHT), Image.Resampling.LANCZOS)
    k = 5 if scale >= 2 else 3  # line-removal size: wider than a drawn line, thinner than a letter's stroke
    otsu = _binary(big, _otsu(big))
    calm = big.filter(ImageFilter.MedianFilter(5))
    denoised = _binary(calm, _otsu(calm))
    # an opening of the dark ink (MaxFilter grows the white, MinFilter grows the dark back): lines and dots thinner
    # than k pixels vanish, letters come back at their size; a final median smooths the edges
    lines_gone = otsu.filter(ImageFilter.MaxFilter(k)).filter(ImageFilter.MinFilter(k)).filter(
        ImageFilter.MedianFilter(3))
    dark = _binary(calm, min(_otsu(calm), _percentile(calm, 0.18)))
    return [big, otsu, denoised, lines_gone, dark]


def stack(bands: list[Any]) -> tuple[Any, list[tuple[int, int]]]:
    """One white picture with the bands one under another: (picture, [(top, bottom) of each band])."""
    from PIL import Image

    width = max(b.width for b in bands) + 2 * MARGIN
    height = sum(b.height for b in bands) + GAP * (len(bands) - 1) + 2 * MARGIN
    sheet = Image.new("L", (width, height), 255)
    spans, y = [], MARGIN
    for b in bands:
        sheet.paste(b, (MARGIN, y))
        spans.append((y, y + b.height))
        y += b.height + GAP
    return sheet, spans


def band_readings(lines: list[Any], spans: list[tuple[int, int]]) -> list[str]:
    """What OCR read in each band, in band order (the words of a band joined without spaces: "K7 Q2" is one answer).
    `lines` are delta.perception.ocr.OcrLine (text, box=(x, y, w, h), words)."""
    found: dict[int, list[tuple[int, str]]] = {}
    for line in lines:
        words = list(getattr(line, "words", ()) or ()) or [(line.text, line.box)]
        for text, box in words:
            mid = box[1] + box[3] / 2
            for i, (top, bottom) in enumerate(spans):
                if top - GAP / 2 <= mid <= bottom + GAP / 2:
                    found.setdefault(i, []).append((box[0], str(text)))
                    break
    return ["".join(t for _x, t in sorted(found[i])) for i in sorted(found)]


def small_crop(jpeg: bytes, max_w: int = 256, max_h: int = 72) -> bytes:
    """The captcha for a vision model: trimmed to its ink with a small border and made small (a few hundred bytes,
    the lowest image price), as a JPEG. The original bytes when it cannot be read as a picture."""
    try:
        from PIL import Image, ImageOps

        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        gray = ImageOps.autocontrast(img.convert("L"), cutoff=1)
        ink = gray.point(lambda v: 255 if v < 200 else 0, mode="L").getbbox()
        if ink:
            pad = 4
            img = img.crop((max(0, ink[0] - pad), max(0, ink[1] - pad), min(img.width, ink[2] + pad),
                            min(img.height, ink[3] + pad)))
        img.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=80)
        return out.getvalue()
    except Exception:
        return jpeg


__all__ = ["Hints", "band_readings", "fit", "hints_from", "pick", "small_crop", "stack", "variants"]
