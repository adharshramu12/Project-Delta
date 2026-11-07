"""Simple image captchas on the user's OWN accounts (e.g. a university portal), read on this PC.

How it decides, for the site open in the browser:

1. reCAPTCHA, hCaptcha, Cloudflare Turnstile and other managed challenges are refused outright: they exist to tell
   people from programs, so Delta never tries to get past them and hands the page back to the user.
2. A saved login for exactly this origin with a password has "This site has a simple text captcha, let Delta read it"
   on (Settings > Logins; ON by default for every login with a password since 2026-11, the user can turn it off per
   login; delta.logins.store), or the site's host is in `captcha.allowed_sites` (`delta captcha allow <host>`, set by
   the user, never by Delta: its settings file is a protected path): the captcha picture is read automatically, with
   no approval:
     a. the captcha model on this PC (delta.tools.captcha_model: ddddocr, a small offline ONNX model made for simple
        text captchas, MIT), on three versions of the picture, held to the box's own maxlength / pattern / inputmode;
        when all three readings agree the answer is typed and nothing else runs;
     b. otherwise Windows OCR on five cleaned-up versions of the picture at once (delta.tools.captcha_ocr), the
        usual 0/O, 1/l/I, 5/S mix-ups fixed when the box says digits or letters; when it lands on what the model
        read, that is typed (Windows OCR is the only engine when the model is not installed);
     c. when the engines disagree, a reading does not fit the box or nothing was read, and a configured model takes
        pictures and the user lets pictures go to a model (attachments.vision is not "off"): ONE vision call on a
        small crop of the captcha (a few hundred bytes, low detail) instead of typing a likely wrong guess. Counted
        (CaptchaSolver.vision_calls, the audit's via=vision). Without one, the model's best reading is typed.
   At most MAX_AUTO_ATTEMPTS (3) tries per site in ATTEMPT_WINDOW_S; from the second try on the captcha is refreshed
   first (a new picture). A sign-in where the model or OCR is sure makes zero AI calls.
3. Otherwise, or once those tries are used up: the user. For a sign-in, the captcha picture goes to the chat the
   request came from (delta.logins.service parks the filled form); for the solve_captcha tool, Delta saves the
   picture with the run's proof and pauses on an approval. The answer is never logged, audited or shown to a model.

Every attempt is written to the audit log (site, mode, how it was read, success). The answer text never is.
"""

from __future__ import annotations

import base64
import contextlib
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from delta.config import read_user_settings
from delta.safety.approvals import ApprovalBroker, ApprovalRequest
from delta.safety.audit import AuditLog
from delta.system.logging import log
from delta.tools.base import Risk, ToolResult
from delta.tools.browser import BrowserManager, _BrowserTool
from delta.tools.captcha_ocr import Hints, fit, hints_from, pick, small_crop

MAX_AUTO_ATTEMPTS = 3
ATTEMPT_WINDOW_S = 15 * 60  # auto attempts per site are counted over this window, then reset
ANSWER = re.compile(r"^[A-Za-z0-9]{3,10}$")  # what a simple text captcha looks like

# Challenges built to stop automation. Matched against frame/script URLs and element classes.
CHALLENGES: dict[str, tuple[str, ...]] = {
    "reCAPTCHA": ("google.com/recaptcha", "gstatic.com/recaptcha", "recaptcha.net", "g-recaptcha"),
    "hCaptcha": ("hcaptcha.com", "h-captcha"),
    "Cloudflare Turnstile": ("challenges.cloudflare.com", "cf-turnstile", "turnstile"),
}

CHALLENGE_JS = """() => {
  const out = [];
  for (const f of document.querySelectorAll('iframe')) out.push(f.src || '');
  for (const s of document.querySelectorAll('script[src]')) out.push(s.src);
  for (const e of document.querySelectorAll('.g-recaptcha,.h-captcha,.cf-turnstile,[data-sitekey]'))
    out.push(e.className || 'data-sitekey');
  return out;
}"""

