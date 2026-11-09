"""Bright coloured captchas (fat, rounded, hot-pink letters of both cases on white, as some college ERPs draw them):
the generated benchmark style, the picture check that sends only such pictures down this path, deciding each letter's
case from its shape, catching a skipped letter so a wrong answer is never taken as sure, the sign-in that retries a
captcha the site turned down without saying so, and (where the captcha model is installed) the read rate against the
old passes on the same pictures. Everything is generated; no real captcha, user name or password."""

from __future__ import annotations

import io
from typing import Any

import pytest

from delta.evals.captcha_bench import ALL_STYLES, KL_LETTERS, STYLES, sample, samples
from delta.tools.captcha import CaptchaSolver
from delta.tools.captcha_colour import (
    Glyph,
    Reading,
    Run,
    combine,
    decide_case,
    glyphs,
    ink_layers,
    is_bright_ink,
    missed_ink,
)
from delta.tools.captcha_model import Readings, installed
from delta.tools.captcha_ocr import Hints


def _img(png: bytes) -> Any:
    from PIL import Image

    return Image.open(io.BytesIO(png))


# --- the generated style ---------------------------------------------------------------------------------------------

def test_kl_style_is_deterministic_and_kept_out_of_the_three_ci_styles():
    assert "kl_bubbly" in ALL_STYLES and "kl_bubbly" not in STYLES  # the old floors are not diluted
    a, b = sample("kl_bubbly", 3), sample("kl_bubbly", 3)
    assert a.png == b.png and a.answer == b.answer
    many = samples(200, styles=("kl_bubbly",))
    assert len(many) == 200 and len({s.answer for s in many}) > 190
    assert all(4 <= len(s.answer) <= 6 and set(s.answer) <= set(KL_LETTERS) for s in many)
    joined = "".join(s.answer for s in many)
    assert any(c.islower() for c in joined) and any(c.isupper() for c in joined)
    for c in "iItTcC":  # the shapes people mix up are in on purpose
        assert c in joined
    assert many[0].attrs == {}  # such a box says nothing about length or letters


def test_only_bright_ink_on_white_takes_the_colour_path():
    for st in ALL_STYLES:
        for s in samples(8, styles=(st,)):
            assert is_bright_ink(_img(s.png)) is (st == "kl_bubbly"), (st, s.seed)
    from PIL import Image

    assert not is_bright_ink(Image.new("RGB", (120, 40), "white"))  # nothing drawn
    assert not is_bright_ink(Image.new("RGB", (120, 40), (230, 70, 120)))  # all pink, no white


def test_layers_isolate_the_ink_by_colour():
    layers = ink_layers(_img(sample("kl_bubbly", 5).png))
    assert len(layers) == 2 and layers[0].height == layers[1].height
    assert layers[1].width > layers[0].width  # the second one stretched sideways
    hist = layers[0].convert("L").histogram()
    dark, light = sum(hist[:64]), sum(hist[192:])
    assert dark > 0.08 * (dark + light) and light > 0.5 * (dark + light)  # black letters on white


# --- case from shape -------------------------------------------------------------------------------------------------

def run(ch: str, up: float = 0.1, low: float = 0.9, raw: str = "") -> tuple[str, Run]:
    return ("default", Run(ch, 0, 0, up, low, raw or ch))


def test_size_case_letters_go_by_height_against_the_tall_ones():
    # "ciiTi"-like: c short, T tall, the i's with dots
    shapes = [Glyph(30, 70), Glyph(10, 70, [(10, 18), (24, 70)], dot=10), Glyph(10, 70, [(10, 70)], dot=-1),
              Glyph(10, 70), Glyph(31, 70)]
    text, doubt = decide_case("ciitc", shapes, [[run("c")], [run("i")], [run("i")], [run("t", 0.9, 0.1)],
                                                 [run("c", 0.9, 0.1)]])
    assert text == "ciITc"  # model odds for the last c ignored: its height says small; the bare bar is a capital I
    assert not doubt
    tall_c = decide_case("cT", [Glyph(11, 70), Glyph(10, 70)], [[run("c")], [run("t", 0.9, 0.1)]])[0]
    assert tall_c == "CT"


def test_tails_and_boxes_decide_the_rest():
    # y hanging below the line the others stand on is small; level with it, a capital
    shapes = [Glyph(10, 70), Glyph(30, 84), Glyph(10, 70)]
    assert decide_case("hyk", shapes, [[run("h")], [run("y", 0.9, 0.1)], [run("k", 0.9, 0.1)]])[0] == "hyK"
    level = [Glyph(10, 70), Glyph(10, 71), Glyph(10, 70)]
    assert decide_case("hyk", level, [[run("h")], [run("y")], [run("k", 0.9, 0.1)]])[0] == "hYK"
    # the box says capitals only: no measuring at all
    assert decide_case("abc", [], [[], [], []], Hints(0, 0, "upper")) == ("ABC", False)


