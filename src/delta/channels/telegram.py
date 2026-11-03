"""Telegram channel (long polling, no public URL). Only allow-listed user ids are served.

Approvals arrive as messages with Approve/Deny buttons in each allowed user's private chat.

Pairing: instead of finding their numeric user id, the user sends the bot a one-time code shown in
Delta's dashboard (valid 10 minutes). Whoever sends it is added to the allow-list. The code only ever
appears on this PC's dashboard, so a bot token alone still controls nothing.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from typing import Any

from delta.bootstrap import Container
from delta.channels.base import approval_text, chunks, is_allowed, result_text
from delta.core.state import TaskRequest
from delta.safety.approvals import ApprovalRequest
from delta.system.logging import log


class TelegramChannel:
    name = "telegram"

    def __init__(self, c: Container, token: str, allowed: list[str]) -> None:
        from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

        self.c = c
        self.allowed = list(allowed)
        self.on_pair: Callable[[list[str]], None] | None = None  # persists the allow-list
        self._code: tuple[str, float] | None = None  # (pairing code, expires at)
        self.bot_name = ""
        # concurrent_updates: an approval button must work while a long task is still running
        self.app = Application.builder().token(token).concurrent_updates(True).build()
        self.app.add_handler(CommandHandler("start", self._start_cmd))
        self.app.add_handler(CommandHandler("status", self._status_cmd))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))
        self.app.add_handler(CallbackQueryHandler(self._on_button, pattern=r"^appr:"))

    async def start(self) -> None:
        self.c.approvals.add_presenter(self.name, self._present)
        await self.app.initialize()
        await self.app.start()
        assert self.app.updater
        await self.app.updater.start_polling(drop_pending_updates=True)
        self.bot_name = (await self.app.bot.get_me()).username or ""
        log.info("telegram.started", bot=self.bot_name)

    def new_pairing_code(self) -> str:
        code = f"{secrets.randbelow(900000) + 100000}"
        self._code = (code, time.monotonic() + 600)
        return code

    async def _try_pair(self, update: Any, text: str) -> None:
        """A not-yet-allowed user wrote: pair them if they sent the current code, else explain how."""
        uid = str(update.effective_user.id)
        code = self._code
        if code and time.monotonic() < code[1] and code[0] in (text or ""):
            self._code = None
            if uid not in self.allowed:
                self.allowed.append(uid)
            if self.on_pair:
                self.on_pair(self.allowed)
            self.c.audit.record("telegram.paired", user=uid)
            await update.message.reply_text(
                f"✅ Paired. This chat now controls Δ {self.c.settings.agent.name} on your PC. "
                "Tell me what to do; approvals will arrive here with Approve/Deny buttons.")
            return
        await update.message.reply_text(
            "🔒 Not connected yet. On your PC open Delta → Settings → Telegram → Connect my phone, "
            "then send me the 6-digit code shown there.")

    async def stop(self) -> None:
        if self.app.updater and self.app.updater.running:
            await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    async def _start_cmd(self, update: Any, ctx: Any) -> None:
        uid = update.effective_user.id
        if not is_allowed(uid, self.allowed):
            await self._try_pair(update, " ".join(ctx.args or []))  # "/start 123456" from a t.me link
            return
        await update.message.reply_text(f"Δ {self.c.settings.agent.name} here. Tell me what to do.")

    async def _status_cmd(self, update: Any, ctx: Any) -> None:
        if not is_allowed(update.effective_user.id, self.allowed):
            return
        g = self.c.governor.status()
        await update.message.reply_text(
            f"health {g['health']} · RAM {g['ram_pct']}% · GPU {g['gpu_temp_c']}°C · "
            f"models {', '.join(self.c.router.usable()) or 'none'}")

    async def _on_message(self, update: Any, ctx: Any) -> None:
        user = update.effective_user
        if not is_allowed(user.id, self.allowed):
            log.warning("telegram.rejected", user=user.id)
            await self._try_pair(update, update.message.text)
            return
        await update.message.chat.send_action("typing")
        res = await self.c.submit(TaskRequest(update.message.text, channel=self.name, user=str(user.id)))
        for part in chunks(result_text(res)):
            await update.message.reply_text(part)

    async def _present(self, req: ApprovalRequest) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"appr:{req.id}:y"),
                                    InlineKeyboardButton("⛔ Deny", callback_data=f"appr:{req.id}:n")]])
        for uid in self.allowed:
            await self.app.bot.send_message(int(uid), approval_text(req), reply_markup=kb)

    async def _on_button(self, update: Any, ctx: Any) -> None:
        q = update.callback_query
        if not is_allowed(q.from_user.id, self.allowed):
            await q.answer("not authorised")
            return
        _, req_id, answer = q.data.split(":")
        ok = self.c.approvals.resolve(req_id, answer == "y", by=f"telegram:{q.from_user.id}")
        await q.answer()
        verdict = ("✅ approved" if answer == "y" else "⛔ denied") if ok else "already answered or expired"
        await q.edit_message_text(f"{q.message.text}\n\n→ {verdict}")
