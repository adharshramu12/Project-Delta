"""Simple captchas read by Delta itself on every saved login with a password (owner decision 2026-11-07): the default
and its one-time migration, the OCR clean-up passes and how their readings become one answer, the order OCR -> one
small vision call -> the user, zero AI calls when OCR reads it, a user who is away never told to "complete it in the
open browser", the waiting picture shown again after a dashboard reload, and the picture sent to Telegram as a photo.

Everything here is made up: no real user name, student id, password or captcha answer."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

import pytest
from test_logins_captcha_flow import ANSWER, SECRET, SITE, USER, CaptchaBrowser, FakeExecutor, as_chat, form, store

from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.evals.portal_fixture import misread, noisy_captcha_png
from delta.logins.flow import LoginFlow
from delta.logins.service import PARK_TTL_S, LoginService
from delta.logins.store import CAPTCHA_SCHEMA
from delta.perception.ocr import OcrLine
from delta.safety.audit import AuditLog
from delta.tools.captcha import MAX_AUTO_ATTEMPTS, CaptchaSolver, stacked_reader
from delta.tools.captcha_ocr import Hints, band_readings, fit, hints_from, pick, small_crop, variants

# --- the default: on for every login with a password -------------------------------------------------------------


def test_new_logins_with_a_password_let_delta_read_the_captcha(tmp_path):
    s = store(tmp_path)
    s.add("fuerp", SITE, USER, SECRET)
    s.add("Library", "https://lib.example.edu", USER, "")  # no password: nothing to sign in with, default off
    s.add("Bank", "https://bank.example.com", USER, SECRET, simple_captcha=False)  # the user's own "no"
    assert s.get("fuerp").simple_captcha and s.get("fuerp").captcha_choice == ""
    assert s.captcha_allowed(SITE + "/index.php?r=site/login")
    assert not s.get("Library").simple_captcha
    assert not s.get("Bank").simple_captcha and s.get("Bank").captcha_choice == "off"
    # the user's choice is kept through a Replace, and a turned-off login stays off
    s.add("Bank", "https://bank.example.com", USER, "New-Made-Up-1", replace=True)
    assert not s.get("Bank").simple_captcha
    s.set_simple_captcha("fuerp", False)
    assert not s.captcha_allowed(SITE + "/")
    assert s.set_simple_captcha("fuerp", True).captcha_choice == "on"


def _legacy(s: Any, lid: str, name: str, origin: str, on: bool, password: str = SECRET) -> None:
    """A login file as #40 wrote it: only "simple_captcha", off by default then."""
    s.box.save(lid, {"name": name, "origin": origin, "username": USER, "password": password, "login_url": "",
                     "permission": "ask", "created": "", "last_used": "", "last_outcome": "", "own_browser": {},
                     "simple_captcha": on})


def test_one_time_migration_turns_it_on_unless_the_user_turned_it_off(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")  # next to logins/, where Delta keeps it
    audit.record("login.captcha_setting", login="turned_off", site="https://b.example.edu", on=True, by="dashboard")
    audit.record("login.captcha_setting", login="turned_off", site="https://b.example.edu", on=False, by="dashboard")
    audit.record("login.captcha_setting", login="back_on", site="https://c.example.edu", on=False, by="dashboard")
    audit.record("login.captcha_setting", login="back_on", site="https://c.example.edu", on=True, by="dashboard")
    s = store(tmp_path)
    _legacy(s, "never_touched", "never touched", "https://a.example.edu", False)
    _legacy(s, "turned_off", "turned off", "https://b.example.edu", False)
    _legacy(s, "back_on", "back on", "https://c.example.edu", True)
    _legacy(s, "no_password", "no password", "https://d.example.edu", False, password="")
    got = {x.id: (x.simple_captcha, x.captcha_choice) for x in s.list()}
    assert got == {"never_touched": (True, ""), "turned_off": (False, "off"), "back_on": (True, "on"),
                   "no_password": (False, "")}
    # written back once with the new schema: a fresh store (no audit log any more) reads the same
    (tmp_path / "audit.jsonl").unlink()
    again = store(tmp_path)
    assert {x.id: (x.simple_captcha, x.captcha_choice) for x in again.list()} == got
    assert again.box.load("turned_off")["captcha_schema"] == CAPTCHA_SCHEMA


# --- the OCR passes ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("attrs, want", [
    ({}, Hints()),
    ({"maxlength": "5"}, Hints(0, 5, "")),
    ({"pattern": "[0-9]{5}"}, Hints(5, 5, "digits")),
    ({"pattern": "\\d{4,6}"}, Hints(4, 6, "digits")),
    ({"pattern": "[A-Z0-9]{6}", "maxlength": "6"}, Hints(6, 6, "upper_digits")),
    ({"pattern": "[A-Za-z]+"}, Hints(0, 0, "letters")),
    ({"inputmode": "numeric", "maxlength": "4"}, Hints(0, 4, "digits")),
    ({"pattern": ".{3,8}"}, Hints(3, 8, "")),  # anything: no character rule
    ({"maxlength": "999", "pattern": "((("}, Hints()),  # nonsense never blocks a read
])
def test_hints_from_the_box(attrs, want):
    assert hints_from(attrs) == want


