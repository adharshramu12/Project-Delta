"""Typing a saved login into the user's OWN browser (their everyday, signed-in Chrome or Edge), with the same rules
as Delta's private browser and a few more, because it is their real browser with real tabs, sessions and extensions.

The model never sees or handles the password: it calls sign_in(site) and this module does the rest, through a
driver for the browser (OwnBrowserDriver). The real driver is delta.logins.own_uia (Windows UI Automation on the one
window Delta opens in the user's browser); evals and tests use stand-ins with the same interface.

What is the same as in the private browser (delta.logins.signin):
- exact-origin binding, checked right before every field is filled: the tab's top-frame origin and the origin of the
  frame that holds the form both equal the saved origin (no lookalike or punycode host, no http downgrade, no other
  port, no form inside a frame from another site); a tab on another origin that asks for a password is refused;
- reCAPTCHA, hCaptcha, Turnstile and one-time codes are handed to the user; a wrong password is reported and never
  retried; nothing here logs, audits or returns the password or the user name.

What is added for the user's own browser:
- Tab binding. The sign-in is bound to the tab that is active in Delta's window when it starts. Before and after
  every fill, and before submitting, Delta checks that the same window and the same tab are still the active ones
  and that the page is still on the saved origin. If the user switched tabs or windows, or the page went somewhere
  else, Delta stops at once, clears what it typed (best effort) and does not submit.
- Values are set on the field itself (not typed into whatever has focus), so a switch can never send the password
  to another tab; the fallback that types keys checks focus first.
- Fill check. After filling, the field must hold exactly as many characters as the saved value. If an extension or
  the page changed or emptied it, Delta reports it plainly, does not submit and does not try again.
- One submit, the normal one (Enter in the password box). A simple image captcha follows the same rule as in the
  private browser: read on this PC only where the user allowed it (the login's captcha switch or `delta captcha
  allow`), else the picture goes to the user, who replies with the characters (LoginService parks the filled form in
  its tab; answer_captcha() checks the tab and the origin again before typing it). When the driver finds no picture
  element, the user gets a band of the page around the captcha box with the password boxes painted over
  (captcha_region_image). Only when no picture at all can be taken, and only when the user is AT the PC (the request
  came from the dashboard or the terminal), does Delta ask them to type it and press Sign in themselves; a user who is
  away (Telegram) is never told to "complete it in the open browser". A "code" box that sits on the same form as the
  password box is a captcha (or a same-form code), never submitted empty.
- The first-use consent per site and browser lives in delta.logins.service (it is a permission question).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from delta.logins.origin import Origin, mismatch, origin_of
from delta.logins.signin import CAPTCHA_SUBMITS, ERROR_WORDS, CaptchaStep, Handoff, SignInResult, captcha_rejected
from delta.logins.store import Login

FIELD_KINDS = ("password", "user", "otp", "captcha")
# Field names (labels, placeholders) as UI Automation and the stand-ins report them.
USER_WORDS = re.compile(r"user|e-?mail|login|roll|regd|reg\.? ?no|registration|uid|student id|\bid\b|mobile|phone",
                        re.I)
OTP_WORDS = re.compile(r"one[- ]time|otp|totp|passcode|verification code|security code|2fa|two[- ]factor|"
                       r"authenticat(ion|or) code|\bcode\b", re.I)
CAPTCHA_WORDS = re.compile(r"captcha|characters|the text in the image|image code", re.I)
EMPTY_PAGE_WAIT_S = 8.0  # a page that shows nothing yet (hidden until a stylesheet loads) gets this long
CHALLENGE_WORDS = re.compile(r"(re)?captcha|hcaptcha|turnstile|i'?m not a robot|verify you are human|"
                             r"cloudflare|checking your browser", re.I)
SETTLE_S = 10.0


class DriverError(RuntimeError):
    """Something the browser did not do, in plain words for the user (never containing a secret)."""


class TabChangedError(DriverError):
    """The bound tab is no longer the active one in Delta's window, or the window is gone."""


