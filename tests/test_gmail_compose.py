"""Writing an email in Gmail: every field filled where it belongs and read back before anything is saved or sent.

Reported on a real PC: "Gmail drafting is not done correctly". FakeGmail below behaves like Gmail's compose window in
the ways that went wrong: the compose link may not fill the fields (account chooser, slow load), typing an address
shows suggestions with ANOTHER contact highlighted (Enter would pick that one), an address typed without confirming
stays loose text, the body can end up in the Subject box, and a signature sits under the body. The real-browser twin
of these tests (tests/e2e/test_gmail_compose_e2e.py) runs the PageDriver against a fake Gmail page in Chrome.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from delta.tools.gmail_compose import ComposeState, GmailComposer, body_matches, gmail_fill_hint, problems

CONTACTS = ["Ravi Kumar <ravi.k@gmail.com>", "Ravi Teja <ravi@example.com>", "Priya S <priya@example.org>"]


class FakeGmail:
    """A compose window. `prefill`: how the compose link lands ("ok", "empty", "body_in_subject", "loose_to")."""

    def __init__(self, prefill: str = "ok", signature: str = "", stuck_body: bool = False,
                 signed_out: bool = False) -> None:
        self.prefill = prefill
        self.signature = signature
        self.stuck_body = stuck_body
        self.signed_out = signed_out
        self.st = ComposeState()
        self.saved = 0
        self.sent = 0
        self.actions: list[str] = []
        self.opened = ""

    async def open(self, url: str) -> None:
        self.opened = url
        if self.signed_out:
            self.st = ComposeState(signed_out=True)
            return
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        to = [a for a in q.get("to", "").split(",") if a]
        su, body = q.get("su", ""), q.get("body", "")
        self.st = ComposeState(found=True, body=self.signature)
        if self.prefill == "ok":
            self.st.to, self.st.subject, self.st.body = to, su, body + self.signature
        elif self.prefill == "body_in_subject":
            self.st.to, self.st.subject = to, body
        elif self.prefill == "loose_to":
            self.st.to_typed, self.st.subject, self.st.body = ", ".join(to), su, body + self.signature

    async def state(self) -> ComposeState:
        return ComposeState(**{**self.st.__dict__, "to": list(self.st.to), "cc": list(self.st.cc),
                               "bcc": list(self.st.bcc)})

    async def set_recipients(self, kind: str, addresses: list[str]) -> None:
        self.actions.append(f"recipients:{kind}")
        chips = getattr(self.st, kind)
        chips[:] = [a for a in chips if a.lower() in {x.lower() for x in addresses}]
        self.st.to_typed = ""
        for addr in addresses:
            if addr.lower() in {c.lower() for c in chips}:
                continue
            # Gmail suggests contacts that match what was typed; the FIRST (highlighted) one may be someone else.
            suggestions = [c for c in CONTACTS if addr.split("@")[0].lower() in c.lower()]
            exact = [c for c in suggestions if f"<{addr.lower()}>" in c.lower()]
            chips.append(exact[0].split("<")[1].rstrip(">") if exact else addr)

    async def set_subject(self, text: str) -> None:
        self.actions.append("subject")
        self.st.subject = text

    async def set_body(self, text: str) -> None:
        self.actions.append("body")
        if not self.stuck_body:
            self.st.body = text + self.signature

    async def save_draft(self) -> str:
        self.saved += 1
        return "Draft saved"

    async def send(self) -> str:
        self.sent += 1
        return "Message sent"


URL = ("https://mail.google.com/mail/?view=cm&fs=1&to=ravi%40example.com&su=Deck%20for%20Friday"
       "&body=Hi%20Ravi%2C%0A%0AThe%20deck%20is%20ready.%0A%0AAdharsh")
TO, SUBJECT, BODY = ["ravi@example.com"], "Deck for Friday", "Hi Ravi,\n\nThe deck is ready.\n\nAdharsh"


def composer(g: FakeGmail) -> GmailComposer:
    return GmailComposer(g, ready_wait_s=0.05, poll_s=0.01)


async def test_a_good_compose_is_saved_as_a_draft_without_touching_anything():
    g = FakeGmail("ok", signature="\n--\nAdharsh Kumar")
    out = await composer(g).run(URL, TO, SUBJECT, BODY, "draft")
    assert out.ok and g.saved == 1 and g.sent == 0 and g.actions == [] and out.fixed == []


async def test_an_empty_compose_is_filled_and_the_exact_contact_is_picked_not_the_highlighted_one():
    g = FakeGmail("empty")
    out = await composer(g).run(URL, TO, SUBJECT, BODY, "send")
    assert out.ok and g.sent == 1
    assert g.st.to == ["ravi@example.com"]  # not ravi.k@gmail.com, which Gmail suggested first
    assert g.st.subject == SUBJECT and body_matches(g.st.body, BODY)
    assert {"recipients:to", "subject", "body"} <= set(g.actions)


async def test_the_body_in_the_subject_box_is_put_back_where_it_belongs():
    g = FakeGmail("body_in_subject")
    out = await composer(g).run(URL, TO, SUBJECT, BODY, "draft")
    assert out.ok and g.st.subject == SUBJECT and body_matches(g.st.body, BODY)
    assert any("went into the Subject" in f for f in out.fixed)


async def test_a_loose_address_in_to_becomes_a_recipient():
    g = FakeGmail("loose_to")
    out = await composer(g).run(URL, TO, SUBJECT, BODY, "draft")
    assert out.ok and g.st.to == TO and g.st.to_typed == ""
    assert any("loose text" in f for f in out.fixed)


async def test_nothing_is_sent_when_a_field_cannot_be_fixed():
    g = FakeGmail("empty", stuck_body=True)
    out = await composer(g).run(URL, TO, SUBJECT, BODY, "send")
    assert not out.ok and g.sent == 0 and g.saved == 0
    assert "nothing was sent" in out.detail and "body" in out.detail


async def test_signed_out_gmail_is_said_plainly():
    g = FakeGmail(signed_out=True)
    out = await composer(g).run(URL, TO, SUBJECT, BODY, "send")
    assert not out.ok and g.sent == 0 and "sign in" in out.detail.lower()


def test_problems_name_each_wrong_field():
    st = ComposeState(found=True, to=["ravi.k@gmail.com"], subject=BODY, body="")
    found = problems(st, TO, SUBJECT, BODY)
    text = " | ".join(found)
    assert "missing ravi@example.com" in text and "ravi.k@gmail.com, which is not in the draft" in text
    assert "went into the Subject" in text and "body does not hold" in text
    ok = ComposeState(found=True, to=["Ravi@Example.com"], subject="Deck  for Friday ",
                      body="Hi Ravi,\nThe deck is ready.\nAdharsh\n\n-- \nsig")
    assert problems(ok, TO, SUBJECT, BODY) == []


@pytest.mark.parametrize(("url", "field", "hinted"), [
    ("https://mail.google.com/mail/u/0/#inbox?compose=new", "To recipients", True),
    ("https://mail.google.com/mail/u/0/#inbox?compose=new", "Subject", True),
    ("https://mail.google.com/mail/u/0/#inbox?compose=new", "Message Body", True),
    ("https://mail.google.com/mail/u/0/#inbox", "Search mail", False),
    ("https://example.com/contact", "Subject", False),
    ("https://mail.google.com.evil.example/", "Subject", False),
])
def test_typing_into_gmail_compose_by_hand_points_to_the_email_tools(url, field, hinted):
    hint = gmail_fill_hint(url, field)
    assert bool(hint) is hinted
    if hinted:
        assert "draft_email" in hint and "save_email_draft" in hint and "send_email" in hint


# ---- the tools ----------------------------------------------------------------------------------------------------
def tools(driver: Any = None) -> tuple[Any, Any, Any]:
    from delta.tools.email import email_tools, save_draft_tool

    draft, send = email_tools()
    if driver is not None:
        async def make() -> Any:
            return driver

        send.driver_factory = make
    return draft, send, save_draft_tool(send)


async def make_draft(draft: Any) -> str:
    r = await draft.run(draft.Args.model_validate({"to": TO, "subject": SUBJECT, "body": BODY}))
    assert r.ok and "save_email_draft" in r.output
    return str(r.evidence["draft_id"])


async def test_save_email_draft_keeps_it_in_gmail_and_sends_nothing():
    g = FakeGmail("empty")
    draft, _send, save = tools(g)
    did = await make_draft(draft)
    r = await save.run(save.Args.model_validate({"draft_id": did}))
    assert r.ok and r.evidence["saved"] and not r.evidence["sent"] and g.saved == 1 and g.sent == 0
    assert "not sent" in r.output and "corrected" in r.output
    assert "mail.google.com/mail/?view=cm" in g.opened


async def test_send_email_checks_the_compose_window_before_send():
    g = FakeGmail("empty", stuck_body=True)
    draft, send, _ = tools(g)
    did = await make_draft(draft)
    r = await send.run(send.Args.model_validate({"draft_id": did}))
    assert not r.ok and g.sent == 0 and "NOT sent" in r.output
    g2 = FakeGmail("ok")
    draft, send, _ = tools(g2)
    did = await make_draft(draft)
    r = await send.run(send.Args.model_validate({"draft_id": did}))
    assert r.ok and g2.sent == 1 and r.evidence["verified"]


async def test_a_long_email_is_typed_into_the_body_instead_of_the_link():
    from delta.tools import email as E

    g = FakeGmail("ok")
    draft, send, save = tools(g)
    long_body = "Hi Ravi,\n\n" + ("Here is the full plan for the launch week, day by day. " * 160) + "\n\nAdharsh"
    r = await draft.run(draft.Args.model_validate({"to": TO, "subject": SUBJECT, "body": long_body}))
    assert r.ok
    did = str(r.evidence["draft_id"])
    d = send.drafts.get(did)
    assert len(E.compose_url(d)) > E.MAX_URL
    out = await save.run(save.Args.model_validate({"draft_id": did}))
    assert out.ok and "body" in g.actions and body_matches(g.st.body, d.body)
    assert "body=&" in g.opened or g.opened.endswith("body=")


async def test_save_email_draft_uses_the_gmail_apps_draft_tool_when_connected():
    from types import SimpleNamespace

    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def call(app: str, tool: str, args: dict[str, Any], risk: Any) -> Any:
        calls.append((app, tool, args))
        return SimpleNamespace(ok=True, text="draft r-123 created")

    spec = SimpleNamespace(id="gmail", name="Gmail")
    conn = SimpleNamespace(needs_reconnect=False, tools=[{"name": "create_draft", "input_schema": {
        "properties": {"to": {"type": "array"}, "subject": {}, "body": {}}}}])
    draft, send, save = tools(FakeGmail())
    send.connectors = SimpleNamespace(manager=SimpleNamespace(connected=lambda: [(spec, conn)], call=call))
    did = await make_draft(draft)
    r = await save.run(save.Args.model_validate({"draft_id": did}))
    assert r.ok and r.evidence["via"] == "the Gmail app"
    assert calls and calls[0][1] == "create_draft" and calls[0][2]["to"] == TO and calls[0][2]["body"]


async def test_save_email_draft_in_the_users_own_browser_opens_the_link_and_saves(monkeypatch):
    from pydantic import BaseModel

    from delta.tools.base import Risk, Tool, ToolResult
    from delta.tools.registry import ToolRegistry

    class U(BaseModel):
        url: str = ""
        text: str = ""

    class Open(Tool[U]):
        name, description, risk, Args = "browser_open", "open", Risk.ACT, U
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def run(self, args: U) -> ToolResult:
            self.urls.append(args.url)
            return ToolResult(True, "New Message  To recipients  Subject  Send  Save & close")

    class Click(Tool[U]):
        name, description, risk, Args = "browser_click", "click", Risk.ACT, U
        def __init__(self) -> None:
            self.clicked: list[str] = []

        async def run(self, args: U) -> ToolResult:
            self.clicked.append(args.text)
            return ToolResult(True, "Draft saved")

    import asyncio

    async def no_wait(s: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    draft, send, save = tools()
    opener, clicker = Open(), Click()
    send.registry = ToolRegistry([opener, clicker])
    did = await make_draft(draft)
    r = await save.run(save.Args.model_validate({"draft_id": did}))
    assert r.ok and opener.urls and clicker.clicked == ["Save & close"] and "Send" not in clicker.clicked