def test_fit_fixes_the_usual_confusions_only_when_the_box_says_which_kind():
    digits = Hints(5, 5, "digits")
    assert fit("4O lSZ-", digits) == "40152"
    assert fit("4O lSZ-") == "4OlSZ"  # an open box: nothing is guessed
    assert fit("K7 Q2X.", Hints(0, 5, "")) == "K7Q2X"
    assert fit("K7Q2X9", Hints(0, 5, "")) is None  # more than the box takes: a misread, never trimmed
    assert fit("A8", Hints()) is None and fit("", Hints()) is None
    assert fit("q0o1", Hints(4, 4, "upper")) == "QOOI"


def test_the_passes_vote_per_character():
    assert pick(["K7Q2X", "K7O2X", "K7Q2X", "", "KTQ2X"], Hints(0, 5, "")) == "K7Q2X"
    assert pick(["4O1S2", "40152", "4015Z"], Hints(5, 5, "digits")) == "40152"
    assert pick(["K7Q2", "K7Q2X", "K7Q2X"], Hints(5, 5, "")) == "K7Q2X"  # the box's own length wins
    assert pick(["", "~~", "noise words here"], Hints()) is None


def _ink(img: Any, mask: Any) -> float:
    """Share of the mask's pixels that are dark in img (both the same size, L)."""
    px, m = img.load(), mask.load()
    on = [(x, y) for x in range(mask.width) for y in range(mask.height) if m[x, y]]
    return sum(1 for x, y in on if px[x, y] < 128) / max(1, len(on))


def test_line_removal_keeps_the_letters_and_drops_the_lines():
    from PIL import Image, ImageDraw, ImageFont

    w, h = 150, 50
    clean = Image.new("L", (w, h), 0)
    lines = Image.new("L", (w, h), 0)
    pic = Image.new("RGB", (w, h), (245, 245, 240))
    try:
        font: Any = ImageFont.load_default(size=30)
    except TypeError:
        font = ImageFont.load_default()
    for i, ch in enumerate("K7Q2X"):
        ImageDraw.Draw(pic).text((10 + i * 26, 8), ch, fill=(30, 30, 80), font=font)
        ImageDraw.Draw(clean).text((10 + i * 26, 8), ch, fill=255, font=font)
    for seg in ((0, 30, 150, 22), (5, 5, 140, 45), (0, 45, 150, 10)):
        ImageDraw.Draw(pic).line(seg, fill=(90, 90, 90), width=1)
        ImageDraw.Draw(lines).line(seg, fill=255, width=1)
    lines.paste(0, mask=clean)  # only line pixels away from the letters
    bands = variants(pic)
    assert len(bands) == 5 and all(b.height == 120 and b.mode == "L" for b in bands)
    big = (bands[0].width, 120)
    clean_big, lines_big = clean.resize(big), lines.resize(big)
    threshold, no_lines = bands[1], bands[3]
    assert _ink(threshold, lines_big) > 0.5  # Otsu keeps the lines
    assert _ink(no_lines, lines_big) < 0.25 * _ink(threshold, lines_big)  # the opening removes most of them
    assert _ink(no_lines, clean_big) > 0.6  # and keeps the letters