@dataclass(frozen=True)
class TabBinding:
    """Which tab a sign-in is bound to: Delta's window and the tab active in it when the sign-in started."""

    window: str
    tab: str


@dataclass
class FieldRef:
    """One input box on the page, as the driver found it."""

    kind: str  # password | user | otp | captcha
    frame_url: str  # the address of the document (page or frame) that holds the box; "" = could not be read
    handle: Any = None  # the driver's own reference to the box
    name: str = ""


class OwnBrowserDriver(Protocol):
    browser_key: str  # chrome | edge: the one-time consent is per site and per browser
    browser_label: str  # "Google Chrome"

    async def ensure_window(self, url: str) -> bool: ...  # Delta's window exists (opened on url if not)
    async def binding(self) -> TabBinding | None: ...  # Delta's window and its active tab, right now
    async def top_url(self) -> str: ...  # the active tab's full address ("" when it cannot be read)
    async def navigate(self, url: str) -> None: ...  # in the bound tab
    async def fields(self) -> list[FieldRef]: ...  # visible, editable boxes in the active tab, every frame
    async def frame_url(self, f: FieldRef) -> str: ...  # the box's document address, read again now
    async def set_value(self, f: FieldRef, value: str, secret: bool) -> None: ...  # into that box only
    async def value_length(self, f: FieldRef) -> int | None: ...  # None = cannot be read
    async def submit(self, f: FieldRef) -> None: ...  # Enter in that box, only if it has focus in Delta's tab
    async def text(self) -> str: ...  # visible text of the active tab
    async def challenge(self) -> str | None: ...  # "reCAPTCHA" and the like, else None
    async def settle(self) -> None: ...  # wait for the page to load after an action
    def remember_secret_box(self, f: FieldRef) -> None: ...  # masked in every later screenshot
    # Optional (getattr): a JPEG of the captcha picture next to box f (None when it cannot be taken), and a new
    # captcha picture (False when the page has no way to give one).
    # async def captcha_image(self, f: FieldRef) -> bytes | None: ...
    # async def refresh_captcha(self, f: FieldRef) -> bool: ...


OwnCaptchaHook = Callable[[Any, int], Awaitable[CaptchaStep]]  # (a CaptchaPage over the driver, attempt) -> step


def classify(name: str, password: bool, auto_id: str = "") -> str:
    """The kind of a text box from its accessible name and its HTML id (UI Automation's AutomationId; '' = not one
    Delta fills). "loginFormCaptcha" with the placeholder "Enter verification Code" is a captcha, not an OTP box."""
    if password:
        return "password"
    if CAPTCHA_WORDS.search(name or "") or CAPTCHA_WORDS.search(auto_id or ""):
        return "captcha"
    if OTP_WORDS.search(name or ""):
        return "otp"
    if USER_WORDS.search(name or ""):
        return "user"
    return ""


class _OwnCaptchaPage:
    """delta.tools.captcha's CaptchaPage over the own-browser driver, so the captcha rules (allowlist, OCR first, at
    most 3 tries, a new picture when one cannot be read) are the same code as in the private browser. Filling goes
    through OwnBrowserSignIn's checks (same tab, exact origin) like every other box."""

    def __init__(self, d: OwnBrowserDriver, box: FieldRef, url: str,
                 fill: Callable[[str], Awaitable[str]]) -> None:
        self.d, self.box, self.url, self._fill = d, box, url, fill

    async def challenge_markers(self) -> list[str]:
        return []  # robot checks were looked for already (d.challenge())

    async def screenshot(self, image: str) -> bytes | None:
        take = getattr(self.d, "captcha_image", None)
        return await take(self.box) if callable(take) else None

    async def has_field(self, field: str) -> bool:
        return True

    async def is_secret_field(self, field: str) -> bool:
        return self.box.kind == "password"

    async def fill(self, field: str, value: str) -> None:
        if why := await self._fill(value):
            raise DriverError(why)

    async def field_value(self, field: str) -> str:
        n = await self.d.value_length(self.box)
        return "x" * (n or 0)

    async def refresh(self, image: str) -> bool:
        new = getattr(self.d, "refresh_captcha", None)
        return bool(await new(self.box)) if callable(new) else False

    async def region_shot(self) -> bytes | None:
        """For the user only: the page around the captcha box, password boxes painted over."""
        take = getattr(self.d, "captcha_region_image", None)
        return await take(self.box) if callable(take) else None


