"""Reading simple captchas well enough to sign in by itself: the captcha model's decoding (held to the box's rules,
case decided with a lean to capitals and a doubt flag), the order model -> Windows OCR -> one vision call when they are
not sure, the per-pass log that never holds a reading, the benchmark of generated ERP-style captchas (a fast subset
here, the full run in CI's `full` job), and the migration that keeps a `delta logins captcha NAME --off` from #40 off.

Everything here is made up: no real user name, password or captcha answer."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_logins_captcha_flow import SECRET, USER, store

from delta.evals.captcha_bench import STYLES, evaluate, sample, samples, table
from delta.tools.captcha import CaptchaSolver
from delta.tools.captcha_model import Readings, decode, decode_detail, installed, shared_model
from delta.tools.captcha_ocr import Hints, hints_from

# --- decoding the model's output -----------------------------------------------------------------------------------

CHARSET = ["", "a", "A", "b", "B", "1", "7", "x", "X"]


def _probs(steps: list[dict[str, float]]) -> list[list[float]]:
    """One row per step: the given probabilities, the rest of the mass on the blank."""
    rows = []
    for st in steps:
        row = [0.0] * len(CHARSET)
        for ch, p in st.items():
            row[CHARSET.index(ch)] = p
        row[0] = max(0.0, 1.0 - sum(st.values()))
        rows.append(row)
    return rows


def test_decode_merges_repeats_and_blanks_like_ctc():
    p = _probs([{"7": 0.9}, {"7": 0.9}, {}, {"7": 0.9}, {"b": 0.5, "B": 0.45}])
    assert decode(p, CHARSET) == "77B"  # a blank splits the two 7s; capital wins b/B with the bias


def test_decode_keeps_to_the_box():
    # the box takes digits only: the model's "b" at step 2 cannot win, the 1 under it does
    p = _probs([{"7": 0.9}, {"b": 0.6, "1": 0.3}])
    assert decode(p, CHARSET, Hints(0, 2, "digits")) == "71"
    assert decode(p, CHARSET, Hints()) == "7b"  # no capital B in the model's odds at all: small
    assert decode(p, CHARSET, Hints(0, 0, "lower")) == "b"  # letters only, small: the 7 is dropped
    # a strange shape never crashes a read
    assert decode([[0.1, 0.2]], CHARSET) == ""
    assert decode([], CHARSET) == ""


def test_case_leans_to_capitals_and_flags_close_calls():
    clear_small = _probs([{"a": 0.97, "A": 0.01}])
    assert decode_detail(clear_small, CHARSET) == ("a", False)
    close = _probs([{"x": 0.6, "X": 0.1}])  # 0.1 * 15 / 0.6 = 2.5: capital wins, but it is a close call
    assert decode_detail(close, CHARSET) == ("X", True)
    assert decode_detail(close, CHARSET, Hints(0, 0, "lower")) == ("x", False)  # the box decides


# --- the order: model, Windows OCR, one vision call ------------------------------------------------------------------

class _Audit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record(self, event: str, **data: Any) -> None:
        self.rows.append({"event": event, **data})


def solver(model: Any = None, ocr: Any = None, vision_reply: str | None = None) -> tuple[CaptchaSolver, list[str]]:
    calls: list[str] = []

    async def vision(b64: str, hint: str = "") -> str:
        calls.append("vision")
        return vision_reply or ""

    async def ask(host: str, saved: str) -> bool:
        return False

    s = CaptchaSolver(allowed_sites=list, vision=vision if vision_reply is not None else None, ask_human=ask,
                      audit=_Audit(), model=model, ocr=ocr)  # type: ignore[arg-type]
    return s, calls


def model_says(*readings: str, doubt: bool = False) -> Any:
    async def read(jpeg: bytes, hints: Hints) -> Readings:
        out = Readings(readings)
        out.case_doubt = doubt
        return out

    return read


def ocr_says(*readings: str, calls: list[str] | None = None) -> Any:
    async def read(jpeg: bytes) -> list[str]:
        if calls is not None:
            calls.append("ocr")
        return list(readings)

    return read


async def test_a_sure_model_answer_needs_nothing_else():
    used: list[str] = []
    s, vis = solver(model_says("K7Q2X", "K7Q2X", "K7Q2X"), ocr_says("ZZZZZ", calls=used), vision_reply="WRONG")
    assert await s._read(b"jpeg", Hints(0, 5, "")) == "K7Q2X"
    assert s.last_via == "model" and used == [] and vis == [] and s.vision_calls == 0


async def test_windows_ocr_settles_a_split_model():
    s, vis = solver(model_says("K7Q2X", "K7O2X", "K7Q2X"), ocr_says("K7O2X", "K7O2X"), vision_reply="NOPE1")
    # OCR lands on one of the model's own passes: that one, no vision call
    assert await s._read(b"jpeg", Hints(0, 5, "")) == "K7O2X"
    assert s.last_via == "model+ocr" and vis == []


async def test_windows_ocr_settles_the_case_of_a_doubtful_model_answer():
    s, vis = solver(model_says("aB7d", "aB7d", "aB7d", doubt=True), ocr_says("ab7d", "ab7d"), vision_reply="")
    assert await s._read(b"jpeg", Hints()) == "ab7d"
    assert s.last_via == "model+ocr" and vis == []


async def test_disagreement_makes_one_small_vision_call_instead_of_a_guess():
    s, vis = solver(model_says("K7Q2X", "K7O2X", "K7Q2Y"), ocr_says("HELLO"), vision_reply="K7Q2X")
    assert await s._read(b"jpeg", Hints(0, 5, "")) == "K7Q2X"
    assert s.last_via == "vision" and vis == ["vision"] and s.vision_calls == 1


async def test_without_vision_the_model_vote_is_typed():
    s, _ = solver(model_says("K7Q2X", "K7O2X", "K7Q2X"), ocr_says("HELLO"))
    assert await s._read(b"jpeg", Hints(0, 5, "")) == "K7Q2X" and s.last_via == "model"
    # a vision call that reads nothing falls back to the same vote, counted once
    s2, vis = solver(model_says("K7Q2X", "K7O2X", "K7Q2X"), ocr_says("HELLO"), vision_reply="??")
    assert await s2._read(b"jpeg", Hints(0, 5, "")) == "K7Q2X" and vis == ["vision"] and s2.vision_calls == 1


async def test_a_reading_that_does_not_fit_the_box_is_never_sure():
    # five characters for a box that takes four: nothing fits, OCR is asked, then vision
    s, vis = solver(model_says("K7Q2X", "K7Q2X", "K7Q2X"), ocr_says("K7Q2"), vision_reply="K7Q2")
    assert await s._read(b"jpeg", Hints(4, 4, "")) == "K7Q2"
    assert vis == ["vision"]


async def test_no_model_installed_keeps_the_windows_ocr_path():
    s, vis = solver(None, ocr_says("K7Q2X", "K7Q2X"), vision_reply="WRONG")
    assert await s._read(b"jpeg", Hints(0, 5, "")) == "K7Q2X" and s.last_via == "ocr" and vis == []
    assert s.can_read()


async def test_a_failing_model_falls_through_to_ocr():
    async def broken(jpeg: bytes, hints: Hints) -> Readings:
        raise RuntimeError("onnxruntime could not start")

    s, _ = solver(broken, ocr_says("K7Q2X"))
    assert await s._read(b"jpeg", Hints(0, 5, "")) == "K7Q2X"


async def test_the_log_never_holds_a_reading(monkeypatch):
    from delta.tools import captcha as cap

    seen: list[tuple[str, dict[str, Any]]] = []

    class Log:
        def info(self, event: str, **kw: Any) -> None:
            seen.append((event, kw))

        def debug(self, event: str, **kw: Any) -> None:
            seen.append((event, kw))

    monkeypatch.setattr(cap, "log", Log())
    monkeypatch.delenv("DELTA_CAPTCHA_DEBUG", raising=False)
    s, _ = solver(model_says("K7Q2X", "K7O2X", "K7Q2X"), ocr_says("K7O2X"))
    await s._read(b"jpeg", Hints(0, 5, ""))
    assert [e for e, _ in seen] == ["captcha.read", "captcha.read"]
    assert "K7" not in repr(seen) and "O2X" not in repr(seen)
    assert seen[0][1]["engine"] == "model" and seen[0][1]["lengths"] == [5, 5, 5] and not seen[0][1]["agree"]
    seen.clear()
    monkeypatch.setenv("DELTA_CAPTCHA_DEBUG", "1")  # the user's own opt-in, for a bug report
    await s._read(b"jpeg", Hints(0, 5, ""))
    assert any(e == "captcha.read.debug" and "K7Q2X" in repr(kw) for e, kw in seen)


# --- the benchmark --------------------------------------------------------------------------------------------------

def test_the_benchmark_pictures_are_the_same_everywhere_and_fit_their_boxes():
    a, b = sample("lines_dots", 3), sample("lines_dots", 3)
    assert a.png == b.png and a.answer == b.answer
    items = samples(4)
    assert {s.style for s in items} == set(STYLES) and len(items) == 12
    for s in items:
        h = hints_from(s.attrs)
        assert h.max_len >= len(s.answer) >= 4
        if s.style == "coloured":
            assert s.answer.isdigit() and h.charset == "digits"


async def test_benchmark_scoring_with_stand_in_engines():
    items = samples(2)

    async def perfect(jpeg: bytes, hints: Hints) -> Readings:
        return Readings([next(s.answer for s in items if s.png == jpeg)] * 3)

    scores = await evaluate(items, model=perfect)
    assert scores["model"]["all"].right == 6 and scores["combined"]["all"].rate() == 1.0
    assert "| model |" in table(scores)


needs_model = pytest.mark.skipif(not installed(), reason="the captcha model (the `captcha` extra) is not installed")


@needs_model
async def test_fast_benchmark_subset_with_the_real_model():
    """10 per style. The full run (40 per style, with Windows OCR) is CI's `full` job: python -m
    delta.evals.captcha_bench. Floors here are a little under the measured rates, to catch a regression."""
    m = shared_model()
    assert m is not None
    scores = await evaluate(samples(10), model=m.read)
    print("\n" + table(scores))
    by = scores["combined"]
    assert by["lines_dots"].rate() >= 0.8
    assert by["coloured"].rate() >= 0.9
    assert by["distorted"].rate_any_case() >= 0.6
    assert scores["combined_3"]["all"].rate() >= 0.85


# --- #40's `delta logins captcha NAME --off` stays off --------------------------------------------------------------

def _forty(s: Any, lid: str, origin: str, on: bool, created: datetime, written: datetime,
           with_key: bool = True) -> None:
    """A login file as #40 (or, without the key, as the code before it) left it, last written at `written`."""
    data: dict[str, Any] = {"name": lid, "origin": origin, "username": USER, "password": SECRET, "login_url": "",
                            "permission": "ask", "created": created.replace(microsecond=0).isoformat(),
                            "last_used": "", "last_outcome": "", "own_browser": {}}
    if with_key:
        data["simple_captcha"] = on
    s.box.save(lid, data)
    t = written.timestamp()
    os.utime(s.folder / f"{lid}.bin", (t, t))


