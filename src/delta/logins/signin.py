"""Typing a saved login into a page, in Delta's private browser (Playwright), only on the exact saved origin.

The model never sees or handles the password: it calls sign_in(site) and this module does the rest.

1. Where: if the open tab is not on the login's origin, Delta opens the saved login page (or the site's home page)
   itself. A tab on another origin that shows a password box is refused instead: that is how phishing looks.
2. Checks before typing, every time, right before each field is filled: the tab's origin and the origin of the frame
   that holds the form both equal the saved origin (no lookalike host, no http downgrade, no other port, no frame
   from another site). Pages that need reCAPTCHA, hCaptcha or Turnstile are handed to the user.
3. Typing: the user name and the password go straight from the sealed store into the page (Playwright fill, over
   the local remote-control connection). The password box is then masked with CSS so a picture of the page sent to
   a model never shows it, even if the site has a "show password" button.
4. A simple image captcha on the form: read automatically where the user allows it (the login's "let Delta read it"
   switch, ON by default for a login with a password, or `delta captcha allow <host>`): Windows OCR first, one small
   vision call only when OCR fails (delta.tools.captcha), at most CAPTCHA_SUBMITS submits when the site says only the
   captcha was wrong, the sign-in checked after each. Otherwise, or after that, the result carries the captcha
   picture and the caller asks the user to type the characters (LoginService parks the filled form until they
   answer, from the dashboard or Telegram). reCAPTCHA, hCaptcha and Turnstile are never solved.
5. After submitting: a one-time code or 2FA step is handed to the user (they finish it on the PC; Delta waits, with
   a time limit); a wrong password is reported and never retried (no lockouts); otherwise the result says where the
   page is now, checked: the sign-in form is gone.

Every outcome is one of OUTCOMES. Nothing here logs, audits or returns the password or the user name.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from delta.logins.origin import Origin, mismatch, origin_of
from delta.logins.store import Login

OUTCOMES = ("signed_in", "already_signed_in", "wrong_password", "refused", "needs_user", "no_form", "failed",
            "aborted")  # aborted: the user switched tabs or the page moved mid-fill (own browser, own_browser.py)

# Fields, found inside one frame. Order matters: the most specific selector first.
PASSWORD_SEL = "input[type=password]"
# Never the captcha box: "LoginForm[captcha]" also matches input[name*=login i] (a common ERP form).
USER_SEL = ", ".join(f"{part}:not([name*=captcha i]):not([id*=captcha i]):not([name*=verifycode i])"
                     for part in (
    "input[autocomplete=username]", "input[type=email]", "input[name*=user i]", "input[id*=user i]",
    "input[name*=login i]", "input[id*=login i]", "input[name*=email i]", "input[name*=roll i]",
    "input[name*=regd i]", "input[name*=reg_no i]", "input[name*=uid i]", "input[name*=userid i]",
    "input[placeholder*=user i]", "input[placeholder*=email i]", "input[placeholder*=roll i]",
    "input[aria-label*=user i]", "input[aria-label*=email i]"))
# A captcha box is never a one-time-code box, even when its placeholder says "Enter verification Code" (as on many
# college ERPs): treating it as an OTP step sent the user a "one-time code" hand-off for a captcha.
_NOT_CAPTCHA = (":not([name*=captcha i]):not([id*=captcha i]):not([placeholder*=captcha i])"
                ":not([name*=verifycode i]):not([id*=verifycode i])")
OTP_SEL = ", ".join(f"{part}{_NOT_CAPTCHA}" for part in (
    "input[autocomplete=one-time-code]", "input[name*=otp i]", "input[id*=otp i]", "input[name*=totp i]",
    "input[name*=verification i]", "input[name*=passcode i]", "input[placeholder*=otp i]",
    "input[placeholder*=code i]", "input[aria-label*=code i]"))
ERROR_WORDS = re.compile(r"\b(invalid|incorrect|wrong|failed|does not match|not match|unsuccessful|try again|"
                         r"not recogni[sz]ed|bad credentials)\b", re.I)
CAPTCHA_ERROR = re.compile(r"captcha|security code|image code|verification code|characters", re.I)
CAPTCHA_SUBMITS = 3  # sign-in submits at most, when only the captcha was wrong (matches the captcha retry cap)
WRONG_DETAILS = re.compile(r"password|user ?name|user ?id|credential|account", re.I)
FORM_WAIT_S = 8.0  # a sign-in form that is on the page but not shown yet (a late stylesheet) gets this long
EXPIRED_WORDS = re.compile(r"\b(session (has )?expired|please (log|sign) ?in again|timed out)\b", re.I)
# Hides what is typed in a box Delta filled, whatever the site does to it (a "show password" toggle).
MASK_CSS = ("input[type=password], [data-delta-secret] { -webkit-text-security: disc !important; "
            "text-security: disc !important; }")
MASK_JS = """(css) => {
  if (!document.getElementById('delta-secret-mask')) {
    const s = document.createElement('style'); s.id = 'delta-secret-mask'; s.textContent = css;
    (document.head || document.documentElement).appendChild(s);
  }
  const sel = 'input[type=password], input[autocomplete*=password], input[name*=pass i], input[id*=pass i]';
  for (const el of document.querySelectorAll(sel)) el.setAttribute('data-delta-secret', '1');
  return true;
}"""
HIDDEN_SIGNIN_JS = """() => [...document.querySelectorAll('input[type=password]')].some(e => {
  const f = e.form;
  const s = (f ? `${f.id} ${f.getAttribute('action') || ''} ${f.getAttribute('name') || ''}` : '')
    + ` ${e.name} ${e.id} ${location.pathname} ${location.search}`;
  return /log.?in|sign.?in|auth/i.test(s);
})"""
# The frame's own idea of where it is: checked right before typing, so a page that navigated a moment ago is caught.
LOCATION_JS = "() => location.href"

@dataclass
class CaptchaStep:
    """What happened with a simple captcha on the sign-in form."""

    entered: bool  # Delta typed an answer into the captcha box (read on this PC)
    note: str = ""  # why not, in plain words
    image: bytes | None = None  # the captcha picture (JPEG) when a person has to read it


Handoff = Callable[[str, str, str], Awaitable[bool]]  # (kind: otp|captcha|challenge, host, what to do) -> done
CaptchaHook = Callable[[Any, int], Awaitable[CaptchaStep]]  # (page, attempt 1..) -> what happened


@dataclass
class SignInResult:
    outcome: str
    message: str
    url: str = ""
    captcha_image: bytes | None = None  # needs_user for a simple captcha: the picture the user is asked to read

    @property
    def ok(self) -> bool:
        return self.outcome in ("signed_in", "already_signed_in")


def captcha_rejected(text: str) -> bool:
    """The page says the CAPTCHA was wrong, and says nothing about the password or user name being wrong. Only then is
    the saved password submitted again (a rejected password is never retried: no account lockouts)."""
    lines = [ln for ln in re.split(r"[\n.!]", text or "") if ERROR_WORDS.search(ln) or "required" in ln.lower()]
    return any(CAPTCHA_ERROR.search(ln) for ln in lines) and not any(WRONG_DETAILS.search(ln) for ln in lines)


async def mask_secret_fields(page: Any) -> None:
    """Mask every password box (and every box Delta typed a secret into) in the page and its frames. Called before
    a picture of Delta's browser is sent to a model, and after typing a password."""
    for frame in [page.main_frame, *[f for f in getattr(page, "frames", []) if f is not page.main_frame]]:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(frame.evaluate(MASK_JS, MASK_CSS), 2.0)


