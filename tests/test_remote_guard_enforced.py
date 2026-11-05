"""Remote Guard enforced centrally: the executor (every tool), the approval broker (every button) and
task submission (every remote channel), not per channel. Fakes only: no Telegram, Slack or network."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from delta.config import Boundaries
from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.safety.approvals import ApprovalBroker, ApprovalRequest
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine
from delta.safety.remote_guard import RemoteGuard, is_remote, needs_remote_pin
from delta.tools.base import Risk, Tool, ToolResult
from delta.tools.filesystem import filesystem_tools
from delta.tools.registry import ToolExecutor, ToolRegistry


def make_guard(tmp_path: Path, pin: str | None = "4821") -> tuple[RemoteGuard, list]:
    store: dict = {}
    events: list = []
    audit = SimpleNamespace(record=lambda e, **k: events.append((e, k)))
    g = RemoteGuard(tmp_path / "guard", audit, alert=lambda t, x: None, get_secret=store.get,
                    set_secret=store.__setitem__)
    if pin:
        g.set_pin(pin, by="dashboard")
    return g, events


class NoArgs(BaseModel):
    pass


class Screen(Tool[NoArgs]):
    """Stands in for look_at_screen: READ risk, auto-allowed by policy."""
    name, description, risk, Args = "look_at_screen", "see the screen", Risk.READ, NoArgs

    async def run(self, args):
        return ToolResult(True, "a bank statement is open")


def executor(tmp_path: Path, guard: RemoteGuard | None) -> ToolExecutor:
    ok = tmp_path / "ok"
    ok.mkdir(exist_ok=True)
    tools = [*filesystem_tools(), Screen()]
    ex = ToolExecutor(ToolRegistry(tools), PolicyEngine(Boundaries(allowed_paths=[str(ok)])),
                      ApprovalBroker(timeout_s=0.05), AuditLog(tmp_path / "audit.jsonl"))
    ex.guard = guard
    return ex


async def run_as(ex: ToolExecutor, channel: str, user: str, tool: str, args: dict) -> ToolResult:
    token = CURRENT_REQUEST.set(TaskRequest("x", channel=channel, user=user))
    try:
        return await ex.execute(tool, args, "t1")
    finally:
        CURRENT_REQUEST.reset(token)


def test_every_channel_except_the_pc_is_remote():
    assert not is_remote("cli") and not is_remote("api") and not is_remote("dashboard")
    assert is_remote("telegram") and is_remote("slack") and is_remote("discord") and is_remote("")
    assert needs_remote_pin("read_file") and needs_remote_pin("look_at_screen")
    assert needs_remote_pin("get_clipboard")  # a future clipboard tool is covered by name
    assert needs_remote_pin("list_dir") is None and needs_remote_pin("list_dir", outside_allowed=True)
    assert needs_remote_pin("open_app") is None


@pytest.mark.parametrize("channel", ["telegram", "slack", "whatsapp"])
async def test_sensitive_reads_from_a_phone_need_the_pin(tmp_path, channel):
    g, events = make_guard(tmp_path)
    ex = executor(tmp_path, g)
    secret = tmp_path / "ok" / "passwords.txt"
    secret.write_text("hunter2")

    r = await run_as(ex, channel, "5", "read_file", {"path": str(secret)})
    assert not r.ok and "hunter2" not in r.output and r.evidence["needs_pin"] and "PIN" in r.output
    r = await run_as(ex, channel, "5", "look_at_screen", {})
    assert not r.ok and "bank" not in r.output
    assert ("guard.blocked", {"tool": "read_file", "channel": channel, "user": "5", "why": "needs_pin"}) in events

    # The same reads from the PC are untouched.
    assert (await run_as(ex, "cli", "owner", "read_file", {"path": str(secret)})).ok
    assert (await run_as(ex, "api", "owner", "look_at_screen", {})).ok

    # After the PIN: allowed for that person only, and audit-logged.
    assert g.try_pin("5", "4821")[0]
    assert "hunter2" in (await run_as(ex, channel, "5", "read_file", {"path": str(secret)})).output
    assert not (await run_as(ex, channel, "6", "read_file", {"path": str(secret)})).ok
    assert ("guard.sensitive", {"action": "read_file", "channel": channel, "user": "5"}) in events


async def test_listing_needs_the_pin_only_outside_allowed_folders(tmp_path):
    g, _ = make_guard(tmp_path)
    ex = executor(tmp_path, g)
    outside = tmp_path / "private"
    outside.mkdir()
    assert (await run_as(ex, "telegram", "5", "list_dir", {"path": str(tmp_path / "ok")})).ok
    r = await run_as(ex, "telegram", "5", "list_dir", {"path": str(outside)})
    assert not r.ok and "outside your allowed folders" in r.output


async def test_no_pin_set_means_refused_with_how_to_set_one(tmp_path):
    g, _ = make_guard(tmp_path, pin=None)
    r = await run_as(executor(tmp_path, g), "telegram", "5", "look_at_screen", {})
    assert not r.ok and "Set a remote PIN on the PC" in r.output


async def test_slack_is_told_it_cannot_take_the_pin(tmp_path):
    g, _ = make_guard(tmp_path)
    r = await run_as(executor(tmp_path, g), "slack", "U1", "look_at_screen", {})
    assert not r.ok and "Slack cannot take the PIN" in r.output


async def test_locked_blocks_every_remote_tool_even_harmless_ones(tmp_path):
    g, events = make_guard(tmp_path)
    ex = executor(tmp_path, g)
    assert g.try_pin("5", "4821")[0]
    g.lock("locked from Telegram by the owner", by="telegram:5")
    r = await run_as(ex, "telegram", "5", "list_dir", {"path": str(tmp_path / "ok")})
    assert not r.ok and "locked" in r.output
    assert any(e == "guard.blocked" and k.get("why") == "locked" for e, k in events)
    assert (await run_as(ex, "cli", "owner", "list_dir", {"path": str(tmp_path / "ok")})).ok  # the PC still works


# --- approvals -------------------------------------------------------------------------------------------
async def pending(broker: ApprovalBroker, risk: str = "destructive", tool: str = "delete_path"):
    task = asyncio.create_task(broker.request(ApprovalRequest(tool=tool, summary="delete x", risk=risk,
                                                              reason="r")))
    await asyncio.sleep(0)
    return task, broker.pending()[0].id


@pytest.mark.parametrize("by", ["telegram:5", "slack:U1", "discord:9"])
async def test_a_locked_remote_approval_is_a_logged_no(tmp_path, by):
    g, events = make_guard(tmp_path)
    broker = ApprovalBroker(timeout_s=2)
    broker.guard = g
    g.lock("locked from Telegram by the owner", by="telegram:5")
    task, rid = await pending(broker, risk="write")
    settled, message = broker.answer(rid, True, by=by)
    outcome = await task
    assert settled and not outcome.approved and "locked" in message
    assert not outcome.human and "remote control locked" in outcome.by  # not a choice the user made
    assert any(e == "guard.approval_blocked" and k["why"] == "locked" and k["by"] == by for e, k in events)


async def test_the_pc_can_still_approve_while_remote_is_locked(tmp_path):
    g, _ = make_guard(tmp_path)
    broker = ApprovalBroker(timeout_s=2)
    broker.guard = g
    g.lock("x", by="dashboard")
    task, rid = await pending(broker)
    assert broker.resolve(rid, True, by="dashboard")
    assert (await task).approved


async def test_approving_a_destructive_action_from_a_phone_needs_the_pin(tmp_path):
    g, _ = make_guard(tmp_path)
    broker = ApprovalBroker(timeout_s=2)
    broker.guard = g
    task, rid = await pending(broker, risk="destructive")
    settled, message = broker.answer(rid, True, by="telegram:5")
    assert not settled and "/unlock" in message and not task.done()  # still open: unlock, then tap again
    assert g.try_pin("5", "4821")[0]
    assert broker.answer(rid, True, by="telegram:5") == (True, "approved")
    assert (await task).approved


async def test_saying_no_from_a_phone_never_needs_the_pin(tmp_path):
    g, _ = make_guard(tmp_path)
    broker = ApprovalBroker(timeout_s=2)
    broker.guard = g
    task, rid = await pending(broker, risk="external")
    assert broker.answer(rid, False, by="slack:U1") == (True, "denied")
    assert not (await task).approved


async def test_low_risk_approvals_from_a_phone_need_no_pin(tmp_path):
    g, _ = make_guard(tmp_path)
    broker = ApprovalBroker(timeout_s=2)
    broker.guard = g
    task, rid = await pending(broker, risk="write", tool="write_file")
    assert broker.answer(rid, True, by="telegram:5") == (True, "approved")
    assert (await task).approved


async def test_a_lock_no_does_not_teach_the_learning_store(tmp_path):
    """A 'no' forced by the lock is not the user's choice, so it must not become a learned 'avoid'."""
    g, _ = make_guard(tmp_path)
    ex = executor(tmp_path, None)  # a PC task: the executor's own remote check does not apply
    ex.approvals.guard = g
    recorded: list = []
    ex.learning = SimpleNamespace(is_active=lambda *a: None, list=lambda **k: [],
                                  record_decision=lambda *a: recorded.append(a))
    g.lock("x", by="telegram:5")

    async def phone(req):
        ex.approvals.answer(req.id, True, by="telegram:5")

    ex.approvals.add_presenter("telegram", phone)
    ex.approvals.timeout_s = 2
    victim = tmp_path / "elsewhere.txt"
    victim.write_text("keep")
    r = await ex.execute("delete_path", {"path": str(victim)}, "t1")
    assert not r.ok and victim.exists() and recorded == []


