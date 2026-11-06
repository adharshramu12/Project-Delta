"""Email: Delta drafts in the user's voice, shows the full draft, and sends only after the user approves THAT email.

  draft_email(to, cc, bcc, subject, body)  checks the recipients, puts the text through the writing style
                                           (delta.core.style.polish_outgoing: code fixes, at most one cheap model
                                           rewrite) and keeps the draft. Sends nothing.
  send_email(draft_id)                     asks the user with the WHOLE draft and every recipient on screen
                                           (Approve / Edit / Cancel in the dashboard, Telegram and Slack), then
                                           sends that exact draft.

Never auto-send: send_email is `approve_each`, so the policy asks every time, whatever the permission level
(Autopilot too), "Allow for this task" is never offered, and a learned "you always say yes" never applies. Edit
sends nothing and gives the agent the user's changes to redraft. The draft is frozen when it is shown: what the
user approved is exactly what goes out.

How it is sent: through the Gmail app (Apps page) when it is connected and offers a send tool; otherwise through
Gmail in the user's own browser (the compose page, filled from the approved draft, then Send). Google's official
Gmail MCP server (Developer Preview) only creates drafts, so with it Delta sends through the browser.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, Field, field_validator

from delta.tools.base import Risk, Tool, ToolResult

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
SEND_TOOL_NAMES = ("send_email", "send_message", "send_mail", "gmail_send_email", "gmail_send", "send")
GMAIL_COMPOSE = "https://mail.google.com/mail/?view=cm&fs=1"
MAX_URL = 7500  # the browser path carries the draft in the compose link; longer drafts go through the Gmail app
PREVIEW_CHARS = 3000  # Telegram allows 4096 per message: the whole draft fits with the recipients
DRAFT_TTL_S = 6 * 3600


def _addresses(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    items = value if isinstance(value, list) else re.split(r"[,;]", str(value))
    out = []
    for item in items:
        a = str(item).strip()
        m = re.search(r"<([^>]+)>", a)  # "Ravi Kumar <ravi@x.com>"
        out.append((m.group(1) if m else a).strip())
    return [a for a in dict.fromkeys(out) if a]


class DraftArgs(BaseModel):
    to: list[str] = Field(description="Recipient email addresses (never guess an address: look it up or ask)")
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20000, description="The email text, in the user's voice")
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)

    @field_validator("to", "cc", "bcc", mode="before")
    @classmethod
    def _split(cls, v: Any) -> list[str]:
        return _addresses(v)


@dataclass
class Draft:
    id: str
    to: list[str]
    subject: str
    body: str
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.monotonic)
    sent: bool = False

    def preview(self) -> str:
        lines = [f"To: {', '.join(self.to)}"]
        if self.cc:
            lines.append(f"Cc: {', '.join(self.cc)}")
        if self.bcc:
            lines.append(f"Bcc: {', '.join(self.bcc)}")
        body = self.body if len(self.body) <= PREVIEW_CHARS else (
            self.body[:PREVIEW_CHARS] + "\n[...the rest of the email]")
        lines += [f"Subject: {self.subject}", "", body]
        return "\n".join(lines)


class Drafts:
    """Drafts waiting for the user's answer (in memory: a draft is never written to disk)."""

    def __init__(self) -> None:
        self.items: dict[str, Draft] = {}

    def add(self, d: Draft) -> Draft:
        now = time.monotonic()
        self.items = {k: v for k, v in self.items.items() if now - v.created < DRAFT_TTL_S and not v.sent}
        self.items[d.id] = d
        return d

    def get(self, draft_id: str) -> Draft | None:
        """By id, or "latest": the newest draft not sent yet (the approval still shows exactly which one)."""
        if draft_id.strip().lower() == "latest":
            waiting = [d for d in self.items.values() if not d.sent]
            return max(waiting, key=lambda d: d.created) if waiting else None
        return self.items.get(draft_id.strip())


