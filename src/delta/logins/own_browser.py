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
- One submit, the normal one (Enter in the password box). A simple image captcha is not solved here: the user types
  it and presses Sign in themselves.
- The first-use consent per site and browser lives in delta.logins.service (it is a permission question).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass
from typing import Any, Protocol

from delta.logins.origin import Origin, mismatch, origin_of
from delta.logins.signin import ERROR_WORDS, Handoff, SignInResult
from delta.logins.store import Login

FIELD_KINDS = ("password", "user", "otp", "captcha")
# Field names (labels, placeholders) as UI Automation and the stand-ins report them.
USER_WORDS = re.compile(r"user|e-?mail|login|roll|regd|reg\.? ?no|registration|uid|student id|\bid\b|mobile|phone",
                        re.I)
OTP_WORDS = re.compile(r"one[- ]time|otp|totp|passcode|verification code|security code|2fa|two[- ]factor|"
                       r"authenticat(ion|or) code|\bcode\b", re.I)
CAPTCHA_WORDS = re.compile(r"captcha|characters|the text in the image|image code", re.I)
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


def classify(name: str, password: bool) -> str:
    """The kind of a text box from its accessible name ('' = not one Delta fills)."""
    if password:
        return "password"
    if CAPTCHA_WORDS.search(name or ""):
        return "captcha"
    if OTP_WORDS.search(name or ""):
        return "otp"
    if USER_WORDS.search(name or ""):
        return "user"
    return ""


class OwnBrowserSignIn:
    def __init__(self, handoff: Handoff, settle_s: float = SETTLE_S) -> None:
        self.handoff = handoff
        self.settle_s = settle_s

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
        boxes, refusal = await self._form(d, saved)
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
                return SignInResult("no_form", "the page asks for a user name, but this login has none saved",
                                    await d.top_url())
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
                return SignInResult("no_form", f"entered the user name on {saved.host} but no password box came",
                                    await d.top_url())
        assert pw is not None
        if not login.password:
            return SignInResult("no_form", "the page asks for a password, but this login has none saved",
                                await d.top_url())
        if user is not None and login.username and (
                why := await self._fill(d, bound, saved, user, login.username, False, filled)):
            return SignInResult("refused", why, await d.top_url())
        if why := await self._fill(d, bound, saved, pw, login.password, True, filled):
            await self._clear(d, filled)
            return SignInResult("refused", why, await d.top_url())
        # 4. submit once, the normal way; a simple captcha is the user's to type and submit
        before = await d.top_url()
        if self._pick(boxes, "captcha") is not None:
            filled.clear()  # the user finishes this form; it must keep what Delta filled
            done = await self.handoff("captcha", saved.host, f"{saved.host} shows a captcha: type the characters and "
                                                             f"press Sign in in your {d.browser_label}, then Approve")
            if not done:
                return SignInResult("needs_user", f"NEEDS HUMAN: {saved.host} has a captcha on the form; the user did "
                                                  "not complete it.", await d.top_url())
        else:
            await self._check_then_submit(d, bound, saved, pw)
            filled.clear()
        await self._settle(d)
        return await self._after(d, bound, saved, before)

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
            if any(f.kind == "otp" for f in await d.fields()):
                done = await self.handoff("otp", saved.host, f"{saved.host} asks for a one-time code or second step: "
                                                             f"enter it in your {d.browser_label} on your PC, then "
                                                             "Approve")
                if not done:
                    return SignInResult("needs_user", f"NEEDS HUMAN: {saved.host} wants a one-time code; the user did "
                                                      "not complete it in time.", url)
                for _ in range(30):
                    await self._settle(d)
                    if not any(f.kind == "otp" for f in await d.fields()):
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