def test_a_cli_turn_off_from_40_is_read_from_the_file_and_stays_off(tmp_path):
    s = store(tmp_path)
    shipped = datetime(2026, 11, 7, 0, 53, 46, tzinfo=UTC)
    long_ago = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    # saved weeks ago, then `delta logins captcha cli_off --off` after #40 shipped: no audit line, the file says off
    _forty(s, "cli_off", "https://a.example.edu", False, long_ago, shipped + timedelta(minutes=20))
    # saved under #40 and never switched: the save's own stamp explains the write, the new default applies
    made = shipped + timedelta(minutes=30)
    _forty(s, "saved_under_40", "https://b.example.edu", False, made, made + timedelta(seconds=1))
    # written before #40 shipped (the code before it never wrote the key): the default applies
    _forty(s, "before_40", "https://c.example.edu", False, long_ago, long_ago, with_key=False)
    # the flag says off but the file was last written before the switch existed: the default applies
    _forty(s, "old_off", "https://d.example.edu", False, long_ago, shipped - timedelta(hours=2))
    # switched on in #40: on
    _forty(s, "cli_on", "https://e.example.edu", True, long_ago, shipped + timedelta(minutes=5))
    got = {x.id: (x.simple_captcha, x.captcha_choice) for x in s.list()}
    assert got == {"cli_off": (False, "off"), "saved_under_40": (True, ""), "before_40": (True, ""),
                   "old_off": (True, ""), "cli_on": (True, "on")}
    assert not s.captcha_allowed("https://a.example.edu/login")
    # written back once with the new schema: read the same again, whatever the file's time is now
    again = store(tmp_path)
    assert {x.id: (x.simple_captcha, x.captcha_choice) for x in again.list()} == got