def test_a_letter_the_model_skipped_is_caught():
    from PIL import Image, ImageDraw

    layer = Image.new("L", (300, 96), 255)
    d = ImageDraw.Draw(layer)
    for x in (20, 70, 120, 170, 220):  # five letters, the model only placed three
        d.rectangle((x, 20, x + 30, 80), fill=0)
    # peaks (in model steps) at the right-hand end of the first three letters
    w64 = int(300 * 64 / 96)
    per_step = w64 / 25 * (300 / w64)
    runs = [Run("x", int(x / per_step), int(x / per_step), 0.1, 0.9) for x in (50, 100, 150)]
    assert missed_ink(layer, Reading("default", 0, runs, 25))
    runs5 = [Run("x", int(x / per_step), int(x / per_step), 0.1, 0.9) for x in (50, 100, 150, 200, 250)]
    assert not missed_ink(layer, Reading("default", 0, runs5, 25))
    assert len(glyphs(layer, Reading("default", 0, runs5, 25))) == 5


def test_readings_that_disagree_are_never_sure():
    from PIL import Image

    layers = [Image.new("L", (200, 96), 255)] * 2
    a = Reading("default", 0, [Run(c, i, i, 0.1, 0.9, c) for i, c in enumerate("abcde")], 25)
    b = Reading("beta", 0, [Run(c, i, i, 0.1, 0.9, c) for i, c in enumerate("abcdx")], 25)
    got = combine([a, a, b], layers)
    assert got.answer.lower() == "abcde" and got.doubt


# --- the solver keeps a case measured from the picture -------------------------------------------------------------

class _Audit:
    def record(self, *a: Any, **k: Any) -> None: ...


async def test_windows_ocr_agreeing_on_the_letters_does_not_override_a_measured_case():
    async def model(jpeg: bytes, hints: Hints) -> Readings:
        out = Readings(["ciiTi", "ciiTi", "ciiti"])
        out.case_doubt, out.case_from_shape = True, True
        return out

    async def ocr(jpeg: bytes) -> list[str]:
        return ["CIITI", "CIITI"]

    solver = CaptchaSolver(allowed_sites=list, vision=None, ask_human=None, audit=_Audit(),  # type: ignore[arg-type]
                           model=model, ocr=ocr)
    assert await solver._read(b"x") == "ciiTi" and solver.last_via == "model+ocr"

    async def plain_model(jpeg: bytes, hints: Hints) -> Readings:
        out = Readings(["K7QZ", "K7Q2", "K7QZ"])
        return out

    async def ocr2(jpeg: bytes) -> list[str]:
        return ["k7qz"]

    solver2 = CaptchaSolver(allowed_sites=list, vision=None, ask_human=None, audit=_Audit(),  # type: ignore[arg-type]
                            model=plain_model, ocr=ocr2)
    assert await solver2._read(b"x") == "k7qz"  # the old rule for other captchas: OCR settles the case


async def test_vision_reads_when_the_colour_reader_is_not_sure():
    calls: list[str] = []

    async def model(jpeg: bytes, hints: Hints) -> Readings:
        out = Readings(["cii", "cii", "cii", "cii"])  # every pass agrees, but a letter was skipped
        out.case_doubt, out.case_from_shape = True, True
        return out

    async def vision(b64: str, hint: str = "") -> str:
        calls.append(hint)
        return "ciiTi"

    solver = CaptchaSolver(allowed_sites=list, vision=vision, ask_human=None, audit=_Audit(),  # type: ignore[arg-type]
                           model=model, ocr=None)
    assert await solver._read(sample("kl_bubbly", 1).png) == "ciiTi" and solver.last_via == "vision"
    assert calls == [""] and solver.vision_calls == 1


# --- the read rate (the captcha model installed: CI's full job; the full 200 run is the benchmark step) -------------

@pytest.mark.skipif(not installed(), reason="the captcha model (ddddocr) is not installed")
async def test_colour_path_reads_more_than_the_old_passes_on_the_same_pictures(monkeypatch):
    import delta.tools.captcha_colour as colour
    from delta.evals.captcha_bench import evaluate
    from delta.tools.captcha_model import CaptchaModel

    items = samples(30, start=500, styles=("kl_bubbly",))
    model = CaptchaModel()
    after = (await evaluate(items, model=model.read, tries=1))["model"]["all"]
    monkeypatch.setattr(colour, "is_bright_ink", lambda img: False)  # the passes Delta used before
    before = (await evaluate(items, model=model.read, tries=1))["model"]["all"]
    assert after.right > before.right and after.right_any_case > before.right_any_case
    assert after.rate() >= 0.25 and after.rate_any_case() >= 0.5