class DraftEmail(Tool[DraftArgs]):
    name = "draft_email"
    description = ("Prepare an email for the user to approve. First learn the context (the recipient, the thread, "
                   "how the user writes to them) with the tools you have, then write it in the user's voice: plain, "
                   "professional Indian English, no AI phrases. Returns a draft id for send_email. Sends nothing.")
    risk = Risk.READ
    Args = DraftArgs

    def __init__(self, drafts: Drafts, router: Any = None) -> None:
        self.drafts = drafts
        self.router = router

    async def run(self, args: DraftArgs) -> ToolResult:
        if not args.to:
            return ToolResult(False, "No recipient. Ask the user who it goes to (an email address).")
        bad = [a for a in (*args.to, *args.cc, *args.bcc) if not EMAIL_RE.match(a)]
        if bad:
            return ToolResult(False, f"Not an email address: {', '.join(bad)}. Find the exact address (their "
                                     "earlier emails, contacts) or ask the user; never guess one.")
        from delta.core.style import polish_outgoing, slop_check

        body = await polish_outgoing(args.body, self.router)
        subject = slop_check(args.subject, "outgoing").text or args.subject
        d = self.drafts.add(Draft(uuid.uuid4().hex[:8], list(args.to), subject, body.text, list(args.cc),
                                  list(args.bcc)))
        note = f" Still worth a look: {'; '.join(body.remaining)}." if body.remaining else ""
        return ToolResult(True, f"Draft {d.id} is ready (nothing sent).{note} Call send_email with draft_id "
                                f"\"{d.id}\": the user sees this exact draft and decides.\n\n{d.preview()}",
                          {"draft_id": d.id, "to": d.to, "cc": d.cc, "subject": d.subject})


class SendArgs(BaseModel):
    draft_id: str = Field(min_length=1, max_length=40, description='The id draft_email returned (or "latest")')