async def _no_fill(value: str) -> str:
    return "not used here"


class OwnBrowserSignIn:
    def __init__(self, handoff: Handoff, settle_s: float = SETTLE_S, captcha: OwnCaptchaHook | None = None,
                 remote: bool = False) -> None:
        self.handoff = handoff
        self.settle_s = settle_s
        self.captcha = captcha
        self.remote = remote  # the request came from a phone: the user is not at the PC
        self.bound: TabBinding | None = None  # the tab this sign-in is bound to (kept when it waits for the user)

    # --- checks --------------------------------------------------------------------------------------------
    async def _same_tab(self, d: OwnBrowserDriver, bound: TabBinding) -> None:
        now = await d.binding()
        if now is None or now.window != bound.window:
            raise TabChangedError("Delta's browser window is gone or is no longer the one it was using")
        if now.tab != bound.tab:
            raise TabChangedError("you switched to another tab while Delta was signing in")

    async def _check(self, d: OwnBrowserDriver, bound: TabBinding, saved: Origin, f: FieldRef) -> str:
        """'' when the box may be filled now; else why not. TabChangedError when the tab changed."""
        await self._same_tab(d, bound)
        if why := mismatch(saved, await d.top_url()):
            return why
        where = origin_of(await d.frame_url(f))
        if where is None:
            return "Delta could not confirm the address of the frame that holds the form"
        if where != saved:
            return f"the form's frame is on {where}, not {saved}"
        return ""

    async def _fill(self, d: OwnBrowserDriver, bound: TabBinding, saved: Origin, f: FieldRef, value: str,
                    secret: bool, filled: list[FieldRef]) -> str:
        """Fill one box after the checks; '' when it holds the value. Raises TabChangedError. The box goes on
        `filled` before anything is put in it, so a stop at any point clears it."""
        if why := await self._check(d, bound, saved, f):
            return "REFUSED: " + why
        filled.append(f)
        await d.set_value(f, value, secret)
        if secret:
            d.remember_secret_box(f)
        await self._same_tab(d, bound)  # a switch during the fill: stop before anything else happens
        n = await d.value_length(f)
        if n is not None and n != len(value):
            what = "password" if secret else "user name"
            raise DriverError(f"the {what} box did not keep what Delta filled in (a browser extension or the page "
                              "may have changed it)")
        return ""

    async def _clear(self, d: OwnBrowserDriver, boxes: list[FieldRef]) -> None:
        for f in boxes:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(d.set_value(f, "", f.kind == "password"), 5.0)

    async def _form(self, d: OwnBrowserDriver, saved: Origin) -> tuple[list[FieldRef], str]:
        """The boxes of the sign-in form on the saved origin, or a refusal when the password box is in a frame
        from another site."""
        found = await d.fields()
        mine: list[FieldRef] = []
        foreign = ""
        for f in found:
            where = origin_of(f.frame_url)
            if where != saved:
                if f.kind == "password":
                    foreign = str(where) if where else "an unknown page"
                continue
            mine.append(f)
        if foreign and not any(f.kind == "password" for f in mine):
            return [], (f"the sign-in form is inside a frame from another site ({foreign}), not {saved}. Delta "
                        "only types a password on the exact saved site")
        return mine, ""

    @staticmethod
    def _pick(boxes: list[FieldRef], kind: str) -> FieldRef | None:
        return next((f for f in boxes if f.kind == kind), None)

    @classmethod
    def _captcha_box(cls, boxes: list[FieldRef]) -> FieldRef | None:
        """The captcha box of a sign-in form. A "code" box on the same form as the password box is one too: a real
        one-time code is asked AFTER the password, so one shown with it is a captcha (or a same-form code). Either
        way a person or OCR fills it; it is never submitted empty."""
        if found := cls._pick(boxes, "captcha"):
            return found
        pw = cls._pick(boxes, "password")
        return next((f for f in boxes if f.kind == "otp" and pw is not None and f.frame_url == pw.frame_url), None)

    @classmethod
    def _otp_boxes(cls, boxes: list[FieldRef]) -> list[FieldRef]:
        """One-time-code boxes, not counting the captcha box of a form that is back after a wrong captcha."""
        cap = cls._captcha_box(boxes)
        return [f for f in boxes if f.kind == "otp" and f is not cap]

    async def _form_soon(self, d: OwnBrowserDriver, saved: Origin, bound: TabBinding) -> tuple[list[FieldRef], str]:
        """_form, but a page that shows nothing yet (many ERPs keep the page hidden until a stylesheet arrives) gets up
        to EMPTY_PAGE_WAIT_S before 'no form' is believed."""
        boxes, refusal = await self._form(d, saved)
        loop = asyncio.get_running_loop()
        end = loop.time() + EMPTY_PAGE_WAIT_S
        while not boxes and not refusal and loop.time() < end and not (await d.text()).strip():
            await asyncio.sleep(0.5)
            await self._same_tab(d, bound)
            boxes, refusal = await self._form(d, saved)
        return boxes, refusal

    async def _has_password(self, d: OwnBrowserDriver) -> bool:
        return any(f.kind == "password" for f in await d.fields())

    async def _settle(self, d: OwnBrowserDriver) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(d.settle(), self.settle_s)

    # --- the whole sign-in ----------------------------------------------------------------------------------
    async def sign_in(self, d: OwnBrowserDriver, login: Login) -> SignInResult:
        filled: list[FieldRef] = []
        url = ""
        try:
            return await self._sign_in(d, login, filled)
        except TabChangedError as e:
            await self._clear(d, filled)
            with contextlib.suppress(Exception):
                url = await d.top_url()
            return SignInResult("aborted", f"STOPPED: {e}. Delta stopped at once, cleared what it had typed and did "
                                           "not submit anything. Ask the user before trying again.", url)
        except DriverError as e:
            await self._clear(d, filled)
            with contextlib.suppress(Exception):
                url = await d.top_url()
            return SignInResult("failed", f"could not sign in to {login.site.host} in your {d.browser_label}: {e}. "
                                          "Delta did not submit and will not try again.", url)

    async def _sign_in(self, d: OwnBrowserDriver, login: Login, filled: list[FieldRef]) -> SignInResult:
        saved = login.site
        start = login.login_url or str(saved) + "/"
        if not await d.ensure_window(start):
            return SignInResult("failed", f"could not open a window of {d.browser_label} for {saved.host}")
        bound = await d.binding()
        if bound is None:
            return SignInResult("failed", f"could not find Delta's window in {d.browser_label}")
        self.bound = bound
        url = await d.top_url()
        # 1. where we are
        if mismatch(saved, url):
            if origin_of(url) is not None and await self._has_password(d):
                why = mismatch(saved, url)
                return SignInResult("refused", f"REFUSED: this tab asks for a password but {why}. Delta did not "
                                               f"type anything. If this is unexpected, it may be a phishing page.",
                                    url)
            await d.navigate(start)
            await self._settle(d)
            await self._same_tab(d, bound)
            url = await d.top_url()
            if why := mismatch(saved, url):
                return SignInResult("refused", f"REFUSED: opening {start} ended up somewhere else: {why}. If the site "
                                               "now signs in on another address, save a login for that address.",
                                    url)
        # 2. challenges built to stop programs: the user's job
        if kind := await d.challenge():
            if not await self.handoff("challenge", saved.host, f"{kind} on {saved.host}: complete it in your "
                                                                 f"{d.browser_label} on your PC, then Approve"):
                return SignInResult("needs_user", f"NEEDS HUMAN: {saved.host} shows {kind}, which Delta never "
                                                  "solves. The user did not complete it.", url)
            await self._settle(d)
            await self._same_tab(d, bound)
        boxes, refusal = await self._form_soon(d, saved, bound)
        if refusal:
            return SignInResult("refused", "REFUSED: " + refusal, await d.top_url())
        user, pw = self._pick(boxes, "user"), self._pick(boxes, "password")
        if user is None and pw is None:
            if login.login_url and (await d.top_url()).rstrip("/") != login.login_url.rstrip("/"):
                await d.navigate(login.login_url)
                await self._settle(d)
                await self._same_tab(d, bound)
                if why := mismatch(saved, await d.top_url()):
                    return SignInResult("refused", f"REFUSED: the login page ended up somewhere else: {why}",
                                        await d.top_url())
                boxes, refusal = await self._form(d, saved)
                if refusal:
                    return SignInResult("refused", "REFUSED: " + refusal, await d.top_url())
                user, pw = self._pick(boxes, "user"), self._pick(boxes, "password")
            if user is None and pw is None:
                return SignInResult("already_signed_in", f"no sign-in form on {saved}: already signed in (your "
                                                         "browser kept the session)", await d.top_url())
        # 3. a user-name-only first step (the password comes on the next page)
        if pw is None and user is not None:
            if not login.username:
                return SignInResult("no_form", f"{saved.host} asks for a user name, but the saved login "
                                               f"'{login.name}' has no user name. Nothing was typed. Save it again "
                                               "with the user name in Settings > Logins (Replace).", await d.top_url())
            if why := await self._fill(d, bound, saved, user, login.username, False, filled):
                return SignInResult("refused", why, await d.top_url())
            await self._check_then_submit(d, bound, saved, user)
            filled.clear()
            await self._settle(d)
            await self._same_tab(d, bound)
            boxes, refusal = await self._form(d, saved)
            if refusal:
                return SignInResult("refused", "REFUSED: " + refusal, await d.top_url())
            pw, user = self._pick(boxes, "password"), None
            if pw is None:
                return SignInResult("no_form", f"entered the user name on {saved.host} but no password box came "
                                               f"(now at {await d.top_url()}). Nothing else was typed.",
                                    await d.top_url())
        assert pw is not None
        if not login.password:
            return SignInResult("no_form", f"{saved.host} asks for a password, but the saved login '{login.name}' has "
                                           "no password saved (only a user name). Nothing was typed. Save it again "
                                           "with the password in Settings > Logins (Replace).", await d.top_url())
        result = SignInResult("failed", "not submitted", await d.top_url())
        for attempt in range(1, CAPTCHA_SUBMITS + 2):  # the last round only fetches the picture for the user
            if user is not None and login.username and (
                    why := await self._fill(d, bound, saved, user, login.username, False, filled)):
                return SignInResult("refused", why, await d.top_url())
            if why := await self._fill(d, bound, saved, pw, login.password, True, filled):
                await self._clear(d, filled)
                return SignInResult("refused", why, await d.top_url())
            # 4. a simple captcha on the form: read here where allowed, else the user reads it
            before = await d.top_url()
            cap = self._captcha_box(boxes)
            if cap is not None:
                step = await self._captcha_step(d, bound, saved, cap, attempt, filled)
                if step is not None:
                    filled.clear()  # the form waits for the user with what Delta filled
                    return step
            elif attempt > 1:
                return result  # the captcha went away; the last answer stands
            await self._check_then_submit(d, bound, saved, pw)
            filled.clear()
            await self._settle(d)
            result = await self._after(d, bound, saved, before)
            if not (cap is not None and result.outcome in ("wrong_password", "failed")
                    and captcha_rejected(await d.text())):
                return result
            boxes, refusal = await self._form(d, saved)  # only the captcha was wrong: the form is back
            if refusal:
                return SignInResult("refused", "REFUSED: " + refusal, await d.top_url())
            user, pw = self._pick(boxes, "user"), self._pick(boxes, "password")
            if pw is None:
                return result
        return SignInResult("needs_user", f"NEEDS HUMAN: {saved.host} kept rejecting the captcha; Delta stopped "
                                          f"after {CAPTCHA_SUBMITS} tries.", await d.top_url())

    async def _captcha_step(self, d: OwnBrowserDriver, bound: TabBinding, saved: Origin, cap: FieldRef, attempt: int,
                            filled: list[FieldRef]) -> SignInResult | None:
        """None when Delta typed the captcha (go on and submit); else the result that ends this round: the picture for
        the user (parked by LoginService), or the old hand-off when no picture can be taken here."""
        url = await d.top_url()

        async def fill(value: str) -> str:
            return await self._fill(d, bound, saved, cap, value, False, filled)

        cpage = _OwnCaptchaPage(d, cap, url, fill)
        step = await self.captcha(cpage, attempt) if self.captcha is not None else CaptchaStep(False, "", None)
        if step.entered:
            return None
        if step.image is None:
            image = None
            with contextlib.suppress(Exception):
                image = await cpage.screenshot("") or await cpage.region_shot()
            step = CaptchaStep(False, step.note, image)
        if step.image is not None:
            return SignInResult("needs_user", f"CAPTCHA: {step.note or 'it needs a person to read it'}", url,
                                captcha_image=step.image)
        if self.remote:  # away from the PC: never "complete it in the open browser"
            return SignInResult("needs_user", f"NEEDS HUMAN: {saved.host} shows a captcha and Delta could not read "
                                              "it or take a picture of it to send you. Nothing was submitted. Ask "
                                              "again in a minute (a new picture often reads), or sign in when you "
                                              "are back at the PC.", url)
        done = await self.handoff("captcha", saved.host, f"{saved.host} shows a captcha: type the characters and "
                                                         f"press Sign in in your {d.browser_label}, then Approve")
        if not done:
            return SignInResult("needs_user", f"NEEDS HUMAN: {saved.host} has a captcha on the form; the user did "
                                              "not complete it.", await d.top_url())
        await self._settle(d)
        return await self._after(d, bound, saved, url)

    async def answer_captcha(self, d: OwnBrowserDriver, bound: TabBinding | None, login: Login,
                             answer: str) -> SignInResult:
        """Finish a sign-in that waited for the user to read the captcha, in the same tab: the tab and the origin
        are checked again, the password filled again if the page cleared it, the answer typed, one submit, then the
        same check that it worked. A captcha the site rejects comes back with the new picture."""
        saved = login.site
        filled: list[FieldRef] = []
        if bound is None:
            return SignInResult("failed", "the sign-in lost its tab; ask again to start over")
        try:
            await self._same_tab(d, bound)
            if why := mismatch(saved, await d.top_url()):
                return SignInResult("refused", f"REFUSED: the sign-in tab is no longer on the saved site: {why}. "
                                               "Nothing was typed.", await d.top_url())
            boxes, refusal = await self._form(d, saved)
            if refusal:
                return SignInResult("refused", "REFUSED: " + refusal, await d.top_url())
            user, pw, cap = self._pick(boxes, "user"), self._pick(boxes, "password"), self._captcha_box(boxes)
            if pw is None or cap is None:
                return SignInResult("no_form", f"the sign-in form on {saved.host} is gone; ask again to start over.",
                                    await d.top_url())
            if user is not None and login.username and not await d.value_length(user) and (
                    why := await self._fill(d, bound, saved, user, login.username, False, filled)):
                return SignInResult("refused", why, await d.top_url())
            if await d.value_length(pw) != len(login.password) and (
                    why := await self._fill(d, bound, saved, pw, login.password, True, filled)):
                return SignInResult("refused", why, await d.top_url())
            if why := await self._fill(d, bound, saved, cap, answer, False, filled):
                return SignInResult("refused", why, await d.top_url())
            before = await d.top_url()
            await self._check_then_submit(d, bound, saved, pw)
            filled.clear()
            await self._settle(d)
            result = await self._after(d, bound, saved, before)
            if result.outcome in ("wrong_password", "failed") and captcha_rejected(await d.text()):
                boxes, _ = await self._form(d, saved)
                image = None
                if (cap := self._captcha_box(boxes)) is not None:
                    shot = _OwnCaptchaPage(d, cap, await d.top_url(), _no_fill)
                    with contextlib.suppress(Exception):
                        image = await shot.screenshot("") or await shot.region_shot()
                return SignInResult("needs_user", f"CAPTCHA: {saved.host} said the characters were wrong; here is "
                                                  "the new picture.", await d.top_url(), captcha_image=image)
            return result
        except TabChangedError as e:
            await self._clear(d, filled)
            return SignInResult("aborted", f"STOPPED: {e}. Delta cleared what it had typed and did not submit.", "")
        except DriverError as e:
            await self._clear(d, filled)
            return SignInResult("failed", f"could not finish signing in to {saved.host}: {e}.", "")

    async def _check_then_submit(self, d: OwnBrowserDriver, bound: TabBinding, saved: Origin, f: FieldRef) -> None:
        if why := await self._check(d, bound, saved, f):
            raise TabChangedError(f"the page changed before Delta could submit ({why})")
        await d.submit(f)

    async def _after(self, d: OwnBrowserDriver, bound: TabBinding, saved: Origin, before: str) -> SignInResult:
        try:
            await self._same_tab(d, bound)
        except TabChangedError as e:
            return SignInResult("aborted", f"STOPPED after submitting: {e}, so Delta did not read the result. Check "
                                           "the tab yourself.", "")
        for _ in range(2):  # an OTP step, then whatever comes after it
            url = await d.top_url()
            if mismatch(saved, url) and origin_of(url) is not None and not await self._has_password(d):
                return SignInResult("signed_in", f"signed in to {saved.host}; the site moved on to {url}", url)
            if self._otp_boxes(await d.fields()):
                done = await self.handoff("otp", saved.host, f"{saved.host} asks for a one-time code or second step: "
                                                             f"enter it in your {d.browser_label} on your PC, then "
                                                             "Approve")
                if not done:
                    return SignInResult("needs_user", f"NEEDS HUMAN: {saved.host} wants a one-time code; the user did "
                                                      "not complete it in time.", url)
                for _ in range(30):
                    await self._settle(d)
                    if not self._otp_boxes(await d.fields()):
                        break
                    await asyncio.sleep(0.5)
                else:
                    return SignInResult("needs_user", f"NEEDS HUMAN: {saved.host} still asks for the one-time code.",
                                        await d.top_url())
                continue
            break
        url = await d.top_url()
        if kind := await d.challenge():
            return SignInResult("needs_user", f"NEEDS HUMAN: after signing in, {saved.host} shows {kind}.", url)
        if await self._has_password(d):
            if ERROR_WORDS.search(await d.text()):
                return SignInResult("wrong_password", f"{saved.host} did not accept the saved login (it says the "
                                                      "details are wrong). Delta will not retry: update it with "
                                                      "`delta logins add --replace` or in Settings > Logins.", url)
            return SignInResult("failed", f"the sign-in form on {saved.host} is still showing after submitting; "
                                          "it may need something else (read the page).", url)
        return SignInResult("signed_in", f"signed in to {saved.host} in your {d.browser_label} (now at {url})", url)


__all__ = ["CHALLENGE_WORDS", "DriverError", "FieldRef", "OwnBrowserDriver", "OwnBrowserSignIn", "TabBinding",
           "TabChangedError", "classify"]