async def _visible(frame: Any, selector: str) -> Any | None:
    loc = frame.locator(selector)
    with contextlib.suppress(Exception):
        for i in range(min(await loc.count(), 8)):
            item = loc.nth(i)
            if await item.is_visible() and await item.is_editable():
                return item
    return None


async def _frame_origin(frame: Any) -> Origin | None:
    """Where the frame really is now, asked from inside it (not Playwright's cached URL)."""
    with contextlib.suppress(Exception):
        return origin_of(str(await asyncio.wait_for(frame.evaluate(LOCATION_JS), 3.0)))
    return origin_of(str(getattr(frame, "url", "")))


def _frames(page: Any) -> list[Any]:
    main = page.main_frame
    return [main, *[f for f in getattr(page, "frames", []) if f is not main]]


async def _text(page: Any) -> str:
    with contextlib.suppress(Exception):
        return str(await asyncio.wait_for(page.inner_text("body"), 5.0))[:4000]
    return ""


async def _challenge(page: Any) -> str | None:
    from delta.tools.captcha import PlaywrightCaptchaPage, challenge_kind

    with contextlib.suppress(Exception):
        return challenge_kind(await PlaywrightCaptchaPage(page).challenge_markers())
    return None


async def _simple_captcha(page: Any) -> bool:
    """A visible captcha box on the form. Its picture is found by the solver (by name, or the nearest img / canvas),
    so a captcha drawn on a bare <canvas> still counts and the form is never submitted with the box empty."""
    from delta.tools.captcha import AUTO_FIELD

    for frame in _frames(page):
        if await _visible(frame, AUTO_FIELD) is not None:
            return True
    return False


