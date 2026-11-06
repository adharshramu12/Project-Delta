"""Email: drafted in the user's voice, shown in full with every recipient, sent only after a yes for THAT email.
Approve / Edit / Cancel; through the Gmail app when it can send, else Gmail in the browser. All with fakes."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import BaseModel

from delta.config import Boundaries
from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.learning.store import LearningStore
from delta.safety.approvals import ApprovalBroker, ApprovalRequest
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine, Verdict
from delta.tools.base import Risk, Tool, ToolResult
from delta.tools.email import compose_url, email_tools
from delta.tools.registry import ToolExecutor, ToolRegistry


class UrlArgs(BaseModel):
    url: str


class TextArgs(BaseModel):
    text: str


class FakeOpen(Tool[UrlArgs]):
    name = "browser_open"
    description = "open"
    risk = Risk.ACT
    Args = UrlArgs

    def __init__(self, page: str = "Gmail compose: To, Subject, Send") -> None:
        self.urls: list[str] = []
        self.page = page

    async def run(self, args: UrlArgs) -> ToolResult:
        self.urls.append(args.url)
        return ToolResult(True, self.page)


class FakeClick(Tool[TextArgs]):
    name = "browser_click"
    description = "click"
    risk = Risk.ACT
    Args = TextArgs

    def __init__(self) -> None:
        self.clicked: list[str] = []

    async def run(self, args: TextArgs) -> ToolResult:
        self.clicked.append(args.text)
        return ToolResult(True, "Message sent. Undo View message")


def setup(tmp_path, answer, autonomy="autopilot", learning=None, page=None):
    draft, send = email_tools()
    opener, clicker = FakeOpen(page) if page else FakeOpen(), FakeClick()
    registry = ToolRegistry([draft, send, opener, clicker])
    send.registry = registry
    broker = ApprovalBroker(timeout_s=2)
    seen: list[ApprovalRequest] = []

    async def presenter(req: ApprovalRequest) -> None:
        seen.append(req)
        asyncio.get_running_loop().call_soon(answer, broker, req)

    broker.add_presenter("test", presenter)
    ex = ToolExecutor(registry, PolicyEngine(Boundaries(autonomy=autonomy)), broker,
                      AuditLog(tmp_path / "a.jsonl"), learning)
    return ex, seen, opener, clicker, send


def approve(b, r):
    b.resolve(r.id, True, "dashboard")


def deny(b, r):
    b.resolve(r.id, False, "dashboard")


async def make_draft(ex, **over):
    args = {"to": ["ravi@example.com"], "cc": "Meena <meena@example.com>", "subject": "Friday review",
            "body": "Hi Ravi,\n\nI hope this email finds you well. The deck is ready \u2014 shall we meet at 4?\n\n"
                    "Adharsh"}
    args.update(over)
    r = await ex.execute("draft_email", args, task_id="t1")
    assert r.ok, r.output
    return r.evidence["draft_id"], r


async def test_draft_is_polished_and_sends_nothing(tmp_path):
    ex, seen, opener, clicker, _ = setup(tmp_path, approve)
    _draft_id, r = await make_draft(ex)
    assert "\u2014" not in r.output and "finds you well" not in r.output
    assert "To: ravi@example.com" in r.output and "Cc: meena@example.com" in r.output
    assert seen == [] and opener.urls == [] and clicker.clicked == []  # drafting never asks and never sends


@pytest.mark.parametrize("to", [["Ravi"], ["ravi at example"], []])
async def test_a_draft_needs_real_addresses(tmp_path, to):
    ex, *_ = setup(tmp_path, approve)
    r = await ex.execute("draft_email", {"to": to, "subject": "x", "body": "y"}, task_id="t1")
    assert not r.ok and ("ask the user" in r.output.lower() or "never guess" in r.output)


async def test_every_email_asks_even_in_autopilot_and_shows_the_whole_draft(tmp_path):
    ex, seen, opener, clicker, _ = setup(tmp_path, approve, autonomy="autopilot")
    draft_id, _ = await make_draft(ex)
    token = CURRENT_REQUEST.set(TaskRequest("send the email to ravi now"))  # "clearly asked" never skips the yes
    try:
        r = await ex.execute("send_email", {"draft_id": draft_id}, task_id="t1")
    finally:
        CURRENT_REQUEST.reset(token)
    assert r.ok and len(seen) == 1
    req = seen[0]
    assert req.can_edit and not req.for_task  # Approve / Edit / Cancel; never "Allow for this task"
    for part in ("To: ravi@example.com", "Cc: meena@example.com", "Subject: Friday review", "shall we meet at 4"):
        assert part in req.summary
    q = parse_qs(urlsplit(opener.urls[0]).query)
    assert q["to"] == ["ravi@example.com"] and q["cc"] == ["meena@example.com"] and q["su"] == ["Friday review"]
    assert "shall we meet at 4" in q["body"][0] and clicker.clicked == ["Send"]
    assert "Sent the email" in r.output and r.evidence["via"] == "Gmail in your browser"


async def test_a_second_email_asks_again(tmp_path):
    ex, seen, *_ = setup(tmp_path, approve)
    for _ in range(2):
        draft_id, _ = await make_draft(ex)
        assert (await ex.execute("send_email", {"draft_id": draft_id}, task_id="t1")).ok
    assert len(seen) == 2


async def test_cancel_sends_nothing(tmp_path):
    ex, _seen, opener, clicker, _ = setup(tmp_path, deny)
    draft_id, _ = await make_draft(ex)
    r = await ex.execute("send_email", {"draft_id": draft_id}, task_id="t1")
    assert not r.ok and r.evidence.get("user_denied") and opener.urls == [] and clicker.clicked == []


async def test_edit_sends_nothing_and_hands_the_changes_back(tmp_path):
    ex, _seen, opener, clicker, _ = setup(tmp_path,
                                          lambda b, r: b.edit(r.id, "make it shorter, say 5 pm", "telegram:1"))
    draft_id, _ = await make_draft(ex)
    r = await ex.execute("send_email", {"draft_id": draft_id}, task_id="t1")
    assert not r.ok and "EDIT" in r.output and "say 5 pm" in r.output and r.evidence["edit"]
    assert opener.urls == [] and clicker.clicked == []
    assert not r.evidence.get("user_denied")  # an edit is not a refusal: the redraft can be sent
    ex2_answer = []

    def approve_now(b, req):
        ex2_answer.append(req)
        b.resolve(req.id, True, "dashboard")

    ex.approvals._presenters["test"] = lambda req: _later(approve_now, ex.approvals, req)
    new_id, _ = await make_draft(ex, body="Hi Ravi, 5 pm works. Adharsh")
    assert (await ex.execute("send_email", {"draft_id": new_id}, task_id="t1")).ok


async def _later(fn, broker, req):
    asyncio.get_running_loop().call_soon(fn, broker, req)


async def test_learned_trust_never_skips_an_email(tmp_path):
    learning = LearningStore(tmp_path / "l.db")
    ex, seen, *_ = setup(tmp_path, approve, autonomy="ask-risky", learning=learning)
    for _ in range(6):  # many yeses in a row would teach "stop asking" for other tools
        draft_id, _ = await make_draft(ex)
        assert (await ex.execute("send_email", {"draft_id": draft_id}, task_id="t1")).ok
    assert len(seen) == 6


async def test_a_sent_draft_is_never_sent_twice(tmp_path):
    ex, _seen, opener, *_ = setup(tmp_path, approve)
    draft_id, _ = await make_draft(ex)
    assert (await ex.execute("send_email", {"draft_id": draft_id}, task_id="t1")).ok
    again = await ex.execute("send_email", {"draft_id": draft_id}, task_id="t1")
    assert not again.ok and len(opener.urls) == 1


async def test_signed_out_gmail_is_reported_not_sent(tmp_path):
    ex, _seen, _opener, clicker, _ = setup(tmp_path, approve,
                                         page="accounts.google.com Sign in to continue to Gmail")
    draft_id, _ = await make_draft(ex)
    r = await ex.execute("send_email", {"draft_id": draft_id}, task_id="t1")
    assert not r.ok and "sign in" in r.output.lower() and clicker.clicked == []


def test_policy_always_asks_for_email():
    _, send = email_tools()
    for level in ("ask-everything", "ask-risky", "autopilot"):
        d = PolicyEngine(Boundaries(autonomy=level)).evaluate(send, send.Args(draft_id="x"))
        assert d.verdict == Verdict.ASK


def test_compose_link_carries_the_exact_draft():
    from delta.tools.email import Draft

    url = compose_url(Draft("d1", ["a@x.com", "b@x.com"], "Hi & bye?", "Line 1\nLine 2 #3", bcc=["c@x.com"]))
    q = parse_qs(urlsplit(url).query)
    assert q["to"] == ["a@x.com,b@x.com"] and q["bcc"] == ["c@x.com"] and q["su"] == ["Hi & bye?"]
    assert q["body"] == ["Line 1\nLine 2 #3"] and q["view"] == ["cm"]


# --- through the Gmail app (a fake connected app with a send tool) ----------------------------------------------

class FakeOutcome:
    def __init__(self, ok: bool, text: str = "sent") -> None:
        self.ok, self.text = ok, text


class FakeConn:
    needs_reconnect = False
    tools: ClassVar[list[dict[str, Any]]] = [{"name": "send_email", "input_schema": {"properties": {
        "to": {"type": "array"}, "cc": {"type": "array"}, "subject": {"type": "string"}, "body": {"type": "string"}}}}]


class FakeSpec:
    id = "gmail"
    name = "Gmail"


class FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.store = type("S", (), {"get": staticmethod(lambda _id: FakeConn())})()

    def connected(self):
        return [(FakeSpec(), FakeConn())]

    async def call(self, app_id, tool, args, risk):
        self.calls.append((app_id, tool, args))
        return FakeOutcome(True)


async def test_a_connected_gmail_app_sends_after_approval(tmp_path):
    ex, _seen, opener, _clicker, send = setup(tmp_path, approve)
    send.connectors = type("A", (), {"manager": FakeManager()})()
    draft_id, _ = await make_draft(ex)
    r = await ex.execute("send_email", {"draft_id": draft_id}, task_id="t1")
    assert r.ok and r.evidence["via"] == "the Gmail app" and opener.urls == []
    app_id, tool, args = send.connectors.manager.calls[0]
    assert (app_id, tool) == ("gmail", "send_email") and args["to"] == ["ravi@example.com"]
    assert args["cc"] == ["meena@example.com"] and "shall we meet at 4" in args["body"]


async def test_an_app_that_can_only_draft_falls_back_to_the_browser(tmp_path):
    ex, _seen, opener, clicker, send = setup(tmp_path, approve)
    mgr = FakeManager()
    FakeConnDraft = type("C", (), {"needs_reconnect": False, "tools": [{"name": "create_draft", "input_schema": {
        "properties": {"to": {}, "subject": {}, "body": {}}}}]})
    mgr.connected = lambda: [(FakeSpec(), FakeConnDraft())]
    send.connectors = type("A", (), {"manager": mgr})()
    draft_id, _ = await make_draft(ex)
    r = await ex.execute("send_email", {"draft_id": draft_id}, task_id="t1")
    assert r.ok and mgr.calls == [] and opener.urls and clicker.clicked == ["Send"]


# --- the answer channels -----------------------------------------------------------------------------------------

async def test_edit_is_only_for_requests_that_offer_it():
    b = ApprovalBroker(timeout_s=1)
    req = ApprovalRequest("delete_path", "delete x", "destructive", "why")
    task = asyncio.create_task(b.request(req))
    await asyncio.sleep(0)
    assert b.edit(req.id, "change", "dashboard") == (False, "this request cannot be edited")
    b.resolve(req.id, False, "dashboard")
    assert (await task).approved is False


def test_dashboard_and_telegram_offer_edit():
    from pathlib import Path

    import delta

    root = Path(delta.__file__).parent
    chat = (root / "api" / "static" / "chat.js").read_text(encoding="utf-8")
    assert "a.can_edit" in chat and '"Edit"' in chat and '"Cancel"' in chat
    tg = (root / "channels" / "telegram.py").read_text(encoding="utf-8")
    assert ':e"' in tg and "self._edits" in tg and "approvals.edit(" in tg
