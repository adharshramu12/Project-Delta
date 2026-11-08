"""A benchmark of generated text captchas like the ones common Indian college and government ERP portals draw, for
measuring how often Delta reads a simple captcha by itself (tests/test_captcha_bench.py, `python -m
delta.evals.captcha_bench`).

Three styles, each deterministic for its seed, drawn with Pillow (already a Delta dependency) and its built-in
scalable font, so a Linux box and a Windows runner draw the same pixels:

  lines_dots   5 capital letters and digits, dark and slightly shifted, on a pale background, with thin crossing
               lines and a sprinkle of coloured dots (the classic PHP / ASP.NET ERP captcha)
  distorted    4 to 6 letters (both cases) and digits, each rotated up to 25 degrees, different sizes and colours,
               squeezed together until they touch, the whole picture bent by a gentle wave, one thick arc through it
  coloured     5 or 6 digits, each in its own strong colour, on a speckled coloured background with a few coloured
               lines (the "enter the digits" captcha of many state portals)

Like those portals, the alphabets leave out the characters people cannot tell apart either (0/O, 1/I/l, g/q/9, and
in the mixed-case style every letter whose small and capital forms have the same shape, such as c/C, s/S, v/V).
Everything here is made up; nothing is sent anywhere.
"""

from __future__ import annotations

import io
import math
import random
from dataclasses import dataclass
from typing import Any

STYLES = ("lines_dots", "distorted", "coloured")
# Styles measured on their own (never folded into the three above, whose floors CI already holds):
#   kl_bubbly    about 5 fat, rounded, hot-pink letters on white, both cases, touching and overlapping, each turned a
#                little: the look of some college ERP sign-in captchas, with the letters people mix up (i l I t T c)
EXTRA_STYLES = ("kl_bubbly",)
ALL_STYLES = (*STYLES, *EXTRA_STYLES)
# Both cases, the shapes people mix up kept in on purpose (i/I, t/T, c/C, x/X, z/Z, k/K, y/Y: some differ only in
# size): the hard part of such captchas. No small l: in the drawing font it is the very same bar as a capital I, so
# no reader (or person) could tell them apart and the score would measure coin flips.
KL_LETTERS = "abcdefhikmnprstuxyzABCDEFGHIKMNRTXYZcitT"
UPPER_DIGITS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MIXED = "ABDEFGHJLMNQRTYabdefghnrty2345678"  # both cases only where they look different; no q or 9 (they look like g)
DIGITS = "0123456789"


@dataclass(frozen=True)
class Sample:
    style: str
    seed: int
    answer: str
    png: bytes
    attrs: dict[str, str]  # the captcha box's attributes on such a portal (what Delta reads as hints)


def _font(size: int) -> Any:
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow without the scalable default font
        return ImageFont.load_default()


def _png(img: Any) -> bytes:
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def _lines_dots(rng: random.Random) -> tuple[str, Any]:
    from PIL import Image, ImageDraw

    answer = "".join(rng.choice(UPPER_DIGITS) for _ in range(5))
    w, h = 160, 50
    img = Image.new("RGB", (w, h), (232 + rng.randrange(20), 232 + rng.randrange(20), 222 + rng.randrange(30)))
    draw = ImageDraw.Draw(img)
    font = _font(30)
    x = 10 + rng.randrange(6)
    for ch in answer:
        draw.text((x, 7 + rng.randrange(-3, 5)), ch, font=font,
                  fill=(rng.randrange(10, 70), rng.randrange(10, 70), rng.randrange(40, 110)))
        x += 27 + rng.randrange(-2, 3)
    for _ in range(5):
        draw.line((rng.randrange(w), rng.randrange(h), rng.randrange(w), rng.randrange(h)), width=1,
                  fill=(rng.randrange(60, 160), rng.randrange(60, 160), rng.randrange(60, 160)))
    for _ in range(160):
        draw.point((rng.randrange(w), rng.randrange(h)),
                   fill=(rng.randrange(40, 200), rng.randrange(40, 200), rng.randrange(40, 200)))
    return answer, img


