"""Writing an email in Gmail in the browser: every field filled where it belongs and checked before anything is
saved or sent.

Reported on a real PC: drafting in Gmail was "not done correctly". The agent typed into Gmail's compose window with
the generic browser_fill: a recipient stayed as loose text (or Enter picked whichever contact Gmail highlighted), the
body landed in the Subject box, and send_email pressed Send without looking at what the compose window held.

Here one routine does it, the same way every time:
  1. open the compose page with the draft in the link (Gmail itself puts To, Subject and Body in their fields);
  2. read the compose window back: the recipient chips, the text still loose in the To box, the Subject value and
     the body text (Gmail's signature under the body is fine);
  3. fix what is wrong, field by field: a recipient is typed and the suggestion whose address matches EXACTLY is
     picked (a different contact Gmail highlights is never taken; with no exact match the address is confirmed as
     typed), the Subject is set, the body is set as plain text in the body editor;
  4. read it back again; if anything is still wrong, nothing is saved or sent and the agent is told what;
  5. then either keep it as a Gmail draft (Save & close) or, only for send_email after the user's yes, press the
     compose window's own Send button and wait for "Message sent".

The routine talks to a small driver: PageDriver for Delta's own browser (Playwright, exact DOM reads); tests use a
fake. In the user's own browser (Windows UI Automation) SendEmail keeps its simpler path: the compose link fills the
fields and the page is checked for the subject and recipients before Send (see delta.tools.email).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Mode = Literal["draft", "send"]
Kind = Literal["to", "cc", "bcc"]
FIX_ROUNDS = 2
READY_WAIT_S = 20.0
CONFIRM_WAIT_S = 12.0


@dataclass
class ComposeState:
    found: bool = False                                  # a compose window with Subject and Body is open
    to: list[str] = field(default_factory=list)          # recipient chips (addresses)
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    to_typed: str = ""                                   # text in the To box that is not a chip yet
    subject: str = ""
    body: str = ""
    signed_out: bool = False
    note: str = ""                                       # a toast: "Message sent", "Draft saved"


class Driver(Protocol):
    async def open(self, url: str) -> None: ...
    async def state(self) -> ComposeState: ...
    async def set_recipients(self, kind: Kind, addresses: list[str]) -> None: ...
    async def set_subject(self, text: str) -> None: ...
    async def set_body(self, text: str) -> None: ...
    async def save_draft(self) -> str: ...
    async def send(self) -> str: ...


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\u00a0", " ")).strip().lower()


def body_matches(shown: str, wanted: str) -> bool:
    """The body editor holds the draft (spacing aside); a signature or quoted text below it is fine."""
    s, w = _flat(shown), _flat(wanted)
    return bool(w) and s.startswith(w)


def issues(st: ComposeState, to: list[str], subject: str, body: str, cc: list[str] | None = None,
           bcc: list[str] | None = None) -> list[tuple[str, str]]:
    """(field, what is wrong in plain words) for the compose window; [] = exactly the draft."""
    if st.signed_out:
        return [("signin", "Gmail asks to sign in")]
    if not st.found:
        return [("window", "no Gmail compose window is open")]
    out: list[tuple[str, str]] = []
    for kind, want, got in (("to", to, st.to), ("cc", cc or [], st.cc), ("bcc", bcc or [], st.bcc)):
        w = {a.lower() for a in want}
        g = {a.lower() for a in got}
        if missing := sorted(w - g):
            out.append((kind, f"{kind.capitalize()} is missing {', '.join(missing)}"))
        if extra := sorted(g - w):
            out.append((kind, f"{kind.capitalize()} has {', '.join(extra)}, which is not in the draft"))
    if st.to_typed.strip():
        out.append(("to", f"To still has loose text \"{st.to_typed.strip()[:60]}\" that is not a recipient"))
    if _flat(st.subject) != _flat(subject):
        if body and _flat(body)[:40] and _flat(body)[:40] in _flat(st.subject):
            out.append(("subject", "the email text went into the Subject box"))
        else:
            out.append(("subject", f"Subject reads \"{st.subject[:80]}\", not \"{subject[:80]}\""))
    if not body_matches(st.body, body):
        out.append(("body", "the body does not hold the draft text" + (" (it is empty)" if not st.body.strip()
                                                                        else "")))
    return out


def problems(st: ComposeState, to: list[str], subject: str, body: str, cc: list[str] | None = None,
             bcc: list[str] | None = None) -> list[str]:
    """What is wrong in the compose window, in plain words ([] = exactly the draft)."""
    return [text for _, text in issues(st, to, subject, body, cc, bcc)]


@dataclass
class Outcome:
    ok: bool
    detail: str
    fixed: list[str] = field(default_factory=list)  # what had to be corrected on the way


class GmailComposer:
    """Fill, check, fix, check again; then keep as a draft or send."""

    def __init__(self, driver: Driver, ready_wait_s: float = READY_WAIT_S, poll_s: float = 0.5) -> None:
        self.d = driver
        self.ready_wait_s = ready_wait_s
        self.poll_s = poll_s

    async def _ready(self) -> ComposeState:
        loop = asyncio.get_running_loop()
        end = loop.time() + self.ready_wait_s
        st = await self.d.state()
        while not (st.found or st.signed_out) and loop.time() < end:
            await asyncio.sleep(self.poll_s)
            st = await self.d.state()
        return st

    async def run(self, url: str, to: list[str], subject: str, body: str, mode: Mode,
                  cc: list[str] | None = None, bcc: list[str] | None = None) -> Outcome:
        await self.d.open(url)
        st = await self._ready()
        fixed: list[str] = []
        wanted: dict[str, list[str]] = {"to": to, "cc": cc or [], "bcc": bcc or []}
        for _ in range(FIX_ROUNDS):
            found = issues(st, to, subject, body, cc, bcc)
            if not found or st.signed_out or not st.found:
                break
            fixed += [text for _, text in found]
            fields = {f for f, _ in found}
            kinds: tuple[Kind, ...] = ("to", "cc", "bcc")
            for kind in kinds:
                if kind in fields:
                    await self.d.set_recipients(kind, wanted[kind])
            if "subject" in fields:
                await self.d.set_subject(subject)
            if "body" in fields:
                await self.d.set_body(body)
            st = await self.d.state()
        left = problems(st, to, subject, body, cc, bcc)
        if left:
            if st.signed_out:
                return Outcome(False, "Gmail asks to sign in. Sign in to Gmail in the browser, then ask again.",
                               fixed)
            return Outcome(False, "the compose window does not match the draft, so nothing was "
                                  f"{'sent' if mode == 'send' else 'saved'}: {'; '.join(left)}.", fixed)
        note = await (self.d.send() if mode == "send" else self.d.save_draft())
        if mode == "send":
            seen = "sent" in note.lower()
            return Outcome(True, "" if seen else " (Gmail's 'Message sent' note was not seen yet)", fixed)
        return Outcome(True, "" if "saved" in note.lower() or "draft" in note.lower() else
                       " (Gmail's 'Draft saved' note was not seen; it is usually in Drafts within seconds)", fixed)


# --- Delta's own browser (Playwright) --------------------------------------------------------------------------
# One script reads the whole compose window. Gmail's markup changes now and then, so every field has a few
# selectors, the stable ones first (names and accessible labels, not class names).
_STATE_JS = r"""
() => {
  const vis = e => !!e && !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const all = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const first = (sels, root) => {
    for (const s of sels) { const f = all(s, root).find(vis); if (f) return f; }
    return null;
  };
  const subj = first(['input[name="subjectbox"]', 'input[aria-label="Subject" i]', 'input[placeholder="Subject" i]']);
  const root = (subj && (subj.closest('[role="dialog"]') || subj.closest('form') || subj.closest('table'))) || document;
  const body = first(['div[aria-label="Message Body" i][contenteditable="true"]',
                      'div[role="textbox"][aria-label*="body" i][contenteditable="true"]',
                      'div[g_editable="true"][contenteditable="true"]'], root);
  const box = kind => first([`input[aria-label="${kind} recipients" i]`, `input[aria-label="${kind}" i]`,
                             `input[name="${kind.toLowerCase()}"]:not([type="hidden"])`,
                             `textarea[name="${kind.toLowerCase()}"]`], root);
  const chips = input => {
    if (!input) return [];
    let row = input;
    for (let i = 0; i < 8 && row.parentElement; i++) {
      row = row.parentElement;
      if (subj && row.contains(subj)) { row = input.parentElement; break; }
      if (row.querySelector('[data-hovercard-id*="@"], [email*="@"]')) break;
    }
    const seen = all('[data-hovercard-id*="@"], [email*="@"]', row)
      .map(e => (e.getAttribute('email') || e.getAttribute('data-hovercard-id') || '').trim()).filter(Boolean);
    return Array.from(new Set(seen));
  };
  const to = box('To'), cc = box('Cc'), bcc = box('Bcc');
  const toasts = all('[role="alert"], [aria-live="assertive"], [aria-live="polite"]').filter(vis)
    .map(e => e.innerText || '').join(' ');
  const signedOut = location.hostname.endsWith('accounts.google.com') ||
                    !!document.querySelector('input[type="email"][name="identifier"]');
  return {found: !!(subj && body), to: chips(to), cc: chips(cc), bcc: chips(bcc), to_typed: to ? (to.value || '') : '',
          subject: subj ? subj.value : '', body: body ? body.innerText : '', signed_out: signedOut,
          note: toasts.slice(0, 300)};
}
"""


class PageDriver:
    """Gmail's compose window in Delta's own browser, through Playwright."""

    def __init__(self, page: Any, suggestion_wait_s: float = 2.5) -> None:
        self.page = page
        self.suggestion_wait_s = suggestion_wait_s

    async def open(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)

    async def state(self) -> ComposeState:
        try:
            raw = await self.page.evaluate(_STATE_JS)
        except Exception:
            return ComposeState()
        return ComposeState(**{k: raw.get(k, v) for k, v in ComposeState().__dict__.items()})

    def _root(self) -> Any:
        return self.page.locator('[role="dialog"]:has(input[name="subjectbox"]), form:has(input[name="subjectbox"])'
                                 ).last

    async def _box(self, kind: Kind) -> Any:
        label = kind.capitalize()
        for sel in (f'input[aria-label="{label} recipients" i]', f'input[aria-label="{label}" i]',
                    f'input[name="{kind}"]:not([type="hidden"])', f'textarea[name="{kind}"]'):
            loc = self.page.locator(sel)
            for i in range(min(await loc.count(), 5)):
                if await loc.nth(i).is_visible():
                    return loc.nth(i)
        if kind != "to":  # Cc and Bcc are hidden until their link is clicked
            link = self.page.get_by_role("link", name=label, exact=True).or_(
                self.page.locator(f'span[role="link"][aria-label*="{label}" i]'))
            if await link.count():
                await link.first.click()
                return await self._box(kind) if await self.page.locator(f'input[aria-label="{label} recipients" i]'
                                                                        ).count() else None
        return None

    async def _remove_chips(self, kind: Kind, keep: set[str]) -> None:
        st = await self.state()
        for addr in getattr(st, kind):
            if addr.lower() in keep:
                continue
            chip = self.page.locator(f'[data-hovercard-id="{addr}"], [email="{addr}"]').first
            remove = chip.locator('[aria-label^="Remove" i], [aria-label^="Delete" i]')
            if await remove.count():
                await remove.first.click()
            else:  # select the chip and delete it with the keyboard
                await chip.click()
                await self.page.keyboard.press("Backspace")

    async def set_recipients(self, kind: Kind, addresses: list[str]) -> None:
        box = await self._box(kind)
        if box is None:
            return
        want = [a.strip() for a in addresses if a.strip()]
        await self._remove_chips(kind, {a.lower() for a in want})
        await box.fill("")
        have = {a.lower() for a in getattr(await self.state(), kind)}
        for addr in want:
            if addr.lower() in have:
                continue
            await box.click()
            await box.press_sequentially(addr, delay=15)
            picked = await self._pick_suggestion(addr)
            if not picked:
                # No suggestion for exactly this address: confirm it as typed. Enter would take whichever contact
                # Gmail highlights, so a comma ends the address instead.
                await box.press_sequentially(",")
            await asyncio.sleep(0.2)
        await box.fill("")  # nothing loose may stay behind in the box

    async def _pick_suggestion(self, addr: str) -> bool:
        options = self.page.locator('[role="listbox"] [role="option"]')
        loop = asyncio.get_running_loop()
        end = loop.time() + self.suggestion_wait_s
        while loop.time() < end:
            for i in range(min(await options.count(), 12)):
                opt = options.nth(i)
                try:
                    if not await opt.is_visible():
                        continue
                    text = (await opt.inner_text()) + " " + " ".join(
                        filter(None, [await opt.get_attribute(a) for a in ("data-hovercard-id", "data-email",
                                                                             "aria-label")]))
                except Exception:
                    continue
                if re.search(rf"(?<![\w.+-]){re.escape(addr)}(?![\w.-])", text, re.I):
                    await opt.click()
                    return True
            await asyncio.sleep(0.25)
        return False

    async def set_subject(self, text: str) -> None:
        box = self.page.locator('input[name="subjectbox"], input[aria-label="Subject" i]').first
        await box.fill(text)

    async def set_body(self, text: str) -> None:
        """Plain text in the body editor, line by line (Enter makes Gmail's own line breaks)."""
        body = self.page.locator('div[aria-label="Message Body" i][contenteditable="true"], '
                                 'div[role="textbox"][aria-label*="body" i][contenteditable="true"]').first
        await body.click()
        await self.page.keyboard.press("Control+A")
        await self.page.keyboard.press("Delete")
        lines = text.replace("\r\n", "\n").split("\n")
        for i, line in enumerate(lines):
            if line:
                await self.page.keyboard.insert_text(line)
            if i < len(lines) - 1:
                await self.page.keyboard.press("Enter")

    async def _wait_note(self, words: tuple[str, ...]) -> str:
        loop = asyncio.get_running_loop()
        end = loop.time() + CONFIRM_WAIT_S
        note = ""
        while loop.time() < end:
            note = (await self.state()).note
            if any(w in note.lower() for w in words):
                return note
            with_text = self.page.get_by_text(re.compile("|".join(words), re.I))
            if await with_text.count() and await with_text.first.is_visible():
                return await with_text.first.inner_text()
            await asyncio.sleep(0.4)
        return note

    async def save_draft(self) -> str:
        """Gmail saves the draft by itself; Save & close makes sure and closes the window."""
        note = await self._wait_note(("draft saved", "saved"))
        close = self.page.locator('[aria-label="Save & close" i], [data-tooltip="Save & close" i]')
        if await close.count():
            await close.first.click()
        return note or "draft"

    async def send(self) -> str:
        """The compose window's own Send button (never just any "Send" text on the page)."""
        button = self._root().locator('div[role="button"][aria-label^="Send" i], button[aria-label^="Send" i], '
                                      'div[role="button"][data-tooltip^="Send" i]').first
        if not await button.count():
            button = self.page.locator('div[role="button"][aria-label^="Send" i], button[aria-label^="Send" i]'
                                       ).first
        await button.click()
        return await self._wait_note(("message sent", "sent"))


# --- typing into Gmail's compose window by hand --------------------------------------------------------------------
_COMPOSE_FIELDS = re.compile(r"^\s*(to|to recipients|recipients|cc|cc recipients|bcc|bcc recipients|subject|"
                             r"subjectbox|message body|body|message)\s*$", re.I)


def gmail_fill_hint(url: str, field: str) -> str:
    """For browser_fill on Gmail's compose fields: the reason to use the email tools instead ("" = fill as usual).
    Typed by hand, a recipient stayed loose text or became the wrong contact, and the body went into Subject."""
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""
    if host != "mail.google.com" or not _COMPOSE_FIELDS.match(field or ""):
        return ""
    return ("Not typed: Gmail's compose window is filled by the email tools, which put every field in its place and "
            "check it. Call draft_email (to, subject, body), then save_email_draft to keep it in Gmail's Drafts, or "
            "send_email to send it after the user approves.")
