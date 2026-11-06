"""Telegram channel (long polling, no public URL). Only allow-listed user ids are served.

Approvals arrive as messages with Approve/Deny buttons in each allowed user's private chat.

Pairing: instead of finding their numeric user id, the user sends the bot a one-time code shown in
Delta's dashboard (valid 10 minutes). Whoever sends it is added to the allow-list. The code only ever
appears on this PC's dashboard, so a bot token alone still controls nothing. The message must be exactly
the code, and after a few wrong tries the code is dropped until a new one is made on the PC.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
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
    "/browser - which browser I use: your default, Chrome, Edge or my private one (switching needs your PIN)\n"
    "/signin - open my private browser on the PC so you can sign in to your sites (needs your PIN)\n"
    "/keys - replace an API key (when credits or limits run out)\n\n"
    "<b>Your PC</b>:\n"
    "/volume - mute, louder, quieter (or /volume 30)\n"
    "/brightness - screen brightness (laptop screens)\n"
    "/theme - Windows light or dark mode\n"
    "/windows - what is open on the PC, tap to close it\n"
    "/screen - a screenshot of the PC now\n"
    "/camera - a photo from the webcam of whoever is at the PC\n\n"
    "<b>Connected apps</b>: /apps to see them, /connect notion to add one (needs your PIN; you finish the sign-in "
    "on the PC), /disconnect notion. Then just ask: \"search my Notion for the launch plan\".\n\n"
    "<b>Schedules</b>: \"every day at 9 am check my attendance\", \"remind me at 6 pm to call Ravi\", "
    "\"what's scheduled?\" (making or turning on a schedule from here needs your PIN)\n\n"
    "/stop - stop what I'm doing now (or just send \"stop\"); /stop all - stop everything waiting too\n"
    "/tasks - recent tasks; resume or discard one that was interrupted\n"
    "/undo - put back the files a task changed (needs your PIN)\n"
    "/unlock - enter your PIN (screen, camera, files, commands, approving deletes or sends,\n"
    "  closing apps and keys need it)\n"
    "/lock - lock all remote control now (only the PC can unlock it)\n"
    "/status - PC health and AIs available\n"
    "/help - this list\n\n"
    "🎙 You can also just <b>send a voice note</b>: Telugu, Hindi or English.\n"
    "<b>Send a photo or a file</b> (PDF, Word, PowerPoint, text) with a caption saying what you want, for example "
    "\"summarise this\" or \"what is wrong in this screenshot?\""
)

# Pairing codes: no look-alike characters (0/O, 1/I/L), so they are easy to read off the screen and type.
PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
PAIR_LENGTH = 8  # 31^8, about 8.5e11 codes
PAIR_MAX_FAILS = 5  # wrong tries before the code is dropped (a new one must be made on the PC)
ALBUM_WAIT_S = 1.5  # photos sent together arrive one by one: wait this long for the rest of an album


def _known_ext(name: str) -> bool:
    from delta.attachments.kinds import IMAGE_EXT, TEXT_EXT

    return Path(name).suffix.lower() in {*IMAGE_EXT, *TEXT_EXT, ".pdf", ".docx", ".pptx"}


class TelegramChannel(PCMenu):
    name = "telegram"

    def __init__(self, c: Container, token: str, allowed: list[str]) -> None:
        from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

        self.c = c
        self.allowed = list(allowed)
        self.on_pair: Callable[[list[str]], None] | None = None  # persists the allow-list
        self._code: tuple[str, float] | None = None  # (pairing code, expires at)
        self._pair_fails = 0  # wrong tries against the current code
        self.bot_name = ""
        self._choices: dict[str, list[str]] = {}  # the choices last offered to each user (tap buttons)
        self._edits: dict[str, str] = {}  # user -> email approval waiting for "what should change?"
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
        self.app.add_handler(CommandHandler("browser", self._browser_cmd))
        self.app.add_handler(CommandHandler("signin", self._signin_cmd))
        self.app.add_handler(CommandHandler("stop", self._stop_cmd))
        self.app.add_handler(CommandHandler("keys", self._keys_cmd))
        self.app.add_handler(CommandHandler("lock", self._lock_cmd))
        self.app.add_handler(CommandHandler("unlock", self._unlock_cmd))
        for name, handler in (("volume", self._volume_cmd), ("brightness", self._brightness_cmd),
                              ("theme", self._theme_cmd),
                              ("windows", self._apps_cmd), ("screen", self._screen_cmd),
                              ("camera", self._camera_cmd)):
            self.app.add_handler(CommandHandler(name, handler))
        self.app.add_handler(CallbackQueryHandler(self._on_pc_button, pattern=r"^sys:"))
        self.app.add_handler(CallbackQueryHandler(self._on_control, pattern=r"^ctl:"))
        self.app.add_handler(CallbackQueryHandler(self._on_answer, pattern=r"^ans:"))
        self.app.add_handler(CallbackQueryHandler(self._on_form, pattern=r"^f[qd]:"))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))
        self.app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self._on_voice))
        # Photos and files (pictures, PDF, Word, PowerPoint, text), with or without a caption (delta.attachments)
        self.app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self._on_file))
        self._albums: dict[str, dict[str, Any]] = {}  # media_group_id -> photos of one album, sent as one request
        self._album_tasks: set[asyncio.Task[None]] = set()
        self.app.add_handler(CallbackQueryHandler(self._on_button, pattern=r"^appr:"))
        from delta.channels.telegram_tasks import attach as attach_tasks

        attach_tasks(self)  # /tasks and Resume / Discard for interrupted tasks (delta.engine)
        from delta.channels.telegram_apps import attach as attach_apps

        attach_apps(self)  # /apps, /connect, /disconnect: connected apps (delta.connectors)
        from delta.channels.telegram_logins import attach as attach_logins

        attach_logins(self)  # /logins and /watchers: read-only (delta.logins, delta.watchers)

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
                ("mode", "Permission level"), ("browser", "Which browser I use"),
                ("signin", "Open my private browser to sign in"), ("keys", "Replace an API key"),
                ("stop", "Stop the running task"),
                ("lock", "Lock remote control now"), ("unlock", "Enter your PIN"),
                ("volume", "PC volume"), ("brightness", "Screen brightness"), ("theme", "Windows light / dark mode"),
                ("apps", "Connected apps (Notion, GitHub...)"), ("connect", "Connect an app"),
                ("logins", "Saved site logins (list only)"), ("watchers", "Sites I watch for you"),
                ("windows", "Open windows, tap to close"),
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

    async def send_text(self, user: str, text: str) -> None:
        """A message Delta starts itself (a scheduled task's result, a reminder) in an allowed user's chat."""
        if not is_allowed(user, self.allowed):
            raise ValueError("not an allowed Telegram user")
        for part in chunks(text):
            try:
                await self.app.bot.send_message(int(user), to_html(part), parse_mode="HTML")
            except Exception:
                await self.app.bot.send_message(int(user), part)

    async def reply(self, update: Any, text: str, **kw: Any) -> None:
        """Formatted (bold, code, links); plain text if Telegram rejects the markup."""
        try:
            await update.message.reply_text(to_html(text), parse_mode="HTML", **kw)
        except Exception:
            await update.message.reply_text(text, **kw)

    def new_pairing_code(self) -> str:
        """A fresh single-use code; only the PC's dashboard can ask for one, which also lifts a lockout."""
        code = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(PAIR_LENGTH))
        self._code = (code, time.monotonic() + 600)
        self._pair_fails = 0
        return code

    async def _try_pair(self, update: Any, text: str) -> None:
        """A not-yet-allowed user wrote: pair them if they sent exactly the current code, else explain how."""
        uid = str(update.effective_user.id)
        code = self._code
        if code and time.monotonic() >= code[1]:
            self._code = code = None
        sent = (text or "").strip().upper().encode()
        if code and hmac.compare_digest(sent, code[0].encode()):
            self._code = None
            self._pair_fails = 0
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
        if code:  # a live code and a wrong guess: count it, and drop the code after too many
            self._pair_fails += 1
            locked = self._pair_fails >= PAIR_MAX_FAILS
            if locked:
                self._code = None
            log.warning("telegram.pair_failed", user=uid, fails=self._pair_fails, locked=locked)
            self.c.audit.record("telegram.pair_failed", user=uid, fails=self._pair_fails, locked=locked)
            if locked and callable(warn := getattr(self.c, "warn", None)):
                warn(f"Telegram pairing stopped after {PAIR_MAX_FAILS} wrong codes. "
                     "Make a new code from Settings → Telegram → Connect my phone.")
        await update.message.reply_text(
            "🔒 Not connected yet. On your PC open Delta → Settings → Telegram → Connect my phone, "
            f"then send me the {PAIR_LENGTH}-character code shown there (just the code).")

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
        from delta.control import AUTOPILOT_STILL_ASKS, LEVEL_HELP, LEVELS

        now = self.c.settings.boundaries.autonomy
        rows = [[(f"{'✅ ' if k == now else ''}{v}: {LEVEL_HELP[k]}"[:60], f"ctl:mode:{k}")] for k, v in LEVELS.items()]
        await update.message.reply_text(f"Permission level. Tap to switch:\n\n{AUTOPILOT_STILL_ASKS}",
                                        reply_markup=self._buttons(rows))

    def _browser_menu(self) -> tuple[str, Any]:
        """The current browser and one button per option; unavailable ones say why (tapping explains)."""
        from delta.control import browser_state

        st = browser_state(self.c)
        rows = []
        for o in st["options"]:
            mark = "✅ " if o["current"] else ("" if o["available"] else "🚫 ")
            rows.append([(f"{mark}{o['label']}" + ("" if o["available"] else " (not available)"),
                          f"ctl:browser:{o['id']}")])
        if any(o["id"] == "private" and o["available"] for o in st["options"]):
            rows.append([(f"{'✅ ' if e['current'] else ''}Private runs in: {e['label'].split(' (')[0]}"[:60],
                          f"ctl:bengine:{e['id']}") for e in st["engines"] if e["available"]])
        if st["signin_available"]:
            rows.append([("🔑 Sign in to my sites (opens on the PC)", "ctl:signin:")])
        from html import escape

        lines = [f"Browser now: <b>{escape(st['label'])}</b>", f"({escape(st['describe'])})"]
        lines += [f"• {escape(o['label'])}: {escape(o['reason'])}" for o in st["options"] if not o["available"]]
        if st["running_note"]:
            lines.append(escape(st["running_note"]))
        lines.append("\nTap to switch (needs your PIN). It applies from the next task, never in the middle of one.")
        return "\n".join(lines), self._buttons(rows)

    async def _browser_cmd(self, update: Any, ctx: Any) -> None:
        if not is_allowed(update.effective_user.id, self.allowed):
            return
        if await self._locked(update.message.reply_text):
            return
        if ctx is not None and ctx.args:  # /browser edge, /browser private
            from delta.tools.browser_choice import parse_choice

            choice = parse_choice(" ".join(ctx.args)) or " ".join(ctx.args)
            await self._apply(update.message.reply_text, "browser", choice, update.effective_user.id)
            return
        text, kb = self._browser_menu()
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

    async def _signin_cmd(self, update: Any, ctx: Any) -> None:
        if not is_allowed(update.effective_user.id, self.allowed):
            return
        url = " ".join(ctx.args) if ctx is not None and ctx.args else ""
        await self._apply(update.message.reply_text, "signin", url, update.effective_user.id)

    async def _stop_cmd(self, update: Any, ctx: Any) -> None:
        if not is_allowed(update.effective_user.id, self.allowed):
            return
        from delta.control import stop_command

        words = "/stop " + " ".join(ctx.args) if ctx is not None and ctx.args else "/stop"  # /stop or /stop all
        reply = stop_command(self.c, words, by=f"telegram:{update.effective_user.id}")
        await update.message.reply_text(reply or "Send /stop to stop what I'm doing now, or /stop all to stop "
                                                 "everything, including tasks waiting their turn.")

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
            elif kind in ("browser", "bengine"):
                from delta.control import set_browser, set_private_engine

                async def switch() -> None:
                    try:
                        if kind == "browser":
                            await say("✅ " + set_browser(self.c, value, by))
                        else:
                            await say("✅ " + set_private_engine(self.c, value, by))
                    except ValueError as e:
                        await say(f"✗ {e}")
                await self._guarded(str(uid), "browser", say, switch)
            elif kind == "signin":
                from delta.control import open_signin_browser

                async def open_it() -> None:
                    ok, message = await open_signin_browser(self.c, value, by)
                    await say(("✅ " if ok else "✗ ") + message)
                await self._guarded(str(uid), "signin", say, open_it)
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
        from delta.control import stop_command

        # "stop" / "stop all" as a plain message works like /stop: first, so it is never taken as a PIN or a task
        if (reply := stop_command(self.c, update.message.text or "", by=f"telegram:{user.id}")) is not None:
            await update.message.reply_text(reply)
            return
        if await self._take_pin(update, str(user.id)):
            return
        if await self._locked(update.message.reply_text):
            return
        if await self._take_key(update, str(user.id)):
            return
        if (req_id := self._edits.pop(str(user.id), None)) is not None:  # the changes after tapping Edit
            settled, why = self.c.approvals.edit(req_id, update.message.text, by=f"telegram:{user.id}")
            await update.message.reply_text("Got it. I'll redraft it and show you again." if settled else
                                            f"That email is no longer waiting ({why}).")
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

    # --- photos and files (delta.attachments) ---------------------------------------------------------------
    def _attachments(self) -> Any:
        return getattr(getattr(self.c, "orchestrator", None), "attachments", None)

    async def _on_file(self, update: Any, ctx: Any) -> None:
        """A photo or a document. Downloaded straight to disk (never into memory), checked, read on this PC, then
        done like a typed request with the caption as the task. Same rules as any phone request: only paired users,
        nothing while remote control is locked, and the PIN rules apply to whatever the task then does."""
        from delta.attachments.kinds import DEFAULT_REQUEST

        user = update.effective_user
        message = update.message
        if not is_allowed(user.id, self.allowed):
            log.warning("telegram.rejected_file", user=user.id)
            return
        uid = str(user.id)
        self._pending_pin.pop(uid, None)  # a file is never a PIN or a key
        self._awaiting_key.pop(uid, None)
        if await self._locked(message.reply_text):
            return
        svc = self._attachments()
        if svc is None:
            await message.reply_text("Files are switched off in Delta's settings, so I can't read this one.")
            return
        caption = (message.caption or "").strip()
        group = getattr(message, "media_group_id", None)
        try:
            att = await self._download(message, svc, uid)
        except ValueError as e:  # AttachmentError: plain words
            await message.reply_text(str(e))
            return
        except Exception as e:
            log.warning("telegram.file_failed", error=str(e)[:200])
            await message.reply_text("I couldn't get that file from Telegram. Please send it again.")
            return
        self.c.audit.record("attachment.added", id=att.id, kind=att.kind, size=att.size, via="telegram", user=uid)
        if group:  # an album: wait a moment for the rest, then one request for all of them
            album = self._albums.setdefault(str(group), {"files": [], "caption": "", "update": update})
            album["files"].append(att)
            album["caption"] = album["caption"] or caption
            if len(album["files"]) == 1:
                task = asyncio.create_task(self._album_done(str(group), uid))
                self._album_tasks.add(task)  # keep a reference until it is done
                task.add_done_callback(self._album_tasks.discard)
            return
        await self._run(update, uid, caption or DEFAULT_REQUEST, [att])

    async def _album_done(self, group: str, uid: str) -> None:
        from delta.attachments.kinds import DEFAULT_REQUEST

        await asyncio.sleep(ALBUM_WAIT_S)
        album = self._albums.pop(group, None)
        if not album:
            return
        svc = self._attachments()
        files = album["files"][: svc.cfg.max_files_per_message] if svc else album["files"]
        if len(album["files"]) > len(files):
            await album["update"].message.reply_text(f"I'll look at the first {len(files)} files of that album.")
        await self._run(album["update"], uid, album["caption"] or DEFAULT_REQUEST, files)

    async def _download(self, message: Any, svc: Any, uid: str) -> Any:
        """The file into a temporary file (checked against the size limit first), then into the store."""
        import tempfile

        from delta.attachments.kinds import TELEGRAM_MAX_MB, AttachmentError, safe_name, too_big

        limit_mb = min(float(svc.cfg.max_mb), float(TELEGRAM_MAX_MB))
        if message.photo:
            sizes = sorted(message.photo, key=lambda p: (p.width or 0) * (p.height or 0))
            # the biggest size up to 2560 px is plenty for OCR and is downscaled again before any model sees it
            fitting = [p for p in sizes if max(p.width or 0, p.height or 0) <= 2560]
            media, name = (fitting or sizes)[-1], f"photo-{message.message_id}.jpg"
        else:
            media = message.document
            name = safe_name(media.file_name or f"file-{message.message_id}", "file")
            if not _known_ext(name):  # say why before downloading anything
                from delta.attachments.kinds import SUPPORTED, detect

                detect(b"?", name)  # raises with the plain words for that kind of file
                raise AttachmentError(f"Delta can't read that kind of file. It reads {SUPPORTED}.")
        size = int(getattr(media, "file_size", 0) or 0)
        if size > limit_mb * 1048576:
            raise AttachmentError(too_big(size, limit_mb) + (" Telegram bots can only download files up to 20 MB."
                                                              if limit_mb >= TELEGRAM_MAX_MB else ""))
        await message.chat.send_action("typing")
        tg_file = await media.get_file()
        fd, tmp = tempfile.mkstemp(prefix="delta-tg-", suffix=".part")
        import os

        os.close(fd)
        try:
            await tg_file.download_to_drive(custom_path=tmp)
            att, _ = await svc.ingest_file(Path(tmp), name, self._chat(uid), "telegram")
            return att
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)

    def _chat(self, uid: str) -> str:
        """This person's Telegram conversation (with memory, also in the dashboard sidebar)."""
        chats = getattr(self.c, "chats", None)
        if chats is None:
            return ""
        title = f"Telegram {uid}"
        found = next((ch for ch in chats.list(title) if ch.title == title), None)
        return (found or chats.create(title)).id

    async def _run(self, update: Any, uid: str, text: str, attachments: list[Any] | None = None) -> None:
        """Do what was asked. The chat's recent turns go along, so an answer like "the second one" means
        something; a question comes back with tap buttons for its choices. `attachments`: files the user sent
        (delta.attachments.store.Attachment), already saved in this chat's folder."""
        message = update.message or update.callback_query.message
        await message.chat.send_action("typing")
        from delta.system.notify import notify

        notify("\U0001f4f1 Delta: request from Telegram", text[:120])
        from delta.system.vault import VaultUnavailableError

        chats, chat_id, history = getattr(self.c, "chats", None), "", ""
        files = list(attachments or [])
        try:
            chat_id = self._chat(uid)
            history = chats.context_for(chat_id) if chats and chat_id else ""
            if files and chat_id and (svc := self._attachments()) is not None:
                files = [svc.store.move(a, chat_id) for a in files]
            if chats and chat_id:
                chats.add(chat_id, "user", text, {"attachments": [a.public() for a in files], "channel": self.name}
                          if files else None)
        except VaultUnavailableError as e:  # never stored unencrypted: tell the owner why, do nothing else
            await message.reply_text(str(e))
            return
        res = await self.c.submit(TaskRequest(text, channel=self.name, user=uid, chat_id=chat_id, history=history,
                                              attachments=[a.id for a in files]))
        if chats and chat_id:
            with contextlib.suppress(VaultUnavailableError):  # the answer is still sent below
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

        if req.can_edit:  # an email draft: Approve / Edit / Cancel
            rows = [[InlineKeyboardButton("✅ Approve", callback_data=f"appr:{req.id}:y"),
                     InlineKeyboardButton("✏️ Edit", callback_data=f"appr:{req.id}:e"),
                     InlineKeyboardButton("⛔ Cancel", callback_data=f"appr:{req.id}:n")]]
        else:
            rows = [[InlineKeyboardButton("✅ Approve", callback_data=f"appr:{req.id}:y"),
                     InlineKeyboardButton("⛔ Deny", callback_data=f"appr:{req.id}:n")]]
        if req.for_task:  # offered only for actions it may cover (see ToolExecutor.task_grant_key)
            rows.append([InlineKeyboardButton("🔁 Allow for this task", callback_data=f"appr:{req.id}:t")])
        kb = InlineKeyboardMarkup(rows)
        for uid in self.allowed:
            await self.app.bot.send_message(int(uid), approval_text(req), reply_markup=kb)

    async def _on_button(self, update: Any, ctx: Any) -> None:
        q = update.callback_query
        if not is_allowed(q.from_user.id, self.allowed):
            await q.answer("not authorised")
            return
        _, req_id, answer = q.data.split(":")
        if answer == "e":  # Edit: the next message from this person says what to change
            self._edits[str(q.from_user.id)] = req_id
            await q.answer()
            await q.edit_message_text(f"{q.message.text}\n\n→ ✏️ edit: send me what should change")
            return
        # Remote Guard (lock, PIN to approve) is checked inside the broker, for every channel alike.
        settled, why = self.c.approvals.answer(req_id, answer in ("y", "t"), by=f"telegram:{q.from_user.id}",
                                               for_task=answer == "t")
        if not settled and why != "already answered or expired":
            await q.answer(why[:200], show_alert=True)  # needs the PIN: the buttons stay for after /unlock
            return
        await q.answer()
        verdict = {"approved": "✅ approved", "denied": "⛔ denied",
                   "approved for this task": "🔁 approved for this task"}.get(why, why)
        await q.edit_message_text(f"{q.message.text}\n\n→ {verdict}")