def _distorted(rng: random.Random) -> tuple[str, Any]:
    from PIL import Image, ImageDraw

    answer = "".join(rng.choice(MIXED) for _ in range(rng.choice((4, 5, 5, 6))))
    bg = (240 + rng.randrange(15), 240 + rng.randrange(15), 240 + rng.randrange(15))
    tiles = []
    for ch in answer:
        size = rng.randrange(28, 38)
        tile = Image.new("RGBA", (size + 16, size + 16), (0, 0, 0, 0))
        colour = (rng.randrange(0, 90), rng.randrange(0, 90), rng.randrange(0, 140), 255)
        ImageDraw.Draw(tile).text((8, 4), ch, font=_font(size), fill=colour)
        tile = tile.rotate(rng.uniform(-25, 25), resample=Image.Resampling.BICUBIC, expand=True)
        tiles.append(tile.crop(tile.getbbox() or (0, 0, tile.width, tile.height)))
    # letters squeezed together until they touch (up to 2 px into each other), never on top of one another: a
    # person can still read every one, as on a real portal
    gaps = [rng.randrange(-2, 4) for _ in tiles]
    h = 60
    w = 24 + sum(t.width for t in tiles) + sum(gaps)
    img = Image.new("RGB", (w, h), bg)
    x = 12
    for tile, gap in zip(tiles, gaps, strict=True):
        img.paste(tile, (x, max(0, (h - tile.height) // 2 + rng.randrange(-4, 5))), tile)
        x += tile.width + gap
    # a gentle vertical wave: each column moved up or down a few pixels
    amp, period, phase = rng.uniform(2, 4), rng.uniform(45, 80), rng.uniform(0, 6.28)
    bent = Image.new("RGB", (w, h), bg)
    for cx in range(w):
        dy = int(amp * math.sin(2 * math.pi * cx / period + phase))
        bent.paste(img.crop((cx, 0, cx + 1, h)), (cx, dy))
    draw = ImageDraw.Draw(bent)
    x0, y0 = rng.randrange(-20, 10), rng.randrange(10, 30)
    draw.arc((x0, y0, x0 + w + 20, y0 + rng.randrange(40, 80)), rng.randrange(180, 220), rng.randrange(320, 360),
             fill=(rng.randrange(0, 90), rng.randrange(0, 90), rng.randrange(0, 140)), width=2)
    return answer, bent


def _coloured(rng: random.Random) -> tuple[str, Any]:
    from PIL import Image, ImageDraw

    answer = "".join(rng.choice(DIGITS) for _ in range(rng.choice((5, 6))))
    w, h = 30 + 26 * len(answer), 48
    base = (rng.randrange(170, 230), rng.randrange(190, 240), rng.randrange(170, 240))
    img = Image.new("RGB", (w, h), base)
    px: Any = img.load()
    for yy in range(h):
        for xx in range(w):
            if rng.random() < 0.35:
                px[xx, yy] = tuple(max(0, min(255, c + rng.randrange(-45, 46))) for c in base)
    draw = ImageDraw.Draw(img)
    for _ in range(4):
        draw.line((rng.randrange(w), rng.randrange(h), rng.randrange(w), rng.randrange(h)), width=1,
                  fill=(rng.randrange(80, 200), rng.randrange(80, 200), rng.randrange(80, 200)))
    strong = [(200, 20, 20), (20, 110, 20), (20, 40, 200), (150, 20, 150), (190, 90, 0), (0, 110, 130), (60, 60, 60)]
    x = 12 + rng.randrange(5)
    for ch in answer:
        r, g, b = rng.choice(strong)
        draw.text((x, 6 + rng.randrange(-3, 5)), ch, font=_font(rng.randrange(28, 34)), fill=(r, g, b))
        x += 24 + rng.randrange(-1, 3)
    return answer, img


def _kl_bubbly(rng: random.Random) -> tuple[str, Any]:
    """Fat rounded pink letters: each drawn big with a thick outline, blurred and thresholded so every corner comes
    out round, turned up to 12 degrees, pushed into its neighbour (up to 8 px), all on one baseline, then the whole
    picture made small the way a browser shows it."""
    from PIL import Image, ImageDraw, ImageFilter

    answer = "".join(rng.choice(KL_LETTERS) for _ in range(rng.choice((5, 5, 5, 4, 6))))
    pink = (rng.randrange(215, 241), rng.randrange(50, 86), rng.randrange(100, 136))
    size = rng.randrange(58, 68)  # one font size for the picture, as such sites draw it
    stroke = rng.randrange(2, 4)  # fatter would close up the dot of an i, which no reader could then tell
    tall = size * 2
    tiles = []
    for ch in answer:
        # every letter on the same baseline (anchor "ls"), so small letters stay small and tails hang below
        tile = Image.new("L", (size * 2, tall), 0)
        ImageDraw.Draw(tile).text((size // 2, round(tall * 0.68)), ch, font=_font(size + rng.randrange(-2, 3)),
                                  fill=255, anchor="ls", stroke_width=stroke, stroke_fill=255)
        tile = tile.filter(ImageFilter.GaussianBlur(1.6)).point(lambda v: 255 if v > 100 else 0)
        tile = tile.rotate(rng.uniform(-12, 12), resample=Image.Resampling.BICUBIC, center=(size, round(tall * 0.6)))
        box = tile.getbbox() or (0, 0, tile.width, tile.height)
        tiles.append(tile.crop((box[0], 0, box[2], tall)))  # trimmed sideways only: the baseline stays put
    gaps = [rng.randrange(-8, 1) for _ in tiles]
    h = tall
    w = 40 + sum(t.width for t in tiles) + sum(gaps)
    mask = Image.new("L", (w, h), 0)
    x = 20
    for tile, gap in zip(tiles, gaps, strict=True):
        mask.paste(255, (x, rng.randrange(-3, 4)), tile)
        x += tile.width + gap
    box = mask.getbbox() or (0, 0, w, h)
    mask = mask.crop((0, max(0, box[1] - 14), w, min(h, box[3] + 14)))
    w, h = mask.size
    mask = mask.filter(ImageFilter.GaussianBlur(0.8))
    img = Image.new("RGB", (w, h), (255, 255, 255))
    img.paste(Image.new("RGB", (w, h), pink), (0, 0), mask)
    scale = rng.uniform(0.5, 0.8)  # the size it is shown at on the page
    return answer, img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)


_DRAW = {"lines_dots": _lines_dots, "distorted": _distorted, "coloured": _coloured, "kl_bubbly": _kl_bubbly}
_ATTRS = {"kl_bubbly": lambda a: {},  # such a box says nothing about the length or the letters
          "lines_dots": lambda a: {"maxlength": "5"},
          "distorted": lambda a: {"maxlength": "6"},
          "coloured": lambda a: {"maxlength": str(len(a)), "inputmode": "numeric"}}


def sample(style: str, seed: int) -> Sample:
    rng = random.Random(f"{style}:{seed}")
    answer, img = _DRAW[style](rng)
    return Sample(style, seed, answer, _png(img), _ATTRS[style](answer))


def samples(per_style: int = 40, start: int = 0, styles: tuple[str, ...] = STYLES) -> list[Sample]:
    """per_style captchas of each style (120 by default), the same pictures on every machine."""
    return [sample(st, start + i) for st in styles for i in range(per_style)]


def matches(got: str | None, answer: str, case_sensitive: bool = True) -> bool:
    if not got:
        return False
    return got == answer if case_sensitive else got.lower() == answer.lower()


# ------------------------------------------------------------------------------------------------- measuring

RETRY_SEEDS = (0, 10_000, 20_000)  # a refreshed captcha is a new picture: tries 2 and 3 read these seeds


@dataclass
class Score:
    right: int = 0
    right_any_case: int = 0
    total: int = 0
    vision_wanted: int = 0  # times the combined reader would have made its one vision call (none is made here)
    wrong_flagged: int = 0  # wrong first answers the combined reader was not sure of (a vision call would be made)
    wrong: int = 0

    def rate(self) -> float:
        return self.right / self.total if self.total else 0.0

    def rate_any_case(self) -> float:
        return self.right_any_case / self.total if self.total else 0.0


def _solver(model: Any, ocr: Any, want: list[int]) -> Any:
    """A CaptchaSolver with the given engines and a vision stand-in that only counts (it never answers)."""
    from delta.tools.captcha import CaptchaSolver

    async def no_vision(b64: str, hint: str = "") -> str:
        want[0] += 1
        return ""

    class _Audit:
        def record(self, *a: Any, **k: Any) -> None: ...

    return CaptchaSolver(allowed_sites=list, vision=no_vision, ask_human=None, audit=_Audit(),  # type: ignore[arg-type]
                         model=model, ocr=ocr)


async def evaluate(items: list[Sample], model: Any = None, ocr: Any = None,
                   tries: int = 3) -> dict[str, dict[str, Score]]:
    """Exact-match scores per engine and style, plus 'combined' (Delta's own order: model, then Windows OCR when the
    model is not sure, never a vision call) and 'combined_3' (up to `tries` pictures, a new one after each miss).
    Engines that are None are left out. Keys: engine -> style (and 'all') -> Score."""
    from delta.tools.captcha_ocr import hints_from, pick

    out: dict[str, dict[str, Score]] = {}

    def add(engine: str, style: str, got: str | None, answer: str, wanted: int = 0) -> None:
        for key in (style, "all"):
            sc = out.setdefault(engine, {}).setdefault(key, Score())
            sc.total += 1
            sc.right += int(matches(got, answer))
            sc.right_any_case += int(matches(got, answer, case_sensitive=False))
            sc.vision_wanted += wanted
            if not matches(got, answer):
                sc.wrong += 1
                sc.wrong_flagged += int(bool(wanted))

    if ocr is not None:  # Windows OCR takes seconds a picture: each picture is read once, for both rows
        seen: dict[bytes, Any] = {}
        raw_ocr = ocr

        async def ocr(jpeg: bytes) -> Any:
            if jpeg not in seen:
                seen[jpeg] = await raw_ocr(jpeg)
            return seen[jpeg]

    want = [0]
    solver = _solver(model, ocr, want) if (model or ocr) else None
    for item in items:
        hints = hints_from(item.attrs)
        if model is not None:
            add("model", item.style, pick(await model(item.png, hints), hints), item.answer)
        if ocr is not None:
            got = await ocr(item.png)
            add("windows_ocr", item.style, pick(got if isinstance(got, list) else [str(got)], hints), item.answer)
        if solver is None:
            continue
        before = want[0]
        first = await solver._read(item.png, hints)
        add("combined", item.style, first, item.answer, want[0] - before)
        ok = matches(first, item.answer)
        for extra in RETRY_SEEDS[1:tries]:
            if ok:
                break
            again = sample(item.style, item.seed + extra)
            ok = matches(await solver._read(again.png, hints_from(again.attrs)), again.answer)
        add("combined_3", item.style, item.answer if ok else None, item.answer)
    return out


def table(scores: dict[str, dict[str, Score]]) -> str:
    """The scores as a Markdown table: exact match (case-blind in brackets) per engine and style."""
    cols = [*STYLES, "all"]
    lines = ["| engine | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
    for engine, by in scores.items():
        cells = []
        for c in cols:
            sc = by.get(c)
            cells.append("-" if sc is None else
                         f"{sc.right}/{sc.total} = {sc.rate():.0%} ({sc.rate_any_case():.0%})")
        lines.append(f"| {engine} | " + " | ".join(cells) + " |")
    comb = scores.get("combined", {}).get("all")
    if comb is not None:
        lines.append("")
        lines.append(f"combined would have made {comb.vision_wanted} small vision calls on {comb.total} captchas "
                     f"(when a configured model takes pictures; none were made here). {comb.wrong_flagged} of its "
                     f"{comb.wrong} wrong first answers were among them; the other {comb.wrong - comb.wrong_flagged} "
                     "would be typed as sure.")
    return "\n".join(lines)


async def _windows_ocr() -> Any:
    from delta.perception.ocr import WindowsOcr
    from delta.tools.captcha import stacked_reader

    engine = WindowsOcr("auto")
    if not engine.platform_ok() or not await engine.probe():
        return None
    return stacked_reader(engine)


async def _main(argv: list[str] | None = None) -> int:
    import argparse

    from delta.tools.captcha_model import shared_model

    p = argparse.ArgumentParser(prog="python -m delta.evals.captcha_bench",
                                description="How often Delta reads generated ERP-style captchas by itself.")
    p.add_argument("--per-style", type=int, default=40)
    p.add_argument("--no-windows-ocr", action="store_true")
    p.add_argument("--out", default="")
    p.add_argument("--min-combined", type=float, default=0.0, help="fail below this first-try rate (0..1)")
    p.add_argument("--min-three", type=float, default=0.0, help="fail below this rate within three tries (0..1)")
    args = p.parse_args(argv)
    import logging

    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))  # no per-pass lines
    m = shared_model()
    ocr = None if args.no_windows_ocr else await _windows_ocr()
    scores = await evaluate(samples(args.per_style), model=m.read if m else None, ocr=ocr)
    text = (f"Captcha benchmark: {args.per_style} per style, {len(STYLES)} styles. Captcha model: "
            f"{'yes' if m else 'not installed'}. Windows OCR: {'yes' if ocr else 'not available'}.\n\n" + table(scores))
    print(text)
    if args.out:
        from pathlib import Path

        Path(args.out).write_text(text + "\n", encoding="utf-8")
    first = scores.get("combined", {}).get("all", Score()).rate()
    three = scores.get("combined_3", {}).get("all", Score()).rate()
    if first < args.min_combined or three < args.min_three:
        print(f"below target: first try {first:.1%} (want {args.min_combined:.0%}), within three {three:.1%} "
              f"(want {args.min_three:.0%})")
        return 1
    return 0


if __name__ == "__main__":
    import asyncio
    import sys

    sys.exit(asyncio.run(_main()))


__all__ = ["DIGITS", "MIXED", "RETRY_SEEDS", "STYLES", "UPPER_DIGITS", "Sample", "Score", "evaluate", "matches",
           "sample", "samples", "table"]
