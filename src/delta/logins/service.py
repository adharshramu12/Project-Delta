"""Using a saved login: the permission check, the hand-offs to the user and the audit trail around SignInEngine.

Permission, per login (set only by the user, in Settings > Logins or `delta logins allow`):

  ask      (default) every use asks first, on every approval channel (dashboard, Telegram, terminal). The prompt
           offers "Allow for this task", which covers the rest of that one task only (kept in memory, never saved).
  always   used without asking, on that exact site only.

Approving a sign-in from a phone needs the remote PIN (it is an "external" action, delta.safety.remote_guard), and a
request that came from Telegram can call sign_in only after /unlock (sign_in is a REMOTE_PIN_TOOLS entry). While
remote control is locked nothing remote signs in at all.

Every use is written to the audit log: the login's name, its site, the task, which browser and the outcome. Never
the user name, the password, a one-time code or a captcha answer.

A simple text captcha on the sign-in form: read automatically, with no approval, where the user allows it: the
login's "This site has a simple text captcha, let Delta read it" switch (ON by default for every login with a password;
the user can turn it off per login) or `delta captcha allow <host>`. Windows OCR on this PC first, one small vision
call per try only when OCR fails and a configured model takes pictures, at most 3 tries (delta.tools.captcha).
Otherwise, or after that, Delta PARKS the sign-in (the filled form stays open in the same tab) and sends the
captcha picture to the chat the request came from (the dashboard, or Telegram as a photo). The user replies with just
the characters; LoginFlow (delta.logins.flow) hands that reply to answer_captcha(), which checks the tab and the
origin again, types it, submits once and checks the result. The answer is never logged or audited. A parked sign-in
expires after PARK_TTL_S and allows at most HUMAN_TRIES answers. reCAPTCHA, hCaptcha and Turnstile are never solved.

The user's OWN browser (their everyday Chrome or Edge, delta.logins.own_browser) has one more question: the first
time a login is filled there, Delta asks for an explicit yes that names the browser and the site, even when the login
is "Always allow". The yes is remembered for that site and that browser (LoginStore.allow_own_browser); "Ask every
time" in Settings withdraws it. It is a one-time consent, so it has no "Allow for this task" button, and from a phone
it needs the PIN like any sign-in approval.
"""

from __future__ import annotations

import base64
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from delta.core.state import CURRENT_REQUEST
from delta.logins.own_browser import OwnBrowserDriver, OwnBrowserSignIn, TabBinding
from delta.logins.signin import CaptchaStep, SignInEngine, SignInResult
from delta.logins.store import Login, LoginStore
from delta.safety.approvals import ApprovalBroker, ApprovalRequest
from delta.safety.remote_guard import is_remote
from delta.system.logging import log

OWN_CONSENT_TOOL = "sign_in_own_browser"  # the approval asked once per site and browser before the first own fill
PARK_TTL_S = 10 * 60  # a sign-in waiting for the user's captcha answer is dropped after this
HUMAN_TRIES = 3  # captcha answers from the user per parked sign-in


@dataclass
class Parked:
    """A sign-in waiting for the user to type the captcha they were sent. The form stays filled in its tab."""

    login_id: str
    key: str  # the chat it was asked from: only an answer from that chat continues it
    browser: str  # private | own:chrome | own:edge
    page: Any  # the Playwright page (private) or the own-browser driver
    bound: TabBinding | None  # own browser: the tab the sign-in is bound to
    host: str
    task_id: str
    tries: int = 0
    since: float = 0.0
    image: bytes | None = None  # the captcha picture sent, shown again when the dashboard reloads the chat


def chat_key() -> str:
    req = CURRENT_REQUEST.get()
    if req is None:
        return ""
    return req.chat_id or f"{req.channel}:{req.user}"