AUTO_IMAGE = ("img[id*=captcha i], img[class*=captcha i], img[src*=captcha i], img[alt*=captcha i], "
              "canvas[id*=captcha i], canvas[class*=captcha i], img[id*=verifycode i], img[src*=verifycode i], "
              "canvas[id*=verifycode i]")
# A "new picture" control next to the captcha. Clicking the picture itself refreshes many simple captchas too.
REFRESH = ("[id*=captcha i][id*=refresh i], [class*=captcha i][class*=refresh i], [id*=refresh i], "
           "[class*=refresh i], [title*=refresh i], [aria-label*=refresh i], [alt*=refresh i]")
AUTO_FIELD = ("input[name*=captcha i], input[id*=captcha i], input[placeholder*=captcha i], "
              "input[aria-label*=captcha i], input[name*=verifycode i], input[id*=verifycode i]")
# The picture next to a captcha box when nothing on the page says "captcha" (a bare <canvas>, an <img> with a random
# id): the visible img or canvas of a sensible size closest to the box, in the box's own form first.
NEAREST_IMAGE_JS = """(box) => {
  const r = box.getBoundingClientRect();
  const scope = box.form || document;
  let best = null, bestD = 1e9;
  for (const el of [...scope.querySelectorAll('img, canvas'), ...document.querySelectorAll('img, canvas')]) {
    const b = el.getBoundingClientRect();
    if (b.width < 30 || b.height < 12 || b.width > 600 || b.height > 250) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    const d = Math.abs((b.top + b.bottom) / 2 - (r.top + r.bottom) / 2) + Math.abs(b.left - r.left) / 4
      + (scope.contains(el) ? 0 : 200);
    if (d < bestD && d < 400) { best = el; bestD = d; }
  }
  if (best) best.setAttribute('data-delta-captcha', '1');
  return !!best;
}"""
HINTS_JS = """(el) => ({maxlength: el.getAttribute('maxlength'), minlength: el.getAttribute('minlength'),
  pattern: el.getAttribute('pattern'), inputmode: el.getAttribute('inputmode'), type: el.getAttribute('type')})"""


# ---------------------------------------------------------------- pure rules (unit tested)

def normalize_site(entry: str) -> str:
    """'https://ERP.Example.in:443/login' -> 'erp.example.in'; '*.example.in' keeps its wildcard."""
    raw = entry.strip().lower()
    wildcard = raw.startswith("*.")
    if wildcard:
        raw = raw[2:]
    host = (urlparse(raw if "://" in raw else f"https://{raw}").hostname or "").rstrip(".")
    if not host or "*" in host or "." not in host:
        return ""
    return f"*.{host}" if wildcard else host


def host_of(url: str) -> str:
    if not url.lower().startswith(("http://", "https://")):
        return ""
    return (urlparse(url).hostname or "").lower().rstrip(".")


def site_allowed(host: str, allowed: list[str]) -> bool:
    """Exact host match only. A '*.example.in' entry also covers its subdomains (not example.in
    itself). Plain string tricks such as 'erp.example.in.evil.com' or 'evil-erp.example.in' never match."""
    host = host.lower().rstrip(".")
    if not host:
        return False
    for entry in filter(None, (normalize_site(e) for e in allowed)):
        if entry.startswith("*."):
            if host.endswith("." + entry[2:]):
                return True
        elif host == entry:
            return True
    return False


def challenge_kind(markers: list[str]) -> str | None:
    """Name of an anti-bot challenge present on the page, or None for a plain captcha."""
    text = " ".join(markers).lower()
    for name, needles in CHALLENGES.items():
        if any(n in text for n in needles):
            return name
    return None


def first_line(reply: str) -> str:
    return re.sub(r"[\s`'\"]", "", reply.strip().splitlines()[0] if reply and reply.strip() else "")


def clean_answer(reply: str) -> str | None:
    """The characters a vision model read, or None when its reply does not look like a captcha answer."""
    token = first_line(reply)
    return token if ANSWER.match(token) else None


