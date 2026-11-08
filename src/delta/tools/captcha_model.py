"""The first captcha reader: a small offline model made for simple text captchas (ddddocr, MIT licence, an ONNX model
run on the CPU by onnxruntime). Installed by Delta's installer (the `captcha` extra, part of `--extra all`); when it is
missing or cannot load (an unusual PC, a broken install) Delta falls back to Windows OCR and says nothing more.

How a reading is made, for one picture:

1. the model runs on three cleaned-up versions of the picture (grayscale with the contrast stretched, Otsu threshold,
   median denoise; delta.tools.captcha_ocr.variants), about 25 ms each on a laptop CPU;
2. its per-step probabilities are decoded HERE, not by ddddocr's own text output, so the captcha box's rules apply
   inside the model's choice instead of after it: only the characters the box takes (digits only, capitals only, ...)
   can win a step, and a letter is first chosen without its case (the model often knows it is a "v" but not which v);
3. the case is then decided per letter, leaning towards capitals (UPPER_BIAS): the model under-reads capitals on noisy
   pictures, and most ERP captchas are capitals or case-blind. A box that says capitals only or small letters only
   decides the case itself.

The three readings then vote (delta.tools.captcha_ocr.pick). When all three agree and no letter's case was a close
call (CASE_MARGIN) the answer is taken as sure; otherwise delta.tools.captcha asks Windows OCR and, if the two engines
still disagree, makes one small vision call.

Nothing here sends anything anywhere or keeps the picture. Readings are never logged (delta.tools.captcha logs only
lengths and agreement, and the readings themselves only with DELTA_CAPTCHA_DEBUG=1).
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import threading
from dataclasses import dataclass
from typing import Any

from delta.tools.captcha_ocr import Hints

# A capital wins a letter when its probability times this beats the small letter's. Chosen on benchmark seeds that
# are not the reported ones (delta.evals.captcha_bench, seeds 1000+): 15 read every capitals-only captcha right and
# lost the fewest mixed-case ones.
UPPER_BIAS = 15.0
# A letter's case is a close call when the (biased) capital and small-letter odds are within this factor; chosen on
# the same seeds: at 3, all but one wrongly cased answer was flagged and about half the letter captchas were.
CASE_MARGIN = 3.0
# captcha_ocr.variants() 0, 1 and 2: grayscale with the contrast stretched, Otsu threshold, median denoise + threshold.
# Chosen on the same seeds (the picture as it is read worse than all three of these).
PASSES = (0, 1, 2)

_ALLOWED = {
    "digits": lambda c: c.isdigit(),
    "letters": lambda c: c.isalpha(),
    "upper": lambda c: c.isalpha(),
    "lower": lambda c: c.isalpha(),
    "upper_digits": lambda c: True,
    "lower_digits": lambda c: True,
}


def _ascii_alnum(c: str) -> bool:
    return len(c) == 1 and c.isascii() and c.isalnum()


class Readings(list[str]):
    """The model's readings, one per pass, and whether the case of any letter was a close call (case_doubt): then the
    answer is not taken as sure even when every pass agrees, and Windows OCR (or one vision call) gets a say."""

    case_doubt: bool = False


def decode(probs: Any, charset: list[str], hints: Hints | None = None, upper_bias: float = UPPER_BIAS) -> str:
    """decode_detail without the case doubt."""
    return decode_detail(probs, charset, hints, upper_bias)[0]


def decode_detail(probs: Any, charset: list[str], hints: Hints | None = None,
                  upper_bias: float = UPPER_BIAS) -> tuple[str, bool]:
    """(text, case_doubt) in one model output: probs is (steps, classes) or (steps, 1, classes) of softmax
    probabilities over `charset` (index 0 is the CTC blank). Only ASCII letters and digits the box takes can win; a
    letter is picked without its case first, then its case is decided (by the box, or by the model with the capital
    bias). case_doubt: the model's case choice for some letter was within CASE_MARGIN of the other case."""
    import numpy as np

    hints = hints or Hints()
    p = np.asarray(probs, dtype=np.float64)
    if p.ndim == 3:
        p = p[:, 0, :] if p.shape[1] == 1 else p[0]
    if p.ndim != 2 or p.shape[0] == 0 or p.shape[1] != len(charset):
        return "", False
    ok = _ALLOWED.get(hints.charset, lambda c: True)
    groups: dict[str, list[int]] = {}
    for i, c in enumerate(charset):
        if i and _ascii_alnum(c) and ok(c):
            groups.setdefault(c.lower(), []).append(i)
    if not groups:
        return "", False
    keys = list(groups)
    merged = np.stack([p[:, 0], *(p[:, groups[k]].sum(axis=1) for k in keys)], axis=1)
    best = merged.argmax(axis=1)
    # CTC: a run of the same class is one character, the blank (0) separates repeats
    runs: list[tuple[int, int, int]] = []
    prev = 0
    for t, j in enumerate(best.tolist()):
        if j and j == prev:
            runs[-1] = (j, runs[-1][1], t)
        elif j:
            runs.append((j, t, t))
        prev = j
    index = {c: i for i, c in enumerate(charset) if i and _ascii_alnum(c)}
    out = []
    doubt = False
    for j, a, b in runs:
        ch = keys[j - 1]
        if ch.isalpha():
            if hints.charset in ("upper", "upper_digits"):
                ch = ch.upper()
            elif hints.charset in ("lower", "lower_digits"):
                ch = ch.lower()
            else:
                up = float(p[a:b + 1, index[ch.upper()]].sum()) if ch.upper() in index else 0.0
                low = float(p[a:b + 1, index[ch.lower()]].sum()) if ch.lower() in index else 0.0
                ch = ch.upper() if up * upper_bias > low else ch.lower()
                if up and low:
                    ratio = up * upper_bias / low
                    doubt = doubt or 1 / CASE_MARGIN < ratio < CASE_MARGIN
        out.append(ch)
    return "".join(out), doubt