def test_inverted_captchas_are_turned_dark_on_light():
    from PIL import Image, ImageDraw

    pic = Image.new("RGB", (120, 40), (20, 20, 20))
    ImageDraw.Draw(pic).text((20, 12), "AB12", fill=(240, 240, 240))
    gray = variants(pic)[0]
    hist = gray.histogram()
    assert sum(i * n for i, n in enumerate(hist)) / sum(hist) > 128  # mostly light now


class FakeEngine:
    """Stands in for WindowsOcr.read: answers one OCR line per band of the stacked sheet."""

    def __init__(self, per_band: list[str]) -> None:
        self.per_band, self.sheets = per_band, []

    async def read(self, sheet: Any) -> list[OcrLine]:
        from delta.tools.captcha_ocr import BAND_HEIGHT, GAP, MARGIN

        self.sheets.append(sheet.size)
        out = []
        for i, text in enumerate(self.per_band):
            top = MARGIN + i * (BAND_HEIGHT + GAP)
            if text:
                words = tuple((t, (30 + 60 * j, top + 20, 50, 80)) for j, t in enumerate(text.split()))
                out.append(OcrLine(text, (30, top + 20, 200, 80), words))
        return out


async def test_one_ocr_call_reads_all_five_passes():
    engine = FakeEngine(["K7 Q2X", "K7O2X", "K7Q2X", "K7Q2X", ""])
    readings = await stacked_reader(engine)(noisy_captcha_png("K7Q2X", 5))
    assert readings == ["K7Q2X", "K7O2X", "K7Q2X", "K7Q2X"] and len(engine.sheets) == 1  # one PowerShell start
    assert pick(readings, Hints(0, 5, "")) == "K7Q2X"


def test_band_readings_order_words_left_to_right():
    spans = [(24, 144), (204, 324)]
    lines = [OcrLine("Q2 K7", (0, 230, 100, 60), (("Q2", (90, 230, 40, 60)), ("K7", (10, 230, 40, 60)))),
             OcrLine("AB", (0, 40, 100, 60), (("AB", (10, 40, 40, 60)),))]
    assert band_readings(lines, spans) == ["AB", "K7Q2"]


def test_the_picture_for_a_model_is_small():
    big = noisy_captcha_png("K7Q2X", 9, size=(600, 200))
    small = small_crop(big)
    from PIL import Image

    im = Image.open(io.BytesIO(small))
    assert im.format == "JPEG" and im.width <= 256 and im.height <= 72 and len(small) < len(big)
    assert small_crop(b"not a picture") == b"not a picture"


# --- the order: OCR, then one small vision call, then the user ------------------------------------------------------

class Page:
    def __init__(self, hints: dict[str, Any] | None = None) -> None:
        self.url, self.value, self.refreshes, self.hints = SITE + "/login", "", 0, hints or {}

    async def challenge_markers(self) -> list[str]:
        return []

    async def screenshot(self, image: str) -> bytes | None:
        return noisy_captcha_png("K7Q2X", self.refreshes)

    async def has_field(self, field: str) -> bool:
        return True

    async def is_secret_field(self, field: str) -> bool:
        return False

    async def fill(self, field: str, value: str) -> None:
        self.value = value

    async def field_value(self, field: str) -> str:
        return self.value

    async def refresh(self, image: str) -> bool:
        self.refreshes += 1
        return True

    async def field_hints(self, field: str) -> dict[str, Any]:
        return self.hints