# --- task submission: /lock stops remote tasks before any AI or tool runs -----------------------------------
async def test_submit_refuses_remote_tasks_while_locked(tmp_path):
    from delta.bootstrap import Container

    g, events = make_guard(tmp_path)
    ran: list = []

    async def run(req):
        ran.append(req.channel)
        return SimpleNamespace(status="done")

    c = Container.__new__(Container)
    c.guard, c.audit, c.orchestrator = g, g.audit, SimpleNamespace(run=run)
    c._task_lock, c.running = asyncio.Semaphore(1), None
    g.lock("locked from Telegram by the owner", by="telegram:5")
    for channel in ("telegram", "slack", "discord"):
        res = await c.submit(TaskRequest("read my notes", channel=channel, user="5"))
        assert res.status == "failed" and "locked" in res.answer
    assert ran == []
    assert (await c.submit(TaskRequest("hi", channel="cli"))).status == "done" and ran == ["cli"]
    assert sum(e == "guard.blocked" for e, _ in events) == 3


# --- the real channels' buttons go through the broker -------------------------------------------------------
async def test_telegram_approval_button_under_lock_is_a_no(tmp_path):
    pytest.importorskip("telegram")
    from delta.channels.telegram import TelegramChannel

    g, _ = make_guard(tmp_path)
    broker = ApprovalBroker(timeout_s=2)
    broker.guard = g
    ch = TelegramChannel(SimpleNamespace(audit=g.audit, guard=g, approvals=broker),  # type: ignore[arg-type]
                         "123456:TEST-TOKEN", ["5"])
    task, rid = await pending(broker)
    g.lock("locked from Telegram by the owner", by="telegram:5")
    shown: list = []

    async def answer(text=None, **kw):
        shown.append(("answer", text))

    async def edit(text, **kw):
        shown.append(("edit", text))

    q = SimpleNamespace(from_user=SimpleNamespace(id=5), data=f"appr:{rid}:y", answer=answer,
                        edit_message_text=edit, message=SimpleNamespace(text="Delete x?"))
    await ch._on_button(SimpleNamespace(callback_query=q), None)
    assert not (await task).approved
    assert any(kind == "edit" and "locked" in text for kind, text in shown)