class LoginService:
    def __init__(self, store: LoginStore, audit: Any, approvals: ApprovalBroker | None,
                 captcha_solver: Any = None, guard: Any = None, proof: Any = None,
                 channels: dict[str, Any] | None = None, settle: Any = None) -> None:
        self.store = store
        self.audit = audit
        self.approvals = approvals
        self.solver = captcha_solver
        self.guard = guard
        self.proof = proof
        self.channels = channels if channels is not None else {}
        self.settle = settle
        self._task_grants: dict[str, set[str]] = {}  # task id -> login ids allowed for the rest of that task
        self.on_used: Callable[[Login, str], None] | None = None
        self.parked: dict[str, Parked] = {}  # chat key -> the sign-in waiting for that chat's captcha answer
        self.last_image: dict[str, bytes] = {}  # chat key -> the captcha picture just sent (the dashboard shows it)
        self.clock: Callable[[], float] = time.monotonic
        self._sending: Any = None  # the photo of a captcha on its way to a phone channel

    # --- permission ------------------------------------------------------------------------------------
    def forget_task(self, task_id: str) -> None:
        self._task_grants.pop(task_id, None)

    async def allowed(self, login: Login, task_id: str = "", why: str = "") -> tuple[bool, str]:
        """(may use it now, how it was decided). 'always' needs no answer; otherwise a person decides."""
        if self._remote_locked():
            return False, "remote control is locked"
        if login.permission == "always":
            return True, "always allowed for this site"
        if task_id and login.id in self._task_grants.get(task_id, set()):
            return True, "allowed for this task"
        if self.approvals is None:
            return False, "no one to ask"
        outcome = await self.approvals.request(ApprovalRequest(
            tool="sign_in", risk="external", task_id=task_id, for_task=bool(task_id),
            summary=f"Sign in to {login.name} ({login.origin}) with your saved login" + (f" ({why})" if why else ""),
            reason="saved logins ask before each use (Settings > Logins can change that for this site)"))
        if outcome.approved and outcome.for_task and task_id:
            self._task_grants.setdefault(task_id, set()).add(login.id)
        if not outcome.approved:
            return False, f"not approved ({outcome.by})"
        return True, "allowed for this task" if outcome.for_task else f"approved by {outcome.by}"

    def _remote_locked(self) -> bool:
        req = CURRENT_REQUEST.get()
        return bool(self.guard is not None and req is not None and is_remote(req.channel) and self.guard.locked)

    async def own_browser_consent(self, login: Login, browser: str, label: str, task_id: str = "") -> tuple[bool, str]:
        """(may fill it in the user's own browser, how it was decided). Asked once per site and browser."""
        if browser in login.own_browser:
            return True, ""
        if self._remote_locked():
            return False, "remote control is locked"
        if self.approvals is None:
            return False, "no one to ask"
        outcome = await self.approvals.request(ApprovalRequest(
            tool=OWN_CONSENT_TOOL, risk="external", task_id=task_id, for_task=False,
            summary=(f"First time in your own browser: let Delta type your saved login for {login.name} "
                     f"({login.origin}) into {label}, your everyday browser with your real tabs, sessions and "
                     "extensions?"),
            reason=(f"asked once per site in {label}, even when the login is 'Always allow'. Delta then remembers "
                    "it for this site; 'Ask every time' in Settings > Logins withdraws it")))
        self.audit.record("login.own_browser_consent", login=login.id, site=login.origin, browser=browser,
                          given=outcome.approved, by=outcome.by)
        if not outcome.approved:
            return False, f"not approved ({outcome.by})"
        self.store.allow_own_browser(login.id, browser)
        login.own_browser[browser] = "now"
        return True, f"own-browser consent given by {outcome.by}"

    # --- hand-offs (one-time codes, captchas the user solves) -------------------------------------------
    async def handoff(self, kind: str, host: str, text: str) -> bool:
        """Ask the user to finish a step on the PC (Approve = done, Deny or no answer = stop). Waits as long as any
        approval (agent.approval_timeout_s)."""
        if self.approvals is None:
            return False
        req = CURRENT_REQUEST.get()
        await self._send_picture(kind, host)
        outcome = await self.approvals.request(ApprovalRequest(
            tool="sign_in_handoff", risk="act", summary=text, reason=f"{host} needs a person ({kind})",
            task_id=req.id if req else ""))
        self.audit.record("login.handoff", site=host, kind=kind, done=outcome.approved, by=outcome.by)
        return outcome.approved

    async def _send_picture(self, kind: str, host: str) -> None:
        """A picture of the page (password boxes masked) to the phone the request came from, so the user knows what
        is waiting on the PC. The dashboard shows the approval card; the picture is also kept with the run's proof."""
        page = getattr(self, "_page", None)
        if page is None:
            return
        try:
            from delta.logins.signin import mask_secret_fields

            await mask_secret_fields(page)
            png = await page.screenshot(type="jpeg", quality=70, timeout=8000)
        except Exception:
            return
        import base64

        saved = ""
        if self.proof is not None:
            with contextlib.suppress(Exception):
                saved = self.proof.save_image(base64.b64encode(png).decode(), f"{kind}-{host}") or ""
        req = CURRENT_REQUEST.get()
        folder = getattr(self.proof, "dir", None)
        if req is None or not is_remote(req.channel) or not saved or folder is None:
            return
        ch = self.channels.get(req.channel)
        send = getattr(ch, "send_file", None)
        if callable(send):
            from pathlib import Path

            with contextlib.suppress(Exception):
                await send(req.user, Path(folder) / saved, f"{host} is waiting for you on the PC ({kind}).")

    async def _captcha(self, page: Any, attempt: int = 1) -> CaptchaStep:
        """A simple captcha on the form in Delta's private browser: read on this PC where the user allowed it
        (delta.tools.captcha.CaptchaSolver.auto_fill), else the picture for the user to read."""
        from delta.tools.captcha import PlaywrightCaptchaPage

        return await self._captcha_on(PlaywrightCaptchaPage(page), attempt)

    async def _captcha_on(self, cpage: Any, attempt: int) -> CaptchaStep:
        if self.solver is None:
            image = None
            with contextlib.suppress(Exception):
                image = await cpage.screenshot("")
            return CaptchaStep(False, "captcha help is not set up on this PC", image)
        auto = getattr(self.solver, "auto_fill", None)
        if not callable(auto):
            return CaptchaStep(False, "captcha help is not set up on this PC")
        if attempt > 3:  # the sign-in's last round: the site rejected every answer read here
            image = None
            with contextlib.suppress(Exception):
                image = await cpage.screenshot("")
            return CaptchaStep(False, "the site rejected the characters Delta read 3 times", image)
        entered, note, image = await auto(cpage)
        if not entered and image is None and callable(region := getattr(cpage, "region_shot", None)):
            with contextlib.suppress(Exception):
                image = await region()  # no picture element: the user still gets one (never read by OCR/a model)
        return CaptchaStep(entered, note, image)

    # --- a sign-in waiting for the user's captcha answer ------------------------------------------------
    def _park(self, login: Login, result: SignInResult, page: Any, browser: str, task_id: str,
              bound: TabBinding | None = None, tries: int = 0) -> SignInResult:
        """Keep the filled form open for the user's answer and send them the picture. Without a chat to answer
        from (the dashboard's Test button, a watcher) nothing is parked."""
        key = chat_key()
        image = result.captcha_image
        why = result.message.removeprefix("CAPTCHA: ").strip().rstrip(".")
        allowed = getattr(self.solver, "allowed", None)
        hint = "" if callable(allowed) and allowed(result.url or login.origin) else (
            f" To let Delta read this site's captcha itself, turn on 'This site has a simple text captcha' for "
            f"'{login.name}' in Settings > Logins.")
        if not key or not image:
            where = "in your own browser" if browser.startswith("own") else "in Delta's browser"
            req = CURRENT_REQUEST.get()
            if req is not None and is_remote(req.channel):  # away from the PC: never "type it in the browser"
                return SignInResult("needs_user", (
                    f"NEEDS HUMAN: {login.site.host} shows a simple captcha: {why}. Delta could not take a picture of "
                    "it to send you, so nothing was submitted. Ask again in a minute (a new picture often reads), or "
                    "sign in when you are back at the PC." + hint), result.url)
            return SignInResult("needs_user", (
                f"NEEDS HUMAN: {login.site.host} shows a simple captcha: {why}. Ask for the sign-in from the chat "
                f"(dashboard or Telegram) and Delta will send you the picture to read, or type it {where} on the PC."
                + hint), result.url)
        self.parked[key] = Parked(login.id, key, browser, page, bound, login.site.host, task_id, tries, self.clock(),
                                  image)
        self.last_image[key] = image
        self._send_captcha(image, login)
        self.audit.record("login.captcha_asked", login=login.id, site=login.origin, browser=browser, tries=tries)
        return SignInResult("needs_user", (
            f"CAPTCHA: {login.site.host} shows a simple captcha: {why}. Your user name and password are filled in. "
            "Reply with just the characters in the picture and Delta will finish signing in (or reply 'cancel')."
            + hint), result.url, captcha_image=image)

    def _send_captcha(self, image: bytes, login: Login) -> None:
        """The picture goes with the answer on the dashboard (LoginFlow) and as a photo to a phone channel."""
        saved = ""
        if self.proof is not None:
            with contextlib.suppress(Exception):
                saved = self.proof.save_image(base64.b64encode(image).decode(), f"captcha-{login.site.host}") or ""
        req = CURRENT_REQUEST.get()
        folder = getattr(self.proof, "dir", None)
        if req is None or not is_remote(req.channel) or not saved or folder is None:
            return
        send = getattr(self.channels.get(req.channel), "send_file", None)
        if callable(send):
            import asyncio
            from pathlib import Path

            async def go() -> None:
                with contextlib.suppress(Exception):
                    await send(req.user, Path(folder) / saved, f"Captcha for {login.name}: reply with just these "
                                                               "characters (or 'cancel').")

            self._sending = asyncio.ensure_future(go())  # keep a reference until it is sent

    def waiting(self, key: str) -> Parked | None:
        """The sign-in parked for this chat, if it has not expired."""
        p = self.parked.get(key)
        if p is not None and self.clock() - p.since > PARK_TTL_S:
            self.parked.pop(key, None)
            p = None
        return p

    def waiting_view(self, key: str) -> dict[str, Any] | None:
        """What the dashboard shows again after a reload: the captcha picture still waiting for this chat's reply."""
        p = self.waiting(key)
        if p is None or not p.image:
            return None
        login = self.store.get(p.login_id)
        return {"login": login.name if login else p.host, "site": p.host, "image": base64.b64encode(p.image).decode(),
                "expires_in_s": max(0, int(PARK_TTL_S - (self.clock() - p.since)))}

    def cancel(self, key: str) -> Parked | None:
        self.last_image.pop(key, None)
        return self.parked.pop(key, None)

    async def answer_captcha(self, key: str, answer: str, task_id: str = "") -> SignInResult:
        """Continue the parked sign-in of chat `key` with the characters the user typed. Never logged."""
        parked = self.waiting(key)
        if parked is None:
            return SignInResult("failed", "No sign-in is waiting for a captcha any more (it expires after "
                                          f"{PARK_TTL_S // 60} minutes). Ask again to sign in.")
        self.parked.pop(key, None)
        self.last_image.pop(key, None)
        login = self.store.get(parked.login_id)
        if login is None:
            return SignInResult("failed", "That saved login was removed, so Delta stopped.")
        try:
            if parked.browser == "private":
                engine = SignInEngine(self.handoff, self._captcha, self.settle)
                result = await engine.answer_captcha(parked.page, login, answer)
            else:
                result = await OwnBrowserSignIn(self.handoff).answer_captcha(parked.page, parked.bound, login, answer)
        except Exception as e:
            log.warning("login.captcha_answer_failed", site=login.origin, error=type(e).__name__)
            result = SignInResult("failed", f"signing in to {login.name} failed ({type(e).__name__})")
        self.audit.record("captcha", site=login.site.host, mode="human_reply", ok=result.ok)  # never the answer
        if result.outcome == "needs_user" and result.captcha_image is not None:
            if parked.tries + 1 >= HUMAN_TRIES:
                result = SignInResult("needs_user", f"NEEDS HUMAN: {login.site.host} rejected the captcha "
                                                    f"{HUMAN_TRIES} times, so Delta stopped. Ask again to start over.",
                                      result.url)
            else:
                result = self._park(login, result, parked.page, parked.browser, task_id or parked.task_id,
                                    parked.bound, parked.tries + 1)
        self.store.touch(login.id, result.outcome)
        self._audit(login, task_id or parked.task_id, result.outcome, "captcha answered by the user", parked.browser)
        if self.on_used is not None:
            with contextlib.suppress(Exception):
                self.on_used(login, result.outcome)
        return result

    # --- the whole thing --------------------------------------------------------------------------------
    def _find(self, ref: str) -> tuple[Login | None, SignInResult | None]:
        login, why = self.store.resolve(ref)
        if login is None:
            return None, SignInResult("failed", f"{why}. Nothing was typed. The user adds or updates logins on the "
                                                "PC: Settings > Logins, or `delta logins add`.")
        return login, None

    async def sign_in(self, ref: str, page: Any, task_id: str = "", why: str = "",
                      ask: bool = True) -> SignInResult:
        """Sign in with the saved login `ref` (its name, id or site) in `page`. Audited, whatever happens."""
        login, missing = self._find(ref)
        if login is None:
            return missing or SignInResult("failed", "no such login")
        if ask:
            ok, how = await self.allowed(login, task_id, why)
            if not ok:
                self._audit(login, task_id, "not_allowed", how)
                return SignInResult("needs_user", f"The user did not allow using the saved login for {login.name} "
                                                  f"({how}). Do not try another way.")
        else:
            how = "already decided"
        self._page = page
        self.cancel(chat_key())  # a new sign-in replaces one still waiting for a captcha in this chat
        try:
            engine = SignInEngine(self.handoff, self._captcha, self.settle)
            result = await engine.sign_in(page, login)
        except Exception as e:
            log.warning("login.failed", site=login.origin, error=type(e).__name__)
            result = SignInResult("failed", f"signing in to {login.name} failed ({type(e).__name__})")
        finally:
            self._page = None
        if result.outcome == "needs_user" and result.message.startswith("CAPTCHA:"):
            result = self._park(login, result, page, "private", task_id)
        self.store.touch(login.id, result.outcome)
        self._audit(login, task_id, result.outcome, how)
        if self.on_used is not None:
            with contextlib.suppress(Exception):
                self.on_used(login, result.outcome)
        return result

    async def sign_in_own(self, ref: str, driver: OwnBrowserDriver, task_id: str = "", why: str = "") -> SignInResult:
        """Sign in with the saved login `ref` in the user's OWN browser through `driver`. Same permission and audit
        as sign_in, plus the one-time own-browser consent (asked first, even for "always")."""
        login, missing = self._find(ref)
        if login is None:
            return missing or SignInResult("failed", "no such login")
        browser = f"own:{driver.browser_key}"
        ok, how = await self.own_browser_consent(login, driver.browser_key, driver.browser_label, task_id)
        if not ok:
            self._audit(login, task_id, "not_allowed", how, browser)
            fix = ("Unlock remote control with /unlock first." if how == "remote control is locked" else
                   f"Approve the one-time question (dashboard, or Telegram with your PIN) to let Delta type "
                   f"'{login.name}' into your own {driver.browser_label} for this site, or switch to Delta's private "
                   "browser (Settings > Browser).")
            return SignInResult("needs_user", f"NEEDS HUMAN: Delta has no OK yet to type the saved login for "
                                              f"{login.name} into your own {driver.browser_label} ({how}). Nothing was "
                                              f"typed. {fix} Do not try another way.")
        if not how:  # consent given before: the login's own permission decides, as in the private browser
            ok, how = await self.allowed(login, task_id, why)
            if not ok:
                self._audit(login, task_id, "not_allowed", how, browser)
                return SignInResult("needs_user", f"The user did not allow using the saved login for {login.name} "
                                                  f"({how}). Do not try another way.")
        # else: the consent question just answered names this site and browser, so it covers this use too
        self.cancel(chat_key())
        req = CURRENT_REQUEST.get()
        own = OwnBrowserSignIn(self.handoff, captcha=self._captcha_on,
                               remote=bool(req is not None and is_remote(req.channel)))
        try:
            result = await own.sign_in(driver, login)
        except Exception as e:
            log.warning("login.failed", site=login.origin, browser=browser, error=type(e).__name__)
            result = SignInResult("failed", f"signing in to {login.name} in your {driver.browser_label} failed "
                                            f"({type(e).__name__})")
        if result.outcome == "needs_user" and result.message.startswith("CAPTCHA:"):
            result = self._park(login, result, driver, browser, task_id, own.bound)
        self.store.touch(login.id, result.outcome)
        self._audit(login, task_id, result.outcome, how, browser)
        if self.on_used is not None:
            with contextlib.suppress(Exception):
                self.on_used(login, result.outcome)
        return result

    def _audit(self, login: Login, task_id: str, outcome: str, how: str, browser: str = "private") -> None:
        req = CURRENT_REQUEST.get()
        self.audit.record("login.use", login=login.id, site=login.origin, task=task_id, outcome=outcome,
                          permission=how, browser=browser, channel=req.channel if req else "")