def make(tmp_path: Path, ocr_reads: list[Any] | None, vision_reply: str = "", sees: bool = True,
         ) -> tuple[CaptchaSolver, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []
    queue = list(ocr_reads or [])

    async def ocr(jpeg: bytes) -> Any:
        return queue.pop(0) if queue else ""

    async def vision(b64: str, hint: str = "") -> str:
        calls.append((b64, hint))
        return vision_reply

    vision.takes_hint = True  # type: ignore[attr-defined]

    async def nobody(host: str, saved: str) -> bool:
        raise AssertionError("never the on-PC hand-off here")

    s = CaptchaSolver(allowed_sites=lambda: [], vision=vision, ask_human=nobody,
                      audit=AuditLog(tmp_path / "c.jsonl"), ocr=ocr if ocr_reads is not None else None,
                      origin_allowed=lambda url: url.startswith(SITE), vision_ready=lambda: sees)
    return s, calls


async def test_ocr_reads_it_and_no_model_is_asked(tmp_path):
    s, calls = make(tmp_path, [["K7 Q2X-", "K7Q2X", "K7O2X"]])
    page = Page({"maxlength": "5"})
    ok, note, _shot = await s.auto_fill(page)
    assert ok and page.value == "K7Q2X" and calls == [] and s.vision_calls == 0 and "on this PC" in note
    assert '"via": "ocr"' in (tmp_path / "c.jsonl").read_text() and "K7Q2X" not in (tmp_path / "c.jsonl").read_text()


async def test_digits_box_fixes_ocr_mixups(tmp_path):
    s, _ = make(tmp_path, [[misread("40152")]])
    page = Page({"pattern": "[0-9]{5}", "inputmode": "numeric"})
    assert (await s.auto_fill(page))[0] and page.value == "40152"


async def test_vision_only_after_ocr_fails_one_small_call_per_try(tmp_path):
    s, calls = make(tmp_path, [[""], ["~~"]], vision_reply="Sorry, I cannot read that")
    page = Page({"maxlength": "5"})
    ok, _note, shot = await s.auto_fill(page)
    assert not ok and shot is not None and page.value == ""
    assert len(calls) == MAX_AUTO_ATTEMPTS == s.vision_calls and page.refreshes == MAX_AUTO_ATTEMPTS - 1
    for b64, hint in calls:
        assert len(base64.b64decode(b64)) < 6000 and hint == "at most 5 characters"
    s2, calls2 = make(tmp_path / "2", [[""]], vision_reply="K7Q2X")
    ok2, note2, _ = await s2.auto_fill(Page())
    assert ok2 and len(calls2) == 1 and "AI model" in note2


async def test_no_vision_call_when_no_model_takes_pictures(tmp_path):
    s, calls = make(tmp_path, [[""]], vision_reply="K7Q2X", sees=False)
    ok, _, _ = await s.auto_fill(Page())
    assert not ok and calls == [] and s.vision_calls == 0
    s2, _ = make(tmp_path / "2", None, sees=False)  # no OCR on this PC and no model that sees: the user, at once
    ok2, note2, shot2 = await s2.auto_fill(Page())
    assert not ok2 and "no way to read it" in note2 and shot2 is not None


async def test_managed_challenges_are_never_read(tmp_path):
    s, calls = make(tmp_path, [["K7Q2X"]], vision_reply="K7Q2X")
    page = Page()

    async def markers() -> list[str]:
        return ["https://challenges.cloudflare.com/turnstile/v0/api.js"]

    page.challenge_markers = markers  # type: ignore[method-assign]
    ok, note, _ = await s.auto_fill(page)
    assert not ok and "never tries" in note and page.value == "" and calls == []


# --- a whole sign-in: default on, zero AI calls, a wrong password never re-sent ------------------------------------

def _service(tmp_path: Path, reads: list[Any], password: str = SECRET) -> LoginService:
    s = store(tmp_path)
    s.add("fuerp", SITE, USER, password, permission="always")  # no captcha choice: the default (on)
    s.allow_own_browser("fuerp", "chrome")
    solver, _ = make(tmp_path, reads)
    solver.origin_allowed = s.captcha_allowed
    return LoginService(s, AuditLog(tmp_path / "audit.jsonl"), None, captcha_solver=solver)


async def test_default_on_signs_in_with_no_approval(tmp_path):
    svc = _service(tmp_path, [[ANSWER, ANSWER]])
    b = CaptchaBrowser(form())
    token = as_chat()
    try:
        r = await svc.sign_in_own("fuerp", b)
    finally:
        CURRENT_REQUEST.reset(token)
    assert r.ok, r.message
    assert b.posts == [{"user": USER, "password": "<set>", "captcha": ANSWER}]
    assert svc.solver.vision_calls == 0


async def test_a_rejected_password_is_never_sent_again(tmp_path):
    svc = _service(tmp_path, [[ANSWER]] * 5, password="Wrong-Made-Up-9")
    b = CaptchaBrowser(form())
    r = await svc.sign_in_own("fuerp", b)
    assert r.outcome == "wrong_password" and len(b.posts) == 1


async def test_orchestrated_sign_in_makes_zero_ai_calls_when_ocr_reads_it(tmp_path):
    from fakes import FakeProvider

    from delta.config import AgentConfig, Boundaries, MemoryConfig, Settings
    from delta.core.events import EventBus
    from delta.core.orchestrator import Orchestrator
    from delta.llm.router import ModelRouter
    from delta.safety.policy import PolicyEngine
    from delta.tools.registry import ToolExecutor, ToolRegistry

    def no_model(task: str, messages: Any) -> Any:
        raise AssertionError(f"no AI call expected ({task})")

    settings = Settings(agent=AgentConfig(max_attempts=1, max_steps=3), memory=MemoryConfig(auto_extract=False),
                        data_dir=tmp_path)
    router = ModelRouter({"fake": FakeProvider("fake", handler=no_model)},
                         {"plan": ["fake"], "chat": ["fake"], "vision": ["fake"]})
    audit = AuditLog(tmp_path / "orch.jsonl")
    orch = Orchestrator(settings, router, ToolExecutor(ToolRegistry([]), PolicyEngine(Boundaries()), None, audit),
                        None, None, EventBus(), audit)
    svc = _service(tmp_path, [[ANSWER]])
    orch.login_flow = LoginFlow(svc, FakeExecutor(svc, CaptchaBrowser(form())), orch.events)
    done = await orch.run(TaskRequest("Login my fixture university erp portal", channel="telegram", user="1",
                                      chat_id="t1"))
    assert done.status == "done" and done.tokens.get("calls", 0) == 0 and svc.solver.vision_calls == 0


# --- the user away from the PC: a picture, never "complete it in the open browser" ---------------------------------

class NoPictureBrowser(CaptchaBrowser):
    async def captcha_image(self, f: Any) -> bytes | None:  # type: ignore[override]
        return None


class BandBrowser(NoPictureBrowser):
    async def captcha_region_image(self, f: Any) -> bytes | None:
        return b"\xff\xd8band-around-the-box"


async def test_remote_user_is_never_told_to_use_the_open_browser(tmp_path):
    s = store(tmp_path)
    s.add("fuerp", SITE, USER, SECRET, permission="always", simple_captcha=False)
    s.allow_own_browser("fuerp", "chrome")
    handed: list[str] = []
    svc = LoginService(s, AuditLog(tmp_path / "audit.jsonl"), None)

    async def handoff(kind: str, host: str, text: str) -> bool:
        handed.append(text)
        return True

    svc.handoff = handoff  # type: ignore[method-assign]
    token = CURRENT_REQUEST.set(TaskRequest("sign in", channel="telegram", user="1", chat_id="t1"))
    try:
        r = await svc.sign_in_own("fuerp", NoPictureBrowser(form()))
        assert r.outcome == "needs_user" and handed == [] and "open browser" not in r.message
        assert "press Sign in" not in r.message and "nothing was submitted" in r.message.lower()
        band = BandBrowser(form())
        r2 = await svc.sign_in_own("fuerp", band)  # no picture element: a band of the page instead
        assert r2.captcha_image == b"\xff\xd8band-around-the-box" and svc.waiting("t1") is not None
        assert handed == [] and band.posts == []
    finally:
        CURRENT_REQUEST.reset(token)


async def test_at_the_pc_the_old_handoff_still_works_without_any_picture(tmp_path):
    s = store(tmp_path)
    s.add("fuerp", SITE, USER, SECRET, permission="always", simple_captcha=False)
    s.allow_own_browser("fuerp", "chrome")
    handed: list[str] = []
    svc = LoginService(s, AuditLog(tmp_path / "audit.jsonl"), None)

    async def handoff(kind: str, host: str, text: str) -> bool:
        handed.append(kind)
        return False

    svc.handoff = handoff  # type: ignore[method-assign]
    token = as_chat()
    try:
        await svc.sign_in_own("fuerp", NoPictureBrowser(form()))
    finally:
        CURRENT_REQUEST.reset(token)
    assert handed == ["captcha"]


# --- the fallback: the picture to the chat, kept across a dashboard reload, sent to Telegram as a photo ------------

class Proof:
    def __init__(self, folder: Path) -> None:
        self.dir = folder
        folder.mkdir(parents=True, exist_ok=True)

    def save_image(self, b64: str, label: str) -> str:
        (self.dir / "01-captcha.jpg").write_bytes(base64.b64decode(b64))
        return "01-captcha.jpg"


class Phone:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Path, str]] = []

    async def send_file(self, user: str, path: Path, caption: str = "") -> None:
        self.sent.append((user, path, caption))


