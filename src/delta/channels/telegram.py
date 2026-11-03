"""Telegram channel (long polling, no public URL). Only allow-listed user ids are served.

Approvals arrive as messages with Approve/Deny buttons in each allowed user's private chat.

Pairing: instead of finding their numeric user id, the user sends the bot a one-time code shown in
Delta's dashboard (valid 10 minutes). Whoever sends it is added to the allow-list. The code only ever
appears on this PC's dashboard, so a bot token alone still controls nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from delta.bootstrap import Container
from delta.channels.base import approval_text, chunks, is_allowed, result_text, to_html
from delta.channels.pc_menu import PCMenu
from delta.core.state import TaskRequest
from delta.safety.approvals import ApprovalRequest
from delta.system.logging import log

HELP = (
    "<b>Talk to me normally</b> to get anything done on your PC: \"play ramadasu songs\", "
    "\"find the biggest files in Downloads\", \"open notebooklm and make a notebook\"...\n\n"
    "<b>Instant controls</b> (no AI, no tokens):\n"
    "/ai - see or switch the main AI\n"
    "/model - pick the model for the main AI\n"
    "/mode - permission level (Careful, Balanced, Trusted, Autopilot)\n"
    "/keys - replace an API key (when credits or limits run out)\n\n"
    "<b>Your PC</b>:\n"
    "/volume - mute, louder, quieter (or /volume 30)\n"
    "/brightness - screen brightness (laptop screens)\n"
    "/theme - Windows light or dark mode\n"
    "/apps - what is open, tap to close it\n"
    "/screen - a screenshot of the PC now\n"
    "/camera - a photo from the webcam of whoever is at the PC\n\n"
    "/stop - stop what I'm doing now\n"
    "/unlock - enter your PIN (camera, screen, files, closing apps, keys need it)\n"
    "/lock - lock all remote control now (only the PC can unlock it)\n"
    "/status - PC health and AIs available\n"
    "/help - this list\n\n"
    "🎙 You can also just <b>send a voice note</b>: Telugu, Hindi or English."
)


class TelegramChannel(PCMenu):
    name = "telegram"

    def __init__(self, c: Container, token: str, allowed: list[str]) -> None:
        from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

        self.c = c
        self.allowed = list(allowed)
        self.on_pair: Callable[[list[str]], None] | None = None  # persists the allow-list
        self._code: tuple[str, float] | None = None  # (pairing code, expires at)
        self.bot_name = ""
        self._choices: dict[str, list[str]] = {}  # the choices last offered to each user (tap buttons)
        self._forms: dict[str, dict[str, Any]] = {}  # user -> open question form (delta.core.questions)
        self._awaiting_key: dict[str, tuple[str, float]] = {}  # user -> (key name, until) after /keys
        self._pending_pin: dict[str, tuple[Any, str, float]] = {}  # user -> (action to run, what, until)
        self._pc_init()
        # concurrent_updates: an approval button must work while a long task is still running
        self.app = Application.builder().token(token).concurrent_updates(True).build()
        self.app.add_handler(CommandHandler("start", self._start_cmd))
        self.app.add_handler(CommandHandler("status", self._status_cmd))
        self.app.add_handler(CommandHandler("help", self._help_cmd))
        self.app.add_handler(CommandHandler("ai", self._ai_cmd))
        self.app.add_handler(CommandHandler("model", self._model_cmd))
        self.app.add_handler(CommandHandler("mode", self._mode_cmd))
        self.app.add_handler(CommandHandler("stop", self._stop_cmd))
        self.app.add_handler(CommandHandler("keys", self._keys_cmd))
        self.app.add_handler(CommandHandler("lock", self._lock_cmd))
        self.app.add_handler(CommandHandler("unlock", self._unlock_cmd))
        for name, handler in (("volume", self._volume_cmd), ("brightness", self._brightness_cmd),
                              ("theme", self._theme_cmd),
                              ("apps", self._apps_cmd), ("screen", self._screen_cmd),
                              ("camera", self._camera_cmd)):
            self.app.add_handler(CommandHandler(name, handler))
        self.app.add_handler(CallbackQueryHandler(self._on_pc_button, pattern=r"^sys:"))
        self.app.add_handler(CallbackQueryHandler(self._on_control, pattern=r"^ctl:"))
        self.app.add_handler(CallbackQueryHandler(self._on_answer, pattern=r"^ans:"))
        self.app.add_handler(CallbackQueryHandler(self._on_form, pattern=r"^f[qd]:"))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))
        self.app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self._on_voice))
        self.app.add_handler(CallbackQueryHandler(self._on_button, pattern=r"^appr:"))

    async def start(self) -> None:
        self.c.approvals.add_presenter(self.name, self._present)
        await self.app.initialize()
        await self.app.start()
        assert self.app.updater
        await self.app.updater.start_polling(drop_pending_updates=True)
        self.bot_name = (await self.app.bot.get_me()).username or ""
        self._menu_task = asyncio.create_task(self._set_menu())  # cosmetic: not part of the start-up timeout

    async def _set_menu(self) -> None:
        with contextlib.suppress(Exception):  # the "/" menu in the chat
            await self.app.bot.set_my_commands([
                ("ai", "See or switch the main AI"), ("model", "Pick the model"),
                ("mode", "Permission level"), ("keys", "Replace an API key"), ("stop", "Stop the running task"),
                ("lock", "Lock remote control now"), ("unlock", "Enter your PIN"),
                ("volume", "PC volume"), ("brightness", "Screen brightness"), ("theme", "Windows light / dark mode"),
                ("apps", "Open apps, tap to close"),
                ("screen", "Screenshot of the PC"), ("camera", "Webcam photo"),
                ("status", "PC health and AIs"), ("help", "What I can do")])
        log.info("telegram.started", bot=self.bot_name)

    async def send_file(self, user: str, path: Path, caption: str = "") -> None:
        """A file from this PC into the user's chat. Photos show inline; everything else is a document
        (full quality, any type)."""
        with path.open("rb") as f:
            if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} and path.stat().st_size < 10e6:
                await self.app.bot.send_photo(int(user), f, caption=caption[:1000])
            else:
                await self.app.bot.send_document(int(user), f, filename=path.name, caption=caption[:1000])

    async def reply(self, update: Any, text: str, **kw: Any) -> None:
        """Formatted (bold, code, links); plain text if Telegram rejects the markup."""
        try:
            await update.message.reply_text(to_html(text), parse_mode="HTML", **kw)
        except Exception:
            await update.message.reply_text(text, **kw)

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
                "Tell me what to do; approvals will arrive here with Approve/Deny buttons.\n\n"
                "🛡 Protect this: whoever has your Telegram can talk to your PC. Turn on Telegram's "
                "Two-Step Verification (Settings → Privacy and Security → Two-Step Verification), and set a "
                "Delta PIN on the PC (Settings → Remote security) for the camera, screen, files and keys.")
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

    # --- instant controls: no AI call, same rules as the dashboard (delta.control) ------------------
    @staticmethod
    def _buttons(rows: list[list[tuple[str, str]]]) -> Any:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])

    async def _help_cmd(self, update: Any, ctx: Any) -> None:
        if is_allowed(update.effective_user.id, self.allowed):
            await update.message.reply_text(HELP, parse_mode="HTML")

    def _ai_menu(self) -> tuple[str, Any]:
        from delta.control import main_ai_choices

        choices = main_ai_choices(self.c)
        rows = [[(f"{'✅ ' if a['current'] else ''}{a['name']}{'' if a['ready'] else ' (not set up)'}",
                  f"ctl:ai:{a['name']}")] for a in choices]
        return (f"Main AI now: <b>{self.c.router.preferred}</b>. Tap to switch:", self._buttons(rows))

    async def _ai_cmd(self, update: Any, ctx: Any) -> None:
        if not is_allowed(update.effective_user.id, self.allowed):
            return
        if ctx.args:  # /ai codex
            await self._apply(update.message.reply_text, "ai", ctx.args[0].lower(), update.effective_user.id)
            return
        text, kb = self._ai_menu()
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

    async def _model_cmd(self, update: Any, ctx: Any) -> None:
        if not is_allowed(update.effective_user.id, self.allowed):
            return
        from delta.control import model_choices

        name = self.c.router.preferred
        opts = model_choices(self.c, name)[:20]
        if not opts:
            await update.message.reply_text(f"{name} has no model choice here. Use /ai to pick another AI.")
            return
        p = self.c.router.providers.get(name)
        now = p.model_for("chat") if p and hasattr(p, "model_for") else ""
        rows = [[(f"{'✅ ' if o['id'] == now else ''}{o.get('label') or o['id']}"[:60], f"ctl:model:{o['id']}"[:64])]
                for o in opts]
        await update.message.reply_text(f"Model for <b>{name}</b>. Tap to switch:", parse_mode="HTML",
                                        reply_markup=self._buttons(rows))

    async def _mode_cmd(self, update: Any, ctx: Any) -> None:
        if not is_allowed(update.effective_user.id, self.allowed):
            return
        from delta.control import LEVEL_HELP, LEVELS

        now = self.c.settings.boundaries.autonomy
        rows = [[(f"{'✅ ' if k == now else ''}{v}: {LEVEL_HELP[k]}"[:60], f"ctl:mode:{k}")] for k, v in LEVELS.items()]
        await update.message.reply_text("Permission level. Tap to switch:", reply_markup=self._buttons(rows))

    async def _stop_cmd(self, update: Any, ctx: Any) -> None:
        if not is_allowed(update.effective_user.id, self.allowed):
            return
        from delta.control import stop_running

        stopped = stop_running(self.c, by=f"telegram:{update.effective_user.id}")
        await update.message.reply_text("⏹ Stopped." if stopped else "Nothing is running right now.")

    async def _apply(self, say: Any, kind: str, value: str, uid: int) -> None:
        from delta.control import set_autonomy, set_main_ai, set_model

        by = f"telegram:{uid}"
        try:
            if kind == "ai":
                await say(f"✅ Main AI is now {set_main_ai(self.c, value, by)}.")
            elif kind == "model":
                set_model(self.c, self.c.router.preferred, value, by)
                await say(f"✅ {self.c.router.preferred} now uses {value}.")
            elif kind == "mode" and value == "autopilot":
                async def go() -> None:
                    await say(f"✅ Permission level: {set_autonomy(self.c, value, by)}.")
                await self._guarded(str(uid), "autopilot", say, go)
            elif kind == "mode":
                await say(f"✅ Permission level: {set_autonomy(self.c, value, by)}.")
            elif kind == "key":
                async def ask_key() -> None:
                    self._awaiting_key[str(uid)] = (value, time.monotonic() + 300)
                    await say("Send the new key as your next message (within 5 minutes). I'll delete it from "
                              "this chat right after reading it, test it, and keep your old key if it doesn't work.")
                await self._guarded(str(uid), "keys", say, ask_key)
        except ValueError as e:
            await say(f"✗ {e}")

    async def _on_control(self, update: Any, ctx: Any) -> None:
        q = update.callback_query
        if not is_allowed(q.from_user.id, self.allowed):
            await q.answer("not authorised")
            return
        _, kind, value = q.data.split(":", 2)
        await q.answer()
        await self._apply(q.edit_message_text, kind, value, q.from_user.id)

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
        if await self._take_pin(update, str(user.id)):
            return
        if await self._locked(update.message.reply_text):
            return
        if await self._take_key(update, str(user.id)):
            return
        if (form := self._forms.pop(str(user.id), None)) is not None:
            from delta.core.questions import answers_text

            text = answers_text(form["questions"], form["answers"]) + "\nAlso: " + update.message.text
            await self._run(update, str(user.id), text)
            return
        await self._run(update, str(user.id), update.message.text)

    # --- Remote Guard: PIN for sensitive actions, panic lock (delta.safety.remote_guard) -------------
    def _guard(self) -> Any:
        return getattr(self.c, "guard", None)

    async def _locked(self, say: Any) -> bool:
        g = self._guard()
        if g is not None and g.locked:
            await say(g.locked_message())
            return True
        return False

    async def _guarded(self, uid: str, action: str, say: Any, run: Any) -> None:
        """Run a sensitive action only for an owner who proved the PIN (in the last 15 minutes). Otherwise
        ask for it; the action runs right after a correct PIN."""
        from delta.safety.remote_guard import SENSITIVE
        from delta.system.notify import notify

        g = self._guard()
        if g is None:
            await run()
            return
        if g.locked:
            await say(g.locked_message())
            return
        if not g.has_pin:
            await say(f"\U0001f510 To {SENSITIVE[action]} from your phone, first set a remote PIN on the PC: "
                      "Delta \u2192 Settings \u2192 Remote security.")
            return
        if g.is_unlocked(uid):
            notify("\U0001f4f1 Delta: remote action", f"From Telegram: {SENSITIVE[action]}")
            self.c.audit.record("guard.sensitive", action=action, user=uid)
            await run()
            return
        self._pending_pin[uid] = (run, action, time.monotonic() + 120)
        await say(f"\U0001f510 Enter your Delta PIN to {SENSITIVE[action]}. I delete it from this chat right away.")

    async def _take_pin(self, update: Any, uid: str) -> bool:
        """The message after a PIN request is the PIN: deleted from the chat at once, then checked."""
        pending = self._pending_pin.get(uid)
        if not pending:
            return False
        run, action, until = pending
        if time.monotonic() > until:
            self._pending_pin.pop(uid, None)
            return False
        pin = (update.message.text or "").strip()
        with contextlib.suppress(Exception):
            await update.message.delete()
        g = self._guard()
        ok, message = g.try_pin(uid, pin)
        chat = update.message.chat
        if ok:
            self._pending_pin.pop(uid, None)
            await chat.send_message("\u2705 " + message)
            await self._guarded(uid, action, chat.send_message, run)
        else:
            if g.locked:
                self._pending_pin.pop(uid, None)
            await chat.send_message("\u2717 " + message)
        return True

    async def _lock_cmd(self, update: Any, ctx: Any) -> None:
        if not is_allowed(update.effective_user.id, self.allowed) or self._guard() is None:
            return
        self._guard().lock("locked from Telegram by the owner", by=f"telegram:{update.effective_user.id}")
        self._pending_pin.clear()
        await update.message.reply_text("\U0001f512 Remote control is locked. Only someone at the PC can unlock it "
                                        "(Delta \u2192 Settings \u2192 Remote security).")

    async def _unlock_cmd(self, update: Any, ctx: Any) -> None:
        if not is_allowed(update.effective_user.id, self.allowed):
            return
        uid = str(update.effective_user.id)

        async def done() -> None:
            await update.message.chat.send_message("Sensitive actions are open for 15 minutes.")
        await self._guarded(uid, "unlock", update.message.reply_text, done)

    async def _keys_cmd(self, update: Any, ctx: Any) -> None:
        if not is_allowed(update.effective_user.id, self.allowed):
            return
        from delta.control import key_status

        keys = key_status()
        lines = "\n".join(f"{k['label']}: {k['masked']}" for k in keys)
        rows = [[(f"Replace {k['label'].split(' (')[0]}", f"ctl:key:{k['name']}")] for k in keys]
        await update.message.reply_text(f"Your keys:\n{lines}\n\nTap one to replace it.",
                                        reply_markup=self._buttons(rows))

    async def _take_key(self, update: Any, uid: str) -> bool:
        """The message right after 'Replace <key>' is the new key: delete it from the chat at once (a bot
        chat is not end-to-end encrypted), test it, save it only if it works. True if it was a key."""
        pending = self._awaiting_key.pop(uid, None)
        if not pending or time.monotonic() > pending[1]:
            return False
        from delta.control import replace_key

        value = (update.message.text or "").strip()
        with contextlib.suppress(Exception):
            await update.message.delete()
        ok, message = await replace_key(self.c, pending[0], value, by=f"telegram:{uid}")
        await update.message.chat.send_message(("\u2705 " if ok else "\u2717 ") + message
                                               + ("\n(I deleted your message so the key does not stay in this chat.)"))
        return True

    async def _on_voice(self, update: Any, ctx: Any) -> None:
        """A voice note: turned into text, shown back (so a mishearing is visible), then done like a typed
        request."""
        from delta.channels.voice import VoiceError, transcribe

        user = update.effective_user
        if not is_allowed(user.id, self.allowed):
            return
        if await self._locked(update.message.reply_text):
            return
        media = update.message.voice or update.message.audio
        await update.message.chat.send_action("typing")
        try:
            audio = bytes(await (await media.get_file()).download_as_bytearray())
            text = await transcribe(audio, getattr(media, "file_name", None) or "voice.ogg")
        except VoiceError as e:
            await update.message.reply_text(f"🎙 {e}")
            return
        except Exception as e:
            log.warning("telegram.voice_failed", error=str(e))
            await update.message.reply_text("🎙 I couldn't get that voice note. Please try again, or type it.")
            return
        self.c.audit.record("telegram.voice", user=str(user.id), chars=len(text))
        await update.message.reply_text(f"🎙 I heard: {text}")
        await self._run(update, str(user.id), text)

    def _chat(self, uid: str) -> str:
        """This person's Telegram conversation (with memory, also in the dashboard sidebar)."""
        chats = getattr(self.c, "chats", None)
        if chats is None:
            return ""
        title = f"Telegram {uid}"
        found = next((ch for ch in chats.list(title) if ch.title == title), None)
        return (found or chats.create(title)).id

    async def _run(self, update: Any, uid: str, text: str) -> None:
        """Do what was asked. The chat's recent turns go along, so an answer like "the second one" means
        something; a question comes back with tap buttons for its choices."""
        message = update.message or update.callback_query.message
        await message.chat.send_action("typing")
        from delta.system.notify import notify

        notify("\U0001f4f1 Delta: request from Telegram", text[:120])
        chats, chat_id = getattr(self.c, "chats", None), self._chat(uid)
        history = chats.context_for(chat_id) if chats and chat_id else ""
        if chats and chat_id:
            chats.add(chat_id, "user", text)
        res = await self.c.submit(TaskRequest(text, channel=self.name, user=uid, chat_id=chat_id, history=history))
        if chats and chat_id:
            chats.add(chat_id, "delta", res.answer, {"status": res.status, "provider": res.provider,
                                                      "options": res.options, "channel": self.name})
        if res.status == "needs_user" and res.questions:
            await self._start_form(message, uid, res)
            return
        parts = chunks(result_text(res))
        kb = None
        if res.status == "needs_user" and res.options:
            self._choices[uid] = list(res.options)
            kb = self._buttons([[(o[:60], f"ans:{i}")] for i, o in enumerate(res.options)])
        for i, part in enumerate(parts):
            await self._send(message, part, kb if i == len(parts) - 1 else None)

    async def _send(self, message: Any, text: str, kb: Any = None) -> None:
        try:
            await message.reply_text(to_html(text), parse_mode="HTML", reply_markup=kb)
        except Exception:
            await message.reply_text(text, reply_markup=kb)

    # --- question forms: several questions, tick boxes (multi) or pick-one ---------------------------------
    async def _start_form(self, message: Any, uid: str, res: Any) -> None:
        form = {"questions": res.questions, "answers": {}, "left": set(range(len(res.questions)))}
        self._forms[uid] = form
        intro = res.answer or "A few quick questions so I get this exactly right:"
        await self._send(message, intro + "\n(Tap your answers, or just type them.)")
        for qi, q in enumerate(res.questions):
            await message.reply_text(self._form_text(q), reply_markup=self._form_kb(q, qi, form))

    @staticmethod
    def _form_text(q: dict[str, Any]) -> str:
        return f"{q['q']}" + ("\n(tick all that apply, then Done)" if q["multi"] else "")

    def _form_kb(self, q: dict[str, Any], qi: int, form: dict[str, Any]) -> Any:
        picked = form["answers"].get(qi, [])
        def label(o: str) -> str:
            return ((("☑ " if o in picked else "☐ ") + o) if q["multi"] else o)[:60]

        rows = [[(label(o), f"fq:{qi}:{oi}")] for oi, o in enumerate(q["options"])]
        if q["multi"]:
            rows.append([("✓ Done", f"fd:{qi}")])
        return self._buttons(rows)

    async def _on_form(self, update: Any, ctx: Any) -> None:
        from delta.core.questions import answers_text

        q = update.callback_query
        uid = str(q.from_user.id)
        if not is_allowed(uid, self.allowed):
            await q.answer("not authorised")
            return
        form = self._forms.get(uid)
        if form is None:
            await q.answer("That question is closed.")
            return
        await q.answer()
        kind, *nums = q.data.split(":")
        qi = int(nums[0])
        question = form["questions"][qi]
        picked: list[str] = form["answers"].setdefault(qi, [])
        if kind == "fq":
            option = question["options"][int(nums[1])]
            if question["multi"]:  # a tick box: toggle and show it, the question stays open
                picked.remove(option) if option in picked else picked.append(option)
                await q.edit_message_reply_markup(self._form_kb(question, qi, form))
                return
            picked[:] = [option]
        form["left"].discard(qi)
        await q.edit_message_text(f"{question['q']}\n→ {', '.join(picked) or 'no preference'}")
        if not form["left"]:
            self._forms.pop(uid, None)
            await self._run(update, uid, answers_text(form["questions"], form["answers"]))

    async def _on_answer(self, update: Any, ctx: Any) -> None:
        """A tapped choice is the user's answer, as if they had typed it."""
        q = update.callback_query
        uid = str(q.from_user.id)
        if not is_allowed(uid, self.allowed):
            await q.answer("not authorised")
            return
        choices = self._choices.pop(uid, [])
        idx = int(q.data.split(":", 1)[1])
        await q.answer()
        if idx >= len(choices):
            await q.edit_message_reply_markup(None)
            return
        await q.edit_message_text(f"{q.message.text}\n\n→ {choices[idx]}")
        await self._run(update, uid, choices[idx])

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