class SendEmail(Tool[SendArgs]):
    name = "send_email"
    description = ("Send a draft from draft_email. The user sees the full draft and recipients and must approve "
                   "this one email (Approve / Edit / Cancel). If they choose Edit, redraft with their changes.")
    risk = Risk.EXTERNAL
    Args = SendArgs
    approve_each = True   # the policy asks for every email, at every permission level; never learned or task-wide
    always_ask = True
    can_edit = True       # the approval offers Edit

    def __init__(self, drafts: Drafts) -> None:
        self.drafts = drafts
        self.connectors: Any = None  # delta.connectors.tools.AppTools, set by bootstrap when apps are on
        self.registry: Any = None    # the tool registry (browser_open / browser_click), set by bootstrap

    def summary(self, args: SendArgs) -> str:
        d = self.drafts.get(args.draft_id)
        if d is None:
            return f"send email draft {args.draft_id} (unknown draft: nothing will be sent)"
        return "Send this email:\n" + d.preview()

    def domains(self, args: SendArgs) -> list[str]:
        return ["mail.google.com"]

    async def run(self, args: SendArgs) -> ToolResult:
        d = self.drafts.get(args.draft_id)
        if d is None:
            return ToolResult(False, "That draft is not here any more. Make it again with draft_email.")
        if d.sent:
            return ToolResult(False, "This draft was already sent. Do not send it again.")
        via = await self._send_with_app(d)
        if via is None:
            via = await self._send_in_browser(d)
        ok, how, detail = via
        if ok:
            d.sent = True
            return ToolResult(True, f"Sent the email \"{d.subject}\" to {', '.join(d.to)} via {how}.{detail}",
                              {"to": d.to, "cc": d.cc, "subject": d.subject, "via": how, "verified": True})
        return ToolResult(False, f"The email was NOT sent ({how}): {detail}", {"via": how})

    # --- ways to send ----------------------------------------------------------------------------------------
    def _app_send_tool(self) -> tuple[str, str] | None:
        """(app id, tool name) of a connected Gmail app tool that sends mail and takes to/subject/body."""
        apps = self.connectors
        if apps is None:
            return None
        try:
            connected = apps.manager.connected()
        except Exception:
            return None
        for spec, conn in connected:
            if "gmail" not in spec.id.lower() and "gmail" not in spec.name.lower():
                continue
            if conn.needs_reconnect:
                continue
            for t in conn.tools:
                name = str(t.get("name", ""))
                props = set(((t.get("input_schema") or {}).get("properties") or {}).keys())
                if name.lower() in SEND_TOOL_NAMES and {"to", "subject"} <= props and ({"body", "text"} & props):
                    return spec.id, name
        return None

    async def _send_with_app(self, d: Draft) -> tuple[bool, str, str] | None:
        found = self._app_send_tool()
        if found is None:
            return None
        app_id, tool = found
        conn = self.connectors.manager.store.get(app_id)
        props = set()
        for t in conn.tools if conn else []:
            if t.get("name") == tool:
                props = set(((t.get("input_schema") or {}).get("properties") or {}).keys())
        args: dict[str, Any] = {"to": d.to if _takes_list(conn, tool, "to") else ", ".join(d.to), "subject": d.subject,
                                ("body" if "body" in props else "text"): d.body}
        if d.cc and "cc" in props:
            args["cc"] = d.cc if _takes_list(conn, tool, "cc") else ", ".join(d.cc)
        if d.bcc and "bcc" in props:
            args["bcc"] = d.bcc if _takes_list(conn, tool, "bcc") else ", ".join(d.bcc)
        if (d.cc and "cc" not in props) or (d.bcc and "bcc" not in props):
            return None  # the app cannot carry every recipient: the browser can
        from delta.tools.base import Risk as R

        out = await self.connectors.manager.call(app_id, tool, args, R.EXTERNAL)
        return (True, "the Gmail app", "") if out.ok else (False, "the Gmail app", out.text[:300])

    async def _send_in_browser(self, d: Draft) -> tuple[bool, str, str]:
        reg = self.registry
        opener = reg.get("browser_open") if reg is not None else None
        clicker = reg.get("browser_click") if reg is not None else None
        if opener is None or clicker is None:
            return False, "Gmail in the browser", "no browser is available on this PC, and no Gmail app is connected."
        url = compose_url(d)
        if len(url) > MAX_URL:
            return False, "Gmail in the browser", ("this email is too long to open in the compose page. Connect "
                                                   "Gmail on the Apps page, or paste it into Gmail yourself.")
        opened = await opener.run(opener.Args.model_validate({"url": url}))
        if not opened.ok:
            return False, "Gmail in the browser", f"Gmail did not open: {opened.output[:200]}"
        if _signed_out(opened.output):
            return False, "Gmail in the browser", ("Gmail asks to sign in. Sign in to Gmail in your browser, then "
                                                   "ask again.")
        clicked = await clicker.run(clicker.Args.model_validate({"text": "Send"}))
        if not clicked.ok:
            return False, "Gmail in the browser", f"the Send button could not be pressed: {clicked.output[:200]}"
        seen = "message sent" in clicked.output.lower()
        return True, "Gmail in your browser", "" if seen else " (Gmail's 'Message sent' note was not seen yet)"


def _takes_list(conn: Any, tool: str, key: str) -> bool:
    for t in getattr(conn, "tools", None) or []:
        if t.get("name") == tool:
            prop = ((t.get("input_schema") or {}).get("properties") or {}).get(key) or {}
            return prop.get("type") == "array"
    return False


def _signed_out(page: str) -> bool:
    low = page.lower()
    return "accounts.google.com" in low and ("sign in" in low or "choose an account" in low)


def compose_url(d: Draft) -> str:
    parts = [GMAIL_COMPOSE, "to=" + quote(",".join(d.to), safe="@,")]
    if d.cc:
        parts.append("cc=" + quote(",".join(d.cc), safe="@,"))
    if d.bcc:
        parts.append("bcc=" + quote(",".join(d.bcc), safe="@,"))
    parts += ["su=" + quote(d.subject, safe=""), "body=" + quote(d.body, safe="")]
    return "&".join(parts)


def email_tools(router: Any = None) -> tuple[DraftEmail, SendEmail]:
    drafts = Drafts()
    return DraftEmail(drafts, router), SendEmail(drafts)