class SignInEngine:
    def __init__(self, handoff: Handoff, captcha: CaptchaHook | None = None, settle: Any = None,
                 nav_timeout_ms: int = 30_000) -> None:
        self.handoff = handoff
        self.captcha = captcha
        self.settle = settle
        self.nav_timeout_ms = nav_timeout_ms

    async def _settle(self, page: Any) -> None:
        if self.settle is not None:
            await self.settle(page)
        else:
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)

    async def _form(self, page: Any, saved: Origin) -> tuple[Any, Any | None, Any | None, str]:
        """(frame, user box, password box, refusal). Only frames on the saved origin are used; a password box in a
        frame from another origin is a refusal."""
        foreign = ""
        for frame in _frames(page):
            where = await _frame_origin(frame)
            pw = await _visible(frame, PASSWORD_SEL)
            if where != saved:
                if pw is not None:
                    foreign = str(where) if where else "an unknown page"
                continue
            user = await _visible(frame, USER_SEL)
            if user is None and pw is not None:  # the text box right before the password box
                user = await _visible(frame, "form:has(input[type=password]) input[type=text], "
                                             "form:has(input[type=password]) input:not([type])")
            if pw is not None or user is not None:
                return frame, user, pw, ""
        if foreign:
            return None, None, None, (f"the sign-in form is inside a frame from another site ({foreign}), not "
                                      f"{saved}. Delta only types a password on the exact saved site")
        return None, None, None, ""

    async def _type(self, page: Any, frame: Any, box: Any, value: str, saved: Origin, secret: bool) -> str:
        """Fill one box after checking, right now, that the tab and the frame are on the saved origin."""
        if why := mismatch(saved, page.url):
            return why
        if (await _frame_origin(frame)) != saved:
            return f"the form's frame is no longer on {saved}"
        if (await _frame_origin(page.main_frame)) != saved:
            return f"the tab is no longer on {saved}"
        await box.fill(value, timeout=8000)
        if secret:
            with contextlib.suppress(Exception):
                await box.evaluate("e => e.setAttribute('data-delta-secret', '1')")
            await mask_secret_fields(page)
        return ""

    async def sign_in(self, page: Any, login: Login) -> SignInResult:
        saved = login.site
        start = login.login_url or str(saved) + "/"
        # 1. where we are
        if mismatch(saved, page.url):
            if origin_of(page.url) is not None and await self._has_password(page):
                why = mismatch(saved, page.url)
                return SignInResult("refused", f"REFUSED: this tab asks for a password but {why}. Delta did not "
                                               f"type anything. If this is unexpected, it may be a phishing page.",
                                    page.url)
            try:
                await page.goto(start, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
            except Exception as e:
                return SignInResult("failed", f"could not open {start}: the site may be down "
                                              f"({type(e).__name__}: {str(e).splitlines()[0][:120]})", page.url)
            await self._settle(page)
            if why := mismatch(saved, page.url):
                return SignInResult("refused", f"REFUSED: opening {start} ended up somewhere else: {why}. If the site "
                                               "now signs in on another address, save a login for that address.",
                                    page.url)
        # 2. challenges built to stop programs
        if kind := await _challenge(page):
            if not await self.handoff("challenge", saved.host, f"{kind} on {saved.host}: complete it in Delta's "
                                                                 "browser on your PC, then Approve"):
                return SignInResult("needs_user", f"NEEDS HUMAN: {saved.host} shows {kind}, which Delta never "
                                                  "solves. The user did not complete it.", page.url)
            await self._settle(page)
        frame, user, pw, refusal = await self._form_soon(page, saved)
        if refusal:
            return SignInResult("refused", "REFUSED: " + refusal, page.url)
        if frame is None:
            if login.login_url and page.url.rstrip("/") != login.login_url.rstrip("/"):
                with contextlib.suppress(Exception):
                    await page.goto(login.login_url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
                    await self._settle(page)
                frame, user, pw, refusal = await self._form_soon(page, saved)
                if refusal:
                    return SignInResult("refused", "REFUSED: " + refusal, page.url)
            if frame is None and await self._hidden_password(page):
                return SignInResult("no_form", f"the sign-in form on {saved} is on the page but stayed hidden for "
                                               f"{FORM_WAIT_S:.0f} s (the site may still be loading). Nothing was "
                                               "typed; try again in a moment.", page.url)
            if frame is None:
                return SignInResult("already_signed_in", f"no sign-in form on {saved}: already signed in (the site "
                                                         "kept the session)", page.url)
        # 3. a user-name-only first step (the password comes on the next page)
        if pw is None and user is not None:
            if not login.username:
                return SignInResult("no_form", f"{saved.host} asks for a user name, but the saved login "
                                               f"'{login.name}' has no user name. Nothing was typed. Save it again "
                                               "with the user name in Settings > Logins (Replace).", page.url)
            if why := await self._type(page, frame, user, login.username, saved, secret=False):
                return SignInResult("refused", "REFUSED: " + why, page.url)
            await user.press("Enter")
            await self._settle(page)
            frame, user, pw, refusal = await self._form_soon(page, saved)
            if refusal:
                return SignInResult("refused", "REFUSED: " + refusal, page.url)
            if pw is None:
                return SignInResult("no_form", f"entered the user name on {saved.host} but no password box came "
                                               f"(now at {page.url}). Nothing else was typed.", page.url)
            user = None  # already entered
        assert pw is not None and frame is not None
        if not login.password:
            return SignInResult("no_form", f"{saved.host} asks for a password, but the saved login '{login.name}' has "
                                           "no password saved (only a user name). Nothing was typed. Save it again "
                                           "with the password in Settings > Logins (Replace).", page.url)
        result = SignInResult("failed", "not submitted", page.url)
        for attempt in range(1, CAPTCHA_SUBMITS + 2):  # the last round only fetches the picture for the user
            if user is not None and login.username and (
                    why := await self._type(page, frame, user, login.username, saved, secret=False)):
                return SignInResult("refused", "REFUSED: " + why, page.url)
            if why := await self._type(page, frame, pw, login.password, saved, secret=True):
                return SignInResult("refused", "REFUSED: " + why, page.url)
            had_captcha = await _simple_captcha(page)
            if had_captcha:  # a simple captcha on the form itself: read on this PC where allowed, else the user
                if self.captcha is None:
                    return SignInResult("needs_user", f"NEEDS HUMAN: the sign-in form on {saved.host} has a captcha "
                                                      "and captcha help is not set up.", page.url)
                step = await self.captcha(page, attempt)
                if not step.entered:
                    return SignInResult("needs_user", f"CAPTCHA: {step.note}", page.url, captcha_image=step.image)
            elif attempt > 1:
                return result  # the captcha went away; the last answer stands
            before = page.url
            try:
                await pw.press("Enter")
            except Exception as e:
                return SignInResult("failed", f"could not submit the form ({type(e).__name__})", page.url)
            await self._settle(page)
            result = await self._after(page, login, saved, before)
            if not (had_captcha and await self._captcha_wrong(page, result)):
                return result
            # the site rejected the captcha, not the password: the form comes back, fill it again (bounded)
            frame, user, pw, refusal = await self._form_soon(page, saved)
            if refusal or frame is None or pw is None:
                return SignInResult("refused", "REFUSED: " + refusal, page.url) if refusal else result
        return SignInResult("needs_user", f"NEEDS HUMAN: {saved.host} kept rejecting the captcha; Delta stopped "
                                          f"after {CAPTCHA_SUBMITS} tries.", page.url)

    async def _captcha_wrong(self, page: Any, result: SignInResult) -> bool:
        """After submitting with a captcha Delta read: the site turned the CAPTCHA down. Either it says so (and says
        nothing about the password or user name, see captcha_rejected), or it just showed the sign-in form again
        with no error at all, which captcha-only sites do (a new picture, the boxes emptied). A wrong password is
        never taken for a wrong captcha: that page names the password or says the details are wrong, and is never
        retried (no account lockouts)."""
        if result.outcome not in ("wrong_password", "failed"):
            return False
        text = await _text(page)
        if captcha_rejected(text):
            return True
        return result.outcome == "failed" and not ERROR_WORDS.search(text) and await _simple_captcha(page)

    async def answer_captcha(self, page: Any, login: Login, answer: str) -> SignInResult:
        """Finish a sign-in that waited for the user to read the captcha: the same checks as sign_in (the tab and
        the form's frame on the exact saved origin), the user name and password filled again (sites often clear the
        password when the page reloads), the user's answer in the captcha box, one submit, then the same check that
        the sign-in worked. A captcha the site rejects comes back with the new picture for the user."""
        saved = login.site
        if why := mismatch(saved, page.url):
            return SignInResult("refused", f"REFUSED: the sign-in tab is no longer on the saved site: {why}. Nothing "
                                           "was typed.", page.url)
        frame, user, pw, refusal = await self._form_soon(page, saved)
        if refusal:
            return SignInResult("refused", "REFUSED: " + refusal, page.url)
        if frame is None or pw is None:
            if not await self._has_password(page):
                return SignInResult("already_signed_in", f"no sign-in form on {saved} any more: already signed in",
                                    page.url)
            return SignInResult("no_form", f"the sign-in form on {saved.host} is gone; ask again to start over.",
                                page.url)
        from delta.tools.captcha import AUTO_FIELD

        box = await _visible(frame, AUTO_FIELD)
        if box is None:
            return SignInResult("no_form", f"the captcha box on {saved.host} is gone; ask again to start over.",
                                page.url)
        if user is not None and login.username and (
                why := await self._type(page, frame, user, login.username, saved, secret=False)):
            return SignInResult("refused", "REFUSED: " + why, page.url)
        if why := await self._type(page, frame, pw, login.password, saved, secret=True):
            return SignInResult("refused", "REFUSED: " + why, page.url)
        if why := await self._type(page, frame, box, answer, saved, secret=False):
            return SignInResult("refused", "REFUSED: " + why, page.url)
        before = page.url
        try:
            await pw.press("Enter")
        except Exception as e:
            return SignInResult("failed", f"could not submit the form ({type(e).__name__})", page.url)
        await self._settle(page)
        result = await self._after(page, login, saved, before)
        if result.outcome in ("wrong_password", "failed") and captcha_rejected(await _text(page)):
            from delta.tools.captcha import PlaywrightCaptchaPage

            image = None
            cpage = PlaywrightCaptchaPage(page)
            with contextlib.suppress(Exception):
                image = await cpage.screenshot("") or await cpage.region_shot()
            return SignInResult("needs_user", f"CAPTCHA: {saved.host} said the characters were wrong; here is the "
                                              "new picture.", page.url, captcha_image=image)
        return result

    async def _form_soon(self, page: Any, saved: Origin) -> tuple[Any, Any | None, Any | None, str]:
        """_form, but a sign-in form that is already on the page and not shown yet (many ERPs hide the whole page
        until a stylesheet arrives) gets up to FORM_WAIT_S to appear, instead of being taken for 'signed in'."""
        found = await self._form(page, saved)
        if found[0] is not None or found[3]:
            return found
        loop = asyncio.get_running_loop()
        end = loop.time() + FORM_WAIT_S
        while loop.time() < end and await self._hidden_password(page):
            await asyncio.sleep(0.4)
            found = await self._form(page, saved)
            if found[0] is not None or found[3]:
                return found
        return found

    async def _hidden_password(self, page: Any) -> bool:
        """A SIGN-IN password box is in the page (any frame) even though none is visible yet: its form, the box itself
        or the address says log in / sign in (a hidden "change password" box on a signed-in page does not count)."""
        for frame in _frames(page):
            with contextlib.suppress(Exception):
                if await asyncio.wait_for(frame.evaluate(HIDDEN_SIGNIN_JS), 3.0):
                    return True
        return False

    async def _after(self, page: Any, login: Login, saved: Origin, before: str) -> SignInResult:
        for _ in range(2):  # an OTP step, then whatever comes after it
            if mismatch(saved, page.url) and origin_of(page.url) is not None and not await self._has_password(page):
                return SignInResult("signed_in", f"signed in to {saved.host}; the site moved on to {page.url}",
                                    page.url)
            if await self._otp(page, saved):
                done = await self.handoff("otp", saved.host, f"{saved.host} asks for a one-time code or second "
                                                             "step: enter it in Delta's browser on your PC, then "
                                                             "Approve")
                if not done:
                    return SignInResult("needs_user", f"NEEDS HUMAN: {saved.host} wants a one-time code; the user did "
                                                      "not complete it in time.", page.url)
                for _ in range(30):  # the code was entered: give the site time to move on
                    await self._settle(page)
                    if not await self._otp(page, saved):
                        break
                    await asyncio.sleep(0.5)
                else:
                    return SignInResult("needs_user", f"NEEDS HUMAN: {saved.host} still asks for the one-time code.",
                                        page.url)
                continue
            break
        if kind := await _challenge(page):
            return SignInResult("needs_user", f"NEEDS HUMAN: after signing in, {saved.host} shows {kind}.", page.url)
        if await self._has_password(page):
            text = await _text(page)
            if ERROR_WORDS.search(text):
                return SignInResult("wrong_password", f"{saved.host} did not accept the saved login (it says the "
                                                      "details are wrong). Delta will not retry: update it with "
                                                      "`delta logins add --replace` or in Settings > Logins.", page.url)
            return SignInResult("failed", f"the sign-in form on {saved.host} is still showing after submitting; "
                                          "it may need something else (read the page).", page.url)
        return SignInResult("signed_in", f"signed in to {saved.host} (now at {page.url})", page.url)

    async def _has_password(self, page: Any) -> bool:
        for frame in _frames(page):
            if await _visible(frame, PASSWORD_SEL) is not None:
                return True
        return False

    async def _otp(self, page: Any, saved: Origin) -> bool:
        for frame in _frames(page):
            if await _visible(frame, OTP_SEL) is not None:
                return True
        return False


def looks_signed_out(text: str, has_password_box: bool) -> bool:
    """A page that says the session ended, or shows a sign-in form, means the site signed Delta out."""
    return has_password_box or bool(EXPIRED_WORDS.search(text or ""))
