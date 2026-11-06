"""Telegram: interrupted tasks (delta.engine) with Resume / Discard buttons, /tasks, and /undo (put back the
files a task changed, delta.selfheal.undo; needs the PIN like Resume).

When Delta starts again after a crash or power cut, the owner who asked for an interrupted task from
Telegram gets one message about it, with buttons. Nothing resumes on its own. Resuming from the phone
follows delta.engine.resume: not while remote control is locked, only after /unlock with the PIN, never
above the permission level the task started with, and a step that was cut off is confirmed first.
"""

from __future__ import annotations

import contextlib
from html import escape
from typing import Any

from delta.channels.base import chunks, is_allowed, result_text
from delta.engine.resume import discard, prepare_resume
from delta.selfheal.actions import undo_task
from delta.system.logging import log

PATTERN = r"^tsk:"


def attach(ch: Any) -> None:
    """Add /tasks and the tsk: buttons to a TelegramChannel."""
    from telegram.ext import CallbackQueryHandler, CommandHandler

    async def command(update: Any, ctx: Any) -> None:
        await tasks_cmd(ch, update)

    async def button(update: Any, ctx: Any) -> None:
        await on_button(ch, update)

    async def undo(update: Any, ctx: Any) -> None:
        await undo_cmd(ch, update, list(getattr(ctx, "args", None) or []))

    ch.app.add_handler(CommandHandler("tasks", command))
    ch.app.add_handler(CommandHandler("undo", undo))
    ch.app.add_handler(CallbackQueryHandler(button, pattern=PATTERN))


def _card(task: dict[str, Any]) -> tuple[str, Any]:
    text = (f"I was interrupted while working on this:\n<i>{escape(task['text'][:300])}</i>\n"
            f"{task['steps']} step(s) had run. Resume continues from there; nothing that already changed something "
            "is done again without asking you. Resuming needs your PIN (/unlock).")
    return text, [[("Resume", f"tsk:r:{task['id']}"), ("Discard", f"tsk:d:{task['id']}")]]


async def announce_interrupted(c: Any) -> int:
    """Once per interrupted task: tell the Telegram user who asked for it. Returns messages sent."""
    ch = getattr(c, "live_channels", {}).get("telegram")
    engine = getattr(c, "engine", None)
    if ch is None or engine is None:
        return 0
    sent = 0
    for task in engine.interrupted():
        if task["channel"] != "telegram" or task.get("notified") or not is_allowed(task["user"], ch.allowed):
            continue
        text, rows = _card(task)
        try:
            await ch.app.bot.send_message(chat_id=int(task["user"]), text=text, parse_mode="HTML",
                                          reply_markup=ch._buttons(rows))
            engine.store.update(task["id"], notified=1)
            sent += 1
        except Exception as e:
            log.warning("telegram.interrupted_notice_failed", task=task["id"], error=str(e))
    return sent


async def tasks_cmd(ch: Any, update: Any) -> None:
    if not is_allowed(update.effective_user.id, ch.allowed):
        return
    if await ch._locked(update.message.reply_text):
        return
    engine = getattr(ch.c, "engine", None)
    if engine is None:
        await update.message.reply_text("The task engine is switched off on the PC.")
        return
    waiting = engine.interrupted(limit=5)
    recent = [t for t in engine.store.list(limit=8) if t["state"] != "interrupted"][:5]
    if not waiting and not recent:
        await update.message.reply_text("No tasks yet.")
        return
    for task in waiting:
        text, rows = _card(task)
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=ch._buttons(rows))
    if recent:
        lines = [f"{t['state'].replace('_', ' ')}: {escape(t['text'][:70])}" for t in recent]
        await update.message.reply_text("Recent tasks:\n" + "\n".join(lines), parse_mode="HTML")


def undoable_tasks(ch: Any, limit: int = 5) -> list[dict[str, Any]]:
    engine = getattr(ch.c, "engine", None)
    undo = getattr(getattr(getattr(ch.c, "orchestrator", None), "executor", None), "undo", None)
    if engine is None or undo is None:
        return []
    return [t for t in engine.store.list(limit=20) if undo.summary(t["id"])["available"]][:limit]


async def undo_cmd(ch: Any, update: Any, args: list[str]) -> None:
    """/undo <task id> restores; /undo alone lists recent tasks whose file changes can be undone."""
    uid = str(update.effective_user.id)
    if not is_allowed(update.effective_user.id, ch.allowed):
        return
    if await ch._locked(update.message.reply_text):
        return
    if args:
        result = undo_task(ch.c, args[0].strip(), ch.name, uid)
        await update.message.reply_text(result["message"])
        return
    tasks = undoable_tasks(ch)
    if not tasks:
        await update.message.reply_text("No recent task has file changes to undo.")
        return
    for t in tasks:
        await update.message.reply_text(f"Undo the file changes of:\n<i>{escape(t['text'][:200])}</i>",
                                        parse_mode="HTML", reply_markup=ch._buttons([[("Undo", f"tsk:u:{t['id']}")]]))


async def on_button(ch: Any, update: Any) -> None:
    q = update.callback_query
    uid = str(q.from_user.id)
    if not is_allowed(q.from_user.id, ch.allowed):
        await q.answer()
        return
    await q.answer()
    _, action, task_id = [*q.data.split(":", 2), "", ""][:3]
    say = q.message.chat.send_message
    if await ch._locked(say):
        return
    if action == "d":
        _ok, message = discard(ch.c, task_id, ch.name, uid)
        with contextlib.suppress(Exception):
            await q.edit_message_reply_markup(reply_markup=None)
        await say(message)
        return
    if action == "u":
        result = undo_task(ch.c, task_id, ch.name, uid)
        with contextlib.suppress(Exception):
            await q.edit_message_reply_markup(reply_markup=None)
        await say(result["message"])
        return
    confirm = {"y": "done", "n": "not_done"}.get(action)
    if action not in ("r", "y", "n"):
        return
    decision = prepare_resume(ch.c, task_id, ch.name, uid, confirm)
    if decision.needs_confirmation:
        rows = [[(o["label"][:60], f"tsk:{'y' if o['id'] == 'done' else 'n'}:{task_id}")] for o in decision.options]
        await say(decision.message, reply_markup=ch._buttons(rows))
        return
    if not decision.ok or decision.request is None:
        await say(decision.message)
        return
    with contextlib.suppress(Exception):
        await q.edit_message_reply_markup(reply_markup=None)
    await say("Resuming. I will message you when it is done.")
    res = await ch.c.submit(decision.request, autonomy_cap=decision.autonomy_cap)
    for part in chunks(result_text(res)):
        await ch._send(q.message, part)