def installed() -> bool:
    """The model's packages are on this PC (checked without importing them)."""
    try:
        return importlib.util.find_spec("ddddocr") is not None and importlib.util.find_spec("onnxruntime") is not None
    except (ImportError, ValueError):
        return False


@dataclass
class ModelStatus:
    ready: bool
    why: str = ""


class CaptchaModel:
    """ddddocr's default model, loaded once on first use (about a second), then about 25 ms a picture. Its second
    bundled model ("beta", the same package, no download) is loaded only for bright coloured captchas
    (delta.tools.captcha_colour), where the two read together."""

    def __init__(self) -> None:
        self._engine: Any = None
        self._beta: Any = None
        self._lock = threading.Lock()
        self.status = ModelStatus(installed(), "" if installed() else "the captcha model is not installed")

    def available(self) -> bool:
        return self.status.ready

    def _load(self) -> Any:
        with self._lock:
            if self._engine is None:
                import contextlib
                import os

                # ddddocr prints a banner on import and load; it goes nowhere
                with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
                    import ddddocr

                    self._engine = ddddocr.DdddOcr(show_ad=False)
            return self._engine

    def _load_beta(self) -> Any:
        """The second model, or None when this ddddocr has none (it then reads alone)."""
        with self._lock:
            if self._beta is None:
                import contextlib
                import os

                try:
                    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
                        import ddddocr

                        self._beta = ddddocr.DdddOcr(show_ad=False, beta=True)
                except Exception:
                    self._beta = False
            return self._beta or None

    def _read_colour(self, img: Any, hints: Hints | None) -> Readings:
        """Bright coloured letters on white: the ink by colour, two layers, both models, case from the shapes."""
        from delta.tools.captcha_colour import Reading, combine, ink_layers, runs_from

        layers = ink_layers(img)
        found: list[Reading] = []
        engines = [("default", self._load()), ("beta", self._load_beta())]
        for name, engine in engines:
            if engine is None:
                continue
            for i, layer in enumerate(layers):
                buf = io.BytesIO()
                layer.save(buf, "PNG")
                got = engine.classification(buf.getvalue(), probability=True)
                if isinstance(got, dict):
                    runs, steps = runs_from(got.get("probabilities"), list(got.get("charset") or []), hints)
                    found.append(Reading(name, i, runs, steps))
        result = combine(found, layers, hints)
        out = Readings(result.readings)
        out.case_doubt = result.doubt
        return out

    def read_sync(self, picture: bytes, hints: Hints | None = None) -> Readings:
        """One reading per pass (PASSES), each already fitted to the box's character rules. [] when the model cannot
        run here (and it is then marked unavailable, so the next captcha goes straight to Windows OCR)."""
        out = Readings()
        if not self.status.ready:
            return out
        try:
            engine = self._load()
            from PIL import Image

            from delta.tools.captcha_colour import is_bright_ink
            from delta.tools.captcha_ocr import variants

            img = Image.open(io.BytesIO(picture))
            if is_bright_ink(img):
                return self._read_colour(img, hints)
            bands = variants(img)
            for which in PASSES:
                buf = io.BytesIO()
                bands[which].save(buf, "PNG")
                got = engine.classification(buf.getvalue(), probability=True)
                text, doubt = (decode_detail(got.get("probabilities"), list(got.get("charset") or []), hints)
                               if isinstance(got, dict) else ("", False))
                out.append(text)
                out.case_doubt = out.case_doubt or doubt
            return out
        except Exception as e:  # a broken onnxruntime, an unusual CPU: Windows OCR still reads
            self.status = ModelStatus(False, f"the captcha model could not run here ({type(e).__name__})")
            return Readings()

    async def read(self, picture: bytes, hints: Hints | None = None) -> Readings:
        return await asyncio.to_thread(self.read_sync, picture, hints)


_shared: CaptchaModel | None = None


def shared_model() -> CaptchaModel | None:
    """The one model Delta loads (None when it is not installed)."""
    global _shared
    if _shared is None:
        _shared = CaptchaModel()
    return _shared if _shared.available() else None


__all__ = ["CASE_MARGIN", "PASSES", "UPPER_BIAS", "CaptchaModel", "ModelStatus", "Readings", "decode", "decode_detail",
           "installed", "shared_model"]