def _log_read(engine: str, readings: list[str], fitted: list[str | None], answer: str | None,
              case_doubt: bool = False) -> None:
    """Per-engine, per-pass log line: how many passes read something that fits the box, their lengths and whether they
    agree. Never the readings or the answer, unless the user opts in with DELTA_CAPTCHA_DEBUG=1 (for a bug report)."""
    import os

    with contextlib.suppress(Exception):
        log.info("captcha.read", engine=engine, passes=len(readings), fits=sum(1 for f in fitted if f),
                 lengths=[len(f) if f else 0 for f in fitted], agree=bool(answer) and all(f == answer for f in fitted),
                 answered=bool(answer), case_doubt=case_doubt)
        if os.environ.get("DELTA_CAPTCHA_DEBUG") == "1":
            log.debug("captcha.read.debug", engine=engine, readings=readings, answer=answer)


# ---------------------------------------------------------------- the page, behind a small interface

class CaptchaPage(Protocol):
    url: str

    async def challenge_markers(self) -> list[str]: ...
    async def screenshot(self, image: str) -> bytes | None: ...  # None: no such element
    async def has_field(self, field: str) -> bool: ...
    async def is_secret_field(self, field: str) -> bool: ...
    async def fill(self, field: str, value: str) -> None: ...
    async def field_value(self, field: str) -> str: ...
    async def refresh(self, image: str) -> bool: ...  # a new captcha picture; False when there is no way to


class PlaywrightCaptchaPage:
    """CaptchaPage over a Playwright page. `image`/`field` are CSS selectors or visible labels;
    empty means 'find the captcha image / input on the page'."""

    def __init__(self, page: Any) -> None:
        self.page = page
        self.url: str = page.url

    async def challenge_markers(self) -> list[str]:
        markers = [f.url for f in self.page.frames]
        with contextlib.suppress(Exception):
            markers += await self.page.evaluate(CHALLENGE_JS)
        return markers

    async def _first(self, locator: Any) -> Any | None:
        with contextlib.suppress(Exception):
            for i in range(min(await locator.count(), 10)):
                if await locator.nth(i).is_visible():
                    return locator.nth(i)
        return None

    async def _image(self, image: str) -> Any | None:
        if not image:
            if found := await self._first(self.page.locator(AUTO_IMAGE)):
                return found
            box = await self._field("")
            with contextlib.suppress(Exception):
                if box is not None and await box.evaluate(NEAREST_IMAGE_JS):
                    return await self._first(self.page.locator("[data-delta-captcha]"))
            return None
        with contextlib.suppress(Exception):
            if found := await self._first(self.page.locator(image)):
                return found
        return await self._first(self.page.get_by_alt_text(image).or_(self.page.get_by_role("img", name=image)))

    async def _field(self, field: str) -> Any | None:
        if not field:
            return await self._first(self.page.locator(AUTO_FIELD))
        with contextlib.suppress(Exception):
            if found := await self._first(self.page.locator(field)):
                return found
        return await self._first(self.page.get_by_label(field).or_(self.page.get_by_placeholder(field)))

    async def screenshot(self, image: str) -> bytes | None:
        """A JPEG of the captcha picture only (an element shot: right at any display scaling, never the password
        box). A canvas-drawn captcha is captured the same way."""
        el = await self._image(image)
        return None if el is None else bytes(await el.screenshot(type="jpeg", quality=90, timeout=8000))

    async def region_shot(self) -> bytes | None:
        """For the USER only (never OCR or a model): the page around the captcha box, password boxes masked, when no
        picture element can be found next to it."""
        el = await self._field("")
        if el is None:
            return None
        with contextlib.suppress(Exception):
            from delta.logins.signin import mask_secret_fields

            await mask_secret_fields(self.page)
            b = await el.bounding_box()
            if not b:
                return None
            vp = self.page.viewport_size or {"width": 1280, "height": 800}
            h = max(20.0, b["height"])
            x = max(0.0, b["x"] - 360)
            y = max(0.0, b["y"] - 2.5 * h)
            w = min(float(vp["width"]) - x, b["width"] + 720)
            hh = min(float(vp["height"]) - y, 5 * h)
            if w <= 0 or hh <= 0:
                return None
            return bytes(await self.page.screenshot(type="jpeg", quality=85, timeout=8000,
                                                    clip={"x": x, "y": y, "width": w, "height": hh}))
        return None

    async def field_hints(self, field: str) -> dict[str, Any]:
        """The captcha box's maxlength, minlength, pattern, inputmode and type (what an answer must fit)."""
        el = await self._field(field)
        if el is None:
            return {}
        with contextlib.suppress(Exception):
            got = await el.evaluate(HINTS_JS)
            return dict(got) if isinstance(got, dict) else {}
        return {}

    async def has_field(self, field: str) -> bool:
        return await self._field(field) is not None

    async def is_secret_field(self, field: str) -> bool:
        el = await self._field(field)
        if el is None:
            return False
        kind = ((await el.get_attribute("type")) or "").lower()
        auto = ((await el.get_attribute("autocomplete")) or "").lower()
        return kind == "password" or "password" in auto or "cc-" in auto

    async def fill(self, field: str, value: str) -> None:
        el = await self._field(field)
        if el is None:
            raise LookupError("captcha input not found")
        await el.fill(value, timeout=8000)

    async def field_value(self, field: str) -> str:
        el = await self._field(field)
        return "" if el is None else str(await el.input_value())

    async def refresh(self, image: str) -> bool:
        target = await self._first(self.page.locator(REFRESH)) or await self._image(image)
        if target is None:
            return False
        with contextlib.suppress(Exception):
            await target.click(timeout=4000)
            await self.page.wait_for_timeout(700)  # the new picture loads
            return True
        return False