async def test_telegram_gets_the_photo_and_the_reply_finishes_the_same_sign_in(tmp_path):
    import asyncio

    s = store(tmp_path)
    s.add("fuerp", SITE, USER, SECRET, permission="always")
    s.allow_own_browser("fuerp", "chrome")
    phone = Phone()
    solver, _ = make(tmp_path, [[""], [""], [""]], sees=False)  # OCR reads nothing three times
    solver.origin_allowed = s.captcha_allowed
    svc = LoginService(s, AuditLog(tmp_path / "audit.jsonl"), None, captcha_solver=solver,
                       proof=Proof(tmp_path / "run"), channels={"telegram": phone})
    b = CaptchaBrowser(form())
    flow = LoginFlow(svc, FakeExecutor(svc, b))

    async def say(text: str) -> Any:
        req = TaskRequest(text, channel="telegram", user="42", chat_id="t1")
        token = CURRENT_REQUEST.set(req)
        try:
            return await flow.handle(req)
        finally:
            CURRENT_REQUEST.reset(token)

    asked = await say("sign in to fuerp")
    await asyncio.sleep(0)
    assert asked.status == "needs_user" and b.posts == []
    assert phone.sent and phone.sent[0][0] == "42" and phone.sent[0][1].suffix == ".jpg"
    assert "reply with just these characters" in phone.sent[0][2]
    view = svc.waiting_view("t1")  # what the dashboard shows again after a reload
    assert view is not None and view["login"] == "fuerp" and base64.b64decode(view["image"])
    assert 0 < view["expires_in_s"] <= PARK_TTL_S
    done = await say(ANSWER)
    assert done.status == "done" and b.posts[-1]["captcha"] == ANSWER and svc.waiting_view("t1") is None
    assert ANSWER not in (tmp_path / "audit.jsonl").read_text()


