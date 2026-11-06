"""Opt-in help with simple image captchas on the user's OWN accounts (e.g. a university portal).

Off by default. How it decides, for the site open in Delta's browser:

1. reCAPTCHA, hCaptcha and Cloudflare Turnstile are refused outright: they exist to tell people
   from programs, so Delta never tries to get past them and hands the page back to the user.
2. The site's host is in `captcha.allowed_sites` (set by the user with `delta captcha allow <host>`,
   never by Delta itself: its settings file is a protected path): the captcha image is read on this PC
   with Windows OCR first and, only when the user lets pictures go to a model (attachments.vision is not
   "off"), by the vision model, then typed into the field. At most MAX_AUTO_ATTEMPTS (3) times per site in
   ATTEMPT_WINDOW_S; from the second attempt on the captcha is refreshed first (a new picture), never more.
3. Otherwise, or once those attempts are used up: human handoff. Delta saves the captcha image to the
   run's proof folder and pauses on an approval ("type the characters into the page on your PC, then
   Approve"), sent to every approval channel (dashboard, Telegram, terminal). Approve = typed it,
   Deny or no answer = stop. The answer never leaves the PC and is never seen by Delta.

Every attempt is written to the audit log (site, mode, success). The answer text never is, and it is
never returned to the model either.
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
from delta.tools.base import Risk, ToolResult
from delta.tools.browser import BrowserManager, _BrowserTool

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
              "canvas[id*=captcha i]")
# A "new picture" control next to the captcha. Clicking the picture itself refreshes many simple captchas too.
REFRESH = ("[id*=captcha i][id*=refresh i], [class*=captcha i][class*=refresh i], [id*=refresh i], "
           "[class*=refresh i], [title*=refresh i], [aria-label*=refresh i], [alt*=refresh i]")
AUTO_FIELD = ("input[name*=captcha i], input[id*=captcha i], input[placeholder*=captcha i], "
              "input[aria-label*=captcha i]")


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


def clean_answer(reply: str) -> str | None:
    """The characters a vision model read, or None when its reply does not look like a captcha answer."""
    token = re.sub(r"[\s`'\"]", "", reply.strip().splitlines()[0] if reply.strip() else "")
    return token if ANSWER.match(token) else None


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
            return await self._first(self.page.locator(AUTO_IMAGE))
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
        el = await self._image(image)
        return None if el is None else bytes(await el.screenshot(type="jpeg", quality=90, timeout=8000))

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

Vision = Callable[[str], Awaitable[str]]  # base64 jpeg -> model reply
Reader = Callable[[bytes], Awaitable[str]]  # jpeg bytes -> text read on this PC (Windows OCR)
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

    def __post_init__(self) -> None:
        self._attempts: dict[str, tuple[int, float]] = {}  # host -> (auto attempts, first attempt time)

    def _used(self, host: str) -> int:
        count, since = self._attempts.get(host, (0, 0.0))
        return count if self.clock() - since < ATTEMPT_WINDOW_S else 0

    def _count(self, host: str) -> int:
        used = self._used(host)
        since = self._attempts[host][1] if used else self.clock()
        self._attempts[host] = (used + 1, since)
        return used + 1

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

        if not site_allowed(host, self.allowed_sites()):
            reason = "site not in captcha.allowed_sites"
        elif self.vision is None and self.ocr is None:
            reason = "no way to read it on this PC (no Windows OCR, and pictures may not go to a model)"
        else:
            reason = "automatic attempts used up"
            while self._used(host) < MAX_AUTO_ATTEMPTS:
                attempt = self._count(host)
                if attempt > 1:  # the last picture was used: ask the site for a new one before reading again
                    with contextlib.suppress(Exception):
                        if await page.refresh(image) and (new := await page.screenshot(image)):
                            shot, b64 = new, base64.b64encode(new).decode()
                answer: str | None = None
                if self.ocr is not None:
                    with contextlib.suppress(Exception):  # OCR missing or unreadable: the model may still read it
                        answer = clean_answer(await self.ocr(shot))
                if not answer and self.vision is not None:
                    with contextlib.suppress(Exception):  # no model, model error: counts as a failed read
                        answer = clean_answer(await self.vision(b64))
                if answer:
                    await page.fill(field, answer)
                self._audit(host, "auto", bool(answer), attempt=attempt)
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
    async def read(b64: str) -> str:
        resp = await router.complete([{"role": "user", "content": [
            {"type": "text", "text": "This image is a text captcha. Reply with ONLY its characters, "
                                     "exactly as shown, no spaces, no other words."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}], task="vision")
        return str(resp.content)

    return read


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
    """Windows OCR as a captcha reader (None off Windows or when OCR is switched off)."""
    from delta.perception.ocr import WindowsOcr

    reader = WindowsOcr(mode)
    if not reader.platform_ok() or mode == "off":
        return None

    async def read(jpeg: bytes) -> str:
        import io

        from PIL import Image

        src = Image.open(io.BytesIO(jpeg))
        big = src.convert("L").resize((src.width * 3, src.height * 3))  # small captchas read better enlarged
        lines = await reader.read(big)
        return "".join(line.text for line in lines)

    return read


def pictures_allowed(settings: Any) -> bool:
    """The user lets pictures go to a model (attachments.vision is not "off")."""
    att = getattr(settings, "attachments", None)
    return str(getattr(att, "vision", "auto") or "auto").lower() != "off"


def make_solver(settings: Any, router: Any, approvals: ApprovalBroker, audit: AuditLog,
                proof: Any = None) -> CaptchaSolver:
    """The captcha solver wired to Delta's settings: local OCR first, the vision model only when pictures are on."""
    return CaptchaSolver(
        allowed_sites=live_allowlist(settings.data_dir, settings.captcha.allowed_sites),
        vision=router_vision(router) if pictures_allowed(settings) else None,
        ask_human=approval_handoff(approvals), audit=audit,
        save_image=proof.save_image if proof else (lambda b64, label: None),
        ocr=local_ocr(getattr(getattr(settings, "perception", None), "ocr", "auto")))


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