async def test_telegram_approve_without_pin_keeps_the_buttons(tmp_path):
    pytest.importorskip("telegram")
    from delta.channels.telegram import TelegramChannel

    g, _ = make_guard(tmp_path)
    broker = ApprovalBroker(timeout_s=2)
    broker.guard = g
    ch = TelegramChannel(SimpleNamespace(audit=g.audit, guard=g, approvals=broker),  # type: ignore[arg-type]
                         "123456:TEST-TOKEN", ["5"])
    task, rid = await pending(broker)
    shown: list = []

    async def answer(text=None, **kw):
        shown.append(("answer", text, kw.get("show_alert")))

    async def edit(text, **kw):
        shown.append(("edit", text, None))

    q = SimpleNamespace(from_user=SimpleNamespace(id=5), data=f"appr:{rid}:y", answer=answer,
                        edit_message_text=edit, message=SimpleNamespace(text="Delete x?"))
    await ch._on_button(SimpleNamespace(callback_query=q), None)
    assert not task.done() and shown[0][0] == "answer" and "PIN" in shown[0][1] and shown[0][2]
    assert not any(kind == "edit" for kind, *_ in shown)
    broker.resolve(rid, False, by="dashboard")
    await task


async def test_slack_approval_button_under_lock_is_a_no(tmp_path):
    pytest.importorskip("slack_bolt")
    from delta.channels.slack import SlackChannel

    g, _ = make_guard(tmp_path)
    broker = ApprovalBroker(timeout_s=2)
    broker.guard = g
    ch = SlackChannel.__new__(SlackChannel)  # no Socket Mode connection in a test
    ch.c, ch.allowed = SimpleNamespace(guard=g, approvals=broker), ["U1"]  # type: ignore[assignment]
    task, rid = await pending(broker, risk="external", tool="send_message")
    g.lock("locked on the PC", by="dashboard")
    updates: list = []

    async def ack():
        return None

    async def chat_update(**kw):
        updates.append(kw["text"])

    async def chat_post(**kw):
        updates.append(kw["text"])

    client = SimpleNamespace(chat_update=chat_update, chat_postMessage=chat_post)
    body = {"user": {"id": "U1"}, "actions": [{"action_id": "delta_approve", "value": rid}],
            "channel": {"id": "D1"}, "message": {"ts": "1", "text": "Send?"}}
    await ch._on_action(ack, body, client)
    assert not (await task).approved and "locked" in updates[-1]


async def test_slack_approve_without_pin_is_refused_and_request_stays_open(tmp_path):
    pytest.importorskip("slack_bolt")
    from delta.channels.slack import SlackChannel

    g, _ = make_guard(tmp_path)
    broker = ApprovalBroker(timeout_s=2)
    broker.guard = g
    ch = SlackChannel.__new__(SlackChannel)
    ch.c, ch.allowed = SimpleNamespace(guard=g, approvals=broker), ["U1"]  # type: ignore[assignment]
    task, rid = await pending(broker, risk="destructive")
    posted: list = []

    async def ack():
        return None

    async def chat_update(**kw):
        posted.append(("update", kw["text"]))

    async def chat_post(**kw):
        posted.append(("post", kw["text"]))

    client = SimpleNamespace(chat_update=chat_update, chat_postMessage=chat_post)
    body = {"user": {"id": "U1"}, "actions": [{"action_id": "delta_approve", "value": rid}],
            "channel": {"id": "D1"}, "message": {"ts": "1", "text": "Delete?"}}
    await ch._on_action(ack, body, client)
    assert not task.done() and posted == [("post", posted[0][1])] and "Slack cannot take the PIN" in posted[0][1]
    broker.resolve(rid, False, by="dashboard")
    await task
