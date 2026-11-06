"""Telegram: the "/" menu (setMyCommands), /help with buttons, and old chats (/new, /chats, /open, /find).

The menu comes from delta.channels.menu.COMMANDS, so it lists exactly the commands the bot handles. It is set for
private chats on every start, but only written when it differs from what Telegram already has (idempotent). An
older list that would hide it is removed: the default-scope list earlier versions set (shown in every chat type)
and a per-chat list for a paired user (Telegram shows the most specific list, so a stale one there wins).
"""

from __future__ import annotations

from typing import Any

from delta.channels.base import is_allowed
from delta.channels.menu import PhoneChats, help_text, telegram_menu
from delta.system.logging import log

PATTERN = r"^cht:"


async def sync_menu(bot: Any, allowed: list[str]) -> bool:
    """Make Telegram's "/" menu the current list, scoped to private chats. True when anything was written."""
    from telegram import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat, BotCommandScopeDefault

    want = telegram_menu()
    changed = False
    try:
        private = BotCommandScopeAllPrivateChats()
        have = [(c.command, c.description) for c in await bot.get_my_commands(scope=private)]
        if have != want:
            await bot.set_my_commands([BotCommand(n, d) for n, d in want], scope=private)
            changed = True
    except Exception as e:  # cosmetic: the bot works without its menu
        log.warning("telegram.menu_failed", error=str(e)[:200])
        return False
    stale = [BotCommandScopeDefault(), *(BotCommandScopeChat(int(u)) for u in allowed if str(u).lstrip("-").isdigit())]
    for scope in stale:
        try:
            if await bot.get_my_commands(scope=scope):
                await bot.delete_my_commands(scope=scope)
                changed = True
        except Exception as e:
            log.warning("telegram.menu_cleanup_failed", scope=getattr(scope, "type", ""), error=str(e)[:200])
    try:  # the menu button opens the command list (not a web app someone set by hand)
        from telegram import MenuButtonCommands

        button = await bot.get_chat_menu_button()
        if getattr(button, "type", "commands") != "commands":
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
            changed = True
    except Exception as e:
        log.warning("telegram.menu_button_failed", error=str(e)[:200])
    log.info("telegram.menu_synced", commands=len(want), changed=changed)
    return changed


def attach(ch: Any) -> None:
    """Add /new, /chats, /open, /find and the cht: buttons to a TelegramChannel."""
    from telegram.ext import CallbackQueryHandler, CommandHandler

    ch.phone_chats = PhoneChats(ch.c, ch.name)

    def args(ctx: Any) -> str:
        return " ".join(getattr(ctx, "args", None) or [])

    async def new(update: Any, ctx: Any) -> None:
        await new_cmd(ch, update)

    async def chats(update: Any, ctx: Any) -> None:
        await chats_cmd(ch, update)

    async def open_(update: Any, ctx: Any) -> None:
        await open_cmd(ch, update, args(ctx))

    async def find(update: Any, ctx: Any) -> None:
        await find_cmd(ch, update, args(ctx))

    async def button(update: Any, ctx: Any) -> None:
        await on_button(ch, update)

    for name, handler in (("new", new), ("chats", chats), ("open", open_), ("find", find)):
        ch.app.add_handler(CommandHandler(name, handler))
    ch.app.add_handler(CallbackQueryHandler(button, pattern=PATTERN))


def help_buttons(ch: Any) -> Any:
    return ch._buttons([[("Recent chats", "cht:l"), ("New chat", "cht:n")]])


async def _ready(ch: Any, update: Any) -> str:
    """The sender's id when they are paired and remote control is not locked; "" otherwise."""
    uid = update.effective_user.id
    if not is_allowed(uid, ch.allowed):
        return ""
    if await ch._locked(update.message.reply_text):
        return ""
    return str(uid)


async def _html(message: Any, text: str, kb: Any = None) -> None:
    await message.reply_text(text, parse_mode="HTML", reply_markup=kb)


def _open_buttons(ch: Any, pairs: list[tuple[str, str]]) -> Any:
    return ch._buttons([[(label, f"cht:o:{cid}")] for label, cid in pairs]) if pairs else None


async def new_cmd(ch: Any, update: Any) -> None:
    if uid := await _ready(ch, update):
        ch.phone_chats.new(uid)
        await update.message.reply_text("New chat started. What should I do?")


async def chats_cmd(ch: Any, update: Any) -> None:
    if uid := await _ready(ch, update):
        text, pairs = ch.phone_chats.listing(uid, "telegram")
        await _html(update.message, text, _open_buttons(ch, pairs))


async def open_cmd(ch: Any, update: Any, ref: str) -> None:
    if uid := await _ready(ch, update):
        await _html(update.message, ch.phone_chats.open(uid, ref, "telegram"))


async def find_cmd(ch: Any, update: Any, words: str) -> None:
    if uid := await _ready(ch, update):
        text, pairs = ch.phone_chats.find(uid, words, "telegram")
        await _html(update.message, text, _open_buttons(ch, pairs))


async def on_button(ch: Any, update: Any) -> None:
    q = update.callback_query
    uid = str(q.from_user.id)
    if not is_allowed(uid, ch.allowed):
        await q.answer("not authorised")
        return
    await q.answer()
    say = q.message.chat.send_message
    if await ch._locked(say):
        return
    _, action, *rest = q.data.split(":", 2)
    if action == "o" and rest:
        await say(ch.phone_chats.reopen(uid, rest[0], "telegram"), parse_mode="HTML")
    elif action == "l":
        text, pairs = ch.phone_chats.listing(uid, "telegram")
        await say(text, parse_mode="HTML", reply_markup=_open_buttons(ch, pairs))
    elif action == "n":
        ch.phone_chats.new(uid)
        await say("New chat started. What should I do?")


HELP = help_text("telegram")