# ---------------------------------------------------------------- the decision logic

Vision = Callable[..., Awaitable[str]]  # (base64 jpeg[, hint=]) -> model reply
# jpeg bytes -> what was read on this PC (Windows OCR): one text, or one per cleaned-up pass (captcha_ocr.variants)
Reader = Callable[[bytes], Awaitable["str | list[str]"]]
# (jpeg bytes, what the box takes) -> one reading per pass of the captcha model (delta.tools.captcha_model)
ModelReader = Callable[[bytes, Hints], Awaitable[list[str]]]
AskHuman = Callable[[str, str], Awaitable[bool]]  # (host, saved image name) -> user says they typed it


@dataclass
class CaptchaSolver:
    allowed_sites: Callable[[], list[str]]
    vision: Vision | None
    ask_human: AskHuman
    audit: AuditLog
    save_image: Callable[[str, str], str | None] = lambda b64, label: None
    clock: Callable[[], float] = time.monotonic
    ocr: Reader | None = None  # local OCR, tried first
    # url -> a saved login for exactly that origin lets Delta read its simple captcha (LoginStore.captcha_allowed)
    origin_allowed: Callable[[str], bool] | None = None
    # Whether a configured model takes pictures right now (asked at each read; None = whenever `vision` is set)
    vision_ready: Callable[[], bool] | None = None
    # The captcha model on this PC (delta.tools.captcha_model), tried before Windows OCR; None when not installed
    model: ModelReader | None = None

    def __post_init__(self) -> None:
        self._attempts: dict[str, tuple[int, float]] = {}  # host -> (auto attempts, first attempt time)
        self.vision_calls = 0  # pictures sent to a model (each one small); OCR reads are free
        self.last_via = ""  # how the last answer was read: model | model+ocr | ocr | vision | ""

    def _used(self, host: str) -> int:
        count, since = self._attempts.get(host, (0, 0.0))
        return count if self.clock() - since < ATTEMPT_WINDOW_S else 0

    def _count(self, host: str) -> int:
        used = self._used(host)
        since = self._attempts[host][1] if used else self.clock()
        self._attempts[host] = (used + 1, since)
        return used + 1

    def allowed(self, url: str) -> bool:
        """The user lets Delta read simple captchas on this page: its host is on the allowlist, or a saved login for
        exactly its origin has the captcha switch on."""
        if site_allowed(host_of(url), self.allowed_sites()):
            return True
        with contextlib.suppress(Exception):
            return bool(self.origin_allowed is not None and self.origin_allowed(url))
        return False

    def _can_see(self) -> bool:
        if self.vision is None:
            return False
        if self.vision_ready is None:
            return True
        with contextlib.suppress(Exception):
            return bool(self.vision_ready())
        return False

    def can_read(self) -> bool:
        return self.model is not None or self.ocr is not None or self._can_see()

    async def _engine(self, name: str, reader: Any, shot: bytes, hints: Hints) -> tuple[str | None, list[str], bool]:
        """One engine's readings: (the answer they vote for or None, the readings that fit the box, sure = at least two
        passes and every one of them fits and reads the same, and no letter's case was a close call). Logged as lengths
        and agreement only; the readings themselves only with DELTA_CAPTCHA_DEBUG=1."""
        readings: list[str] = []
        doubt = False
        with contextlib.suppress(Exception):  # an engine missing or failing: the next one still reads
            got = await (reader(shot, hints) if name == "model" else reader(shot))
            readings = [str(r or "") for r in got] if isinstance(got, list) else [str(got or "")]
            doubt = bool(getattr(got, "case_doubt", False))
        answer = pick(readings, hints) if readings else None
        fitted = [fit(r, hints) for r in readings]
        _log_read(name, readings, fitted, answer, doubt)
        sure = bool(answer) and not doubt and len(fitted) >= 2 and all(f == answer for f in fitted)
        return answer, [f for f in fitted if f], sure

    async def _read(self, shot: bytes, hints: Hints | None = None) -> str | None:
        """The answer for one picture, read on this PC whenever the engines are sure:

        1. the captcha model (delta.tools.captcha_model) on three passes: all three agree and no letter's case was a
           close call -> taken, nothing else runs;
        2. otherwise Windows OCR on its five passes: when it lands on what the model read (its vote, one of its
           passes, or the same characters in another case), OCR's reading is taken;
        3. the engines disagree, or a reading does not fit the box, or nothing was read: ONE small vision call when a
           configured model takes pictures (counted in vision_calls), instead of typing a likely wrong guess;
        4. no vision: the model's vote, else OCR's (a try costs one of the three, and the user is asked after)."""
        hints = hints or Hints()
        self.last_via = ""
        model_answer: str | None = None
        model_fits: list[str] = []
        if self.model is not None:
            model_answer, model_fits, sure = await self._engine("model", self.model, shot, hints)
            if sure:
                self.last_via = "model"
                return model_answer
        ocr_answer: str | None = None
        if self.ocr is not None:
            ocr_answer, _fits, _sure = await self._engine("ocr", self.ocr, shot, hints)
        if model_answer and ocr_answer and (ocr_answer == model_answer or ocr_answer in model_fits
                                            or ocr_answer.lower() == model_answer.lower()):
            # the same characters: Windows OCR, a general text reader, also settles their case
            self.last_via = "model+ocr"
            return ocr_answer
        if ocr_answer and self.model is None:  # Windows OCR is the only engine here: its vote is the answer
            self.last_via = "ocr"
            return ocr_answer
        guess, via = (model_answer, "model") if model_answer else (ocr_answer, "ocr")
        if self._can_see() and self.vision is not None:
            crop = base64.b64encode(small_crop(shot)).decode()
            self.vision_calls += 1
            seen: str | None = None
            with contextlib.suppress(Exception):  # no model, model error: counts as a failed read
                reply = (await self.vision(crop, hint=hints.describe()) if getattr(self.vision, "takes_hint", False)
                         else await self.vision(crop))
                seen = pick([tok], hints) if (tok := clean_answer(reply)) else None
            _log_read("vision", [seen or ""], [seen], seen)
            if seen:
                self.last_via = "vision"
                return seen
        if guess:
            self.last_via = via
        return guess

    @staticmethod
    async def _hints(page: CaptchaPage, field: str = "") -> Hints:
        get = getattr(page, "field_hints", None)
        if not callable(get):
            return Hints()
        with contextlib.suppress(Exception):
            return hints_from(await get(field))
        return Hints()

    async def auto_fill(self, page: CaptchaPage) -> tuple[bool, str, bytes | None]:
        """For a saved-login sign-in: (typed an answer, why not, the current captcha picture). Reads only where the
        user allowed it, at most MAX_AUTO_ATTEMPTS per site in ATTEMPT_WINDOW_S, asking the site for a new picture
        when one could not be read. Never asks a person itself and never submits: the sign-in does both."""
        host = host_of(page.url)
        if not host:
            return False, "no web page is open", None
        if kind := challenge_kind(await page.challenge_markers()):
            self._audit(host, "refused", False, challenge=kind)
            return False, f"this page uses {kind}, which Delta never tries to solve or get around", None
        if not await page.has_field(""):
            return False, "no captcha box was found on the form", None
        if await page.is_secret_field(""):
            return False, "REFUSED: the box that looked like a captcha is a password or payment box", None
        shot = await page.screenshot("")
        if shot is None:
            return False, "no captcha picture was found next to the box", None
        if not self.allowed(page.url):
            return False, f"Delta does not read the captcha on {host} (that switch is off in Settings > Logins)", shot
        if not self.can_read():
            return False, ("there is no way to read it on this PC (Windows OCR is not available and no AI model that "
                           "takes pictures is set up)"), shot
        hints = await self._hints(page)
        tried = False
        while self._used(host) < MAX_AUTO_ATTEMPTS:
            attempt = self._count(host)
            if tried:  # this picture could not be read: ask the site for a new one first
                with contextlib.suppress(Exception):
                    if await page.refresh("") and (new := await page.screenshot("")):
                        shot = new
            tried = True
            answer = await self._read(shot, hints)
            if answer:
                await page.fill("", answer)
            self._audit(host, "auto", bool(answer), attempt=attempt, via=self.last_via or "none")
            if answer:
                how = "by the AI model (one small picture)" if self.last_via == "vision" else "on this PC"
                return True, f"read {how} (attempt {attempt} of {MAX_AUTO_ATTEMPTS})", shot
        return False, f"Delta could not read the captcha on {host} after {MAX_AUTO_ATTEMPTS} tries", shot

    def _audit(self, host: str, mode: str, ok: bool, **extra: Any) -> None:
        self.audit.record("captcha", site=host, mode=mode, ok=ok, **extra)  # never the answer

    async def solve(self, page: CaptchaPage, image: str = "", field: str = "") -> ToolResult:
        host = host_of(page.url)
        if not host:
            return ToolResult(False, "no web page is open in Delta's browser")
        if kind := challenge_kind(await page.challenge_markers()):
            self._audit(host, "refused", False, challenge=kind)
            return ToolResult(False, (
                f"NEEDS HUMAN: this page uses {kind}, which Delta never tries to solve or get around. "
                "Ask the user to complete it themselves, then continue."), {"site": host, "challenge": kind})
        if not await page.has_field(field):
            return ToolResult(False, "no captcha input field found; pass field= (its label or a CSS selector)")
        if await page.is_secret_field(field):
            return ToolResult(False, "REFUSED: that is a password/payment field, not a captcha box")
        shot = await page.screenshot(image)
        if shot is None:
            return ToolResult(False, "no captcha image found; pass image= (a CSS selector or its alt text)")
        b64 = base64.b64encode(shot).decode()

        if not self.allowed(page.url):
            reason = "site not in captcha.allowed_sites"
        elif not self.can_read():
            reason = "no way to read it on this PC (no Windows OCR, and pictures may not go to a model)"
        else:
            reason = "automatic attempts used up"
            hints = await self._hints(page, field)
            while self._used(host) < MAX_AUTO_ATTEMPTS:
                attempt = self._count(host)
                if attempt > 1:  # the last picture was used: ask the site for a new one before reading again
                    with contextlib.suppress(Exception):
                        if await page.refresh(image) and (new := await page.screenshot(image)):
                            shot, b64 = new, base64.b64encode(new).decode()
                answer = await self._read(shot, hints)
                if answer:
                    await page.fill(field, answer)
                self._audit(host, "auto", bool(answer), attempt=attempt, via=self.last_via or "none")
                if answer:
                    last = attempt >= MAX_AUTO_ATTEMPTS
                    return ToolResult(True, (
                        f"typed the captcha characters into the field on {host} (auto attempt {attempt} of "
                        f"{MAX_AUTO_ATTEMPTS}). Submit the form as the task needs; if the site says the captcha "
                        f"was wrong, call solve_captcha again{' (the user will be asked)' if last else ''}."),
                        {"site": host, "mode": "auto", "attempt": attempt})
        return await self._handoff(page, host, field, b64, reason)

    async def _handoff(self, page: CaptchaPage, host: str, field: str, b64: str, reason: str) -> ToolResult:
        saved = self.save_image(b64, f"captcha-{host}") or ""
        typed = await self.ask_human(host, saved)
        ok = typed and bool((await page.field_value(field)).strip())
        self._audit(host, "human", ok, reason=reason)
        if ok:
            return ToolResult(True, f"the user typed the captcha on {host}; continue the task",
                              {"site": host, "mode": "human"})
        why = "the field is still empty" if typed else "the user did not confirm"
        return ToolResult(False, f"NEEDS HUMAN: captcha on {host} not entered ({why}). Do not retry; tell the user.",
                          {"site": host, "mode": "human"})


