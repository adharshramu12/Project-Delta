"""Telegram: saved logins and watchers, read-only.

  /logins     the saved logins: name, site and permission. Never a user name or a password. Adding or changing a
              login is only possible on the PC (Settings > Logins or `delta logins add`): a password never travels
              through Telegram. Using a login from the phone (asking Delta to sign in somewhere) needs the PIN.
  /watchers   the sites Delta watches, and how the last check went.
"""

from __future__ import annotations

from html import escape
from typing import Any

from delta.channels.base import is_allowed

PC_ONLY = ("Logins are added on the PC only (Settings > Logins, or <code>delta logins add</code>): a password never "
           "goes through Telegram. Asking me to sign in somewhere from here needs your PIN (/unlock).")


def attach(ch: Any) -> None:
    from telegram.ext import CommandHandler

    async def logins(update: Any, ctx: Any) -> None:
        await logins_cmd(ch, update)

    async def watchers(update: Any, ctx: Any) -> None:
        await watchers_cmd(ch, update)

    ch.app.add_handler(CommandHandler("logins", logins))
    ch.app.add_handler(CommandHandler("watchers", watchers))


def _locked(ch: Any) -> str:
    guard = getattr(ch.c, "guard", None)
    return guard.locked_message() if guard is not None and guard.locked else ""


async def logins_cmd(ch: Any, update: Any) -> None:
    if not is_allowed(update.effective_user.id, ch.allowed):
        return
    if why := _locked(ch):
        await update.message.reply_text(why)
        return
    svc = getattr(ch.c, "logins", None)
    items = svc.store.list() if svc is not None else []
    words = {"ask": "asks every time", "always": "always allowed"}
    lines = [f"<b>{escape(x.name)}</b>: {escape(x.origin)} ({words.get(x.permission, x.permission)})" for x in items]
    text = "<b>Saved logins</b>\n" + ("\n".join(lines) if lines else "None yet.") + "\n\n" + PC_ONLY
    await update.message.reply_text(text, parse_mode="HTML")


async def watchers_cmd(ch: Any, update: Any) -> None:
    if not is_allowed(update.effective_user.id, ch.allowed):
        return
    if why := _locked(ch):
        await update.message.reply_text(why)
        return
    svc = getattr(ch.c, "watchers", None)
    items = svc.store.list() if svc is not None else []
    lines = [f"<b>{escape(w.name)}</b>: every {w.interval_minutes // 60} h; last check: "
             f"{escape(w.last_status or 'not yet')} {escape(w.last_message[:120])}" for w in items]
    text = ("<b>Watchers</b>\n" + ("\n".join(lines) if lines else "None yet.")
            + "\n\nSay \"watch my college portal for exams\" to add one (needs your PIN from here).")
    await update.message.reply_text(text, parse_mode="HTML")