async def test_a_reload_shows_the_waiting_picture(tmp_path):
    s = store(tmp_path)
    s.add("fuerp", SITE, USER, SECRET, permission="always", simple_captcha=False)
    s.allow_own_browser("fuerp", "chrome")
    svc = LoginService(s, AuditLog(tmp_path / "audit.jsonl"), None)
    token = as_chat("dash1")
    try:
        await svc.sign_in_own("fuerp", CaptchaBrowser(form()))
    finally:
        CURRENT_REQUEST.reset(token)
    assert svc.waiting_view("dash1") is not None and svc.waiting_view("other") is None
    svc.clock = lambda: 1e12
    assert svc.waiting_view("dash1") is None  # expired after 10 minutes


def test_chat_route_carries_the_picture():
    js = (Path(__file__).parents[1] / "src/delta/api/static/chat.js").read_text(encoding="utf-8")
    server = (Path(__file__).parents[1] / "src/delta/api/server.py").read_text(encoding="utf-8")
    assert "waiting_view(chat_id)" in server and '"captcha": captcha' in server
    assert "data.captcha" in js and "captcha-pic" in js


# --- the CLI and the settings copy -----------------------------------------------------------------------------------

def test_cli_help_and_list_say_on_by_default(tmp_path):
    import argparse

    from delta.config import Settings
    from delta.logins.cli import add_parser, run_cli

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    add_parser(sub)
    helptext = sub.choices["logins"].format_help()
    assert "the default for a login with a password" in " ".join(helptext.split())
    s = store(tmp_path)
    s.add("fuerp", SITE, USER, SECRET)
    out: list[str] = []
    run_cli(Settings(data_dir=tmp_path), parser.parse_args(["logins", "list"]), out=out.append, store=s)
    assert "Delta reads its simple captcha (default)" in out[0] and SECRET not in out[0]
    out.clear()
    run_cli(Settings(data_dir=tmp_path), parser.parse_args(["logins", "captcha", "fuerp", "--off"]), out=out.append,
            store=s)
    assert "sends you the captcha picture" in out[0] and s.get("fuerp").captcha_choice == "off"


def test_settings_copy_says_on_by_default():
    html = (Path(__file__).parents[1] / "src/delta/api/static/index.html").read_text(encoding="utf-8")
    assert 'id="lg_captcha" checked' in html and "On by default" in html
    assert json.dumps("Off by default") not in html