def approval_handoff(approvals: ApprovalBroker) -> AskHuman:
    """Human handoff over the existing approval channels: the user types the captcha on the PC
    and approves; deny or no answer stops."""

    async def ask(host: str, saved: str) -> bool:
        from delta.core.state import CURRENT_REQUEST

        req = CURRENT_REQUEST.get()
        where = f" (image saved with this run's proof as {saved})" if saved else ""
        outcome = await approvals.request(ApprovalRequest(
            tool="solve_captcha", risk=Risk.ACT,
            summary=f"Captcha on {host}: type the characters into the page in Delta's browser on your PC, "
                    f"then Approve{where}. Deny to stop.",
            reason="captcha needs a human", task_id=req.id if req else ""))
        return outcome.approved

    return ask


def router_vision(router: Any) -> Vision:
    """One small, low-detail picture of the captcha to the configured vision model (only after OCR failed)."""

    async def read(b64: str, hint: str = "") -> str:
        want = f" It has {hint}." if hint else ""
        resp = await router.complete([{"role": "user", "content": [
            {"type": "text", "text": "This image is a text captcha." + want + " Reply with ONLY its characters, "
                                     "exactly as shown, no spaces, no other words."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"}}]}],
            task="vision")
        return str(resp.content)

    read.takes_hint = True  # type: ignore[attr-defined]
    return read


def router_sees(router: Any) -> Callable[[], bool]:
    """A configured model takes pictures right now (the vision route has a provider that is up)."""

    def ready() -> bool:
        with contextlib.suppress(Exception):
            return bool(router.candidates("vision"))
        return False

    return ready


def live_allowlist(data_dir: Path, shipped: list[str]) -> Callable[[], list[str]]:
    """Read the allowlist on every use, so `delta captcha remove` applies to the very next attempt."""

    def current() -> list[str]:
        user = read_user_settings(data_dir).get("captcha")
        if isinstance(user, dict) and isinstance(user.get("allowed_sites"), list):
            return [str(s) for s in user["allowed_sites"]]
        return list(shipped)

    return current


# ---------------------------------------------------------------- the tool

class CaptchaArgs(BaseModel):
    image: str = Field(default="", description="CSS selector or alt text of the captcha image (empty: find it)")
    field: str = Field(default="", description="Label, placeholder or CSS selector of the captcha input "
                                               "(empty: find it)")


class SolveCaptcha(_BrowserTool[CaptchaArgs]):
    name = "solve_captcha"
    description = ("Enter a simple image/text captcha on the open page: read automatically only on sites "
                   "the user allowed, otherwise the user is asked to type it. Never for reCAPTCHA, hCaptcha "
                   "or Cloudflare checks (refused). Does not submit the form.")
    risk = Risk.ACT
    Args = CaptchaArgs

    def __init__(self, mgr: BrowserManager, solver: CaptchaSolver) -> None:
        super().__init__(mgr)
        self.solver = solver

    def summary(self, args: CaptchaArgs) -> str:
        return f"enter the captcha on {host_of(self.mgr.current_url) or 'the page'}"

    async def run(self, args: CaptchaArgs) -> ToolResult:
        async def go() -> ToolResult:
            page = await self.mgr.page()
            self.mgr.current_url = page.url
            return await self.solver.solve(PlaywrightCaptchaPage(page), args.image, args.field)

        return await self._guarded(go)


def local_ocr(mode: str = "auto") -> Reader | None:
    """Windows OCR as a captcha reader (None off Windows or when OCR is switched off): the picture cleaned up five
    ways (delta.tools.captcha_ocr.variants), stacked, read in ONE OCR call, one reading per pass."""
    from delta.perception.ocr import WindowsOcr

    reader = WindowsOcr(mode)
    if not reader.platform_ok() or mode == "off":
        return None
    return stacked_reader(reader)


def stacked_reader(reader: Any) -> Reader:
    """A Reader over any OCR engine with WindowsOcr's `read(PIL image) -> [OcrLine]` (tests and evals pass one)."""

    async def read(jpeg: bytes) -> list[str]:
        import io

        from PIL import Image

        from delta.tools.captcha_ocr import band_readings, stack, variants

        bands = variants(Image.open(io.BytesIO(jpeg)))
        sheet, spans = stack(bands)
        return band_readings(await reader.read(sheet), spans)

    return read


def pictures_allowed(settings: Any) -> bool:
    """The user lets pictures go to a model (attachments.vision is not "off")."""
    att = getattr(settings, "attachments", None)
    return str(getattr(att, "vision", "auto") or "auto").lower() != "off"


def captcha_model_reader() -> ModelReader | None:
    """The captcha model as a reader (None when it is not installed here: Windows OCR is then the first engine)."""
    from delta.tools.captcha_model import shared_model

    model = shared_model()
    return None if model is None else model.read


def make_solver(settings: Any, router: Any, approvals: ApprovalBroker, audit: AuditLog,
                proof: Any = None) -> CaptchaSolver:
    """The captcha solver wired to Delta's settings: the captcha model, then Windows OCR, then (only when they are not
    sure and pictures are on) one small vision call."""
    return CaptchaSolver(
        model=captcha_model_reader(),
        allowed_sites=live_allowlist(settings.data_dir, settings.captcha.allowed_sites),
        vision=router_vision(router) if pictures_allowed(settings) else None,
        ask_human=approval_handoff(approvals), audit=audit,
        save_image=proof.save_image if proof else (lambda b64, label: None),
        ocr=local_ocr(getattr(getattr(settings, "perception", None), "ocr", "auto")),
        vision_ready=router_sees(router))


def captcha_tool(mgr: BrowserManager, settings: Any, router: Any, approvals: ApprovalBroker, audit: AuditLog,
                 proof: Any = None, solver: CaptchaSolver | None = None) -> SolveCaptcha:
    """The solve_captcha tool wired to Delta's settings, OCR, vision model, approvals and audit log."""
    return SolveCaptcha(mgr, solver or make_solver(settings, router, approvals, audit, proof))


def manage_allowlist(settings: Any, action: str, host: str | None, out: Callable[[str], None] = print) -> int:
    """`delta captcha allow|remove|list [host]`: the user's own switch, saved to their settings file."""
    from delta.config import write_user_settings

    current = live_allowlist(settings.data_dir, settings.captcha.allowed_sites)()
    if action == "list":
        out("\n".join(current) if current else "No sites: every captcha is handed to you (default).")
        return 0
    site = normalize_site(host or "")
    if not site:
        out("give a host, e.g. `delta captcha allow erp.example.edu` (or *.example.edu for its subdomains)")
        return 2
    if action == "allow":
        new = current if site in current else [*current, site]
        out(f"Delta may now read simple captchas on {site}. Use it only on your own accounts, "
            "within the site's terms.")
    else:
        new = [s for s in current if normalize_site(s) != site]
        out(f"removed {site}" if len(new) != len(current) else f"{site} was not in the list")
    write_user_settings(settings.data_dir, {"captcha": {"allowed_sites": new}})
    return 0
