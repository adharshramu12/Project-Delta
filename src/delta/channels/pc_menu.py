"""Telegram controls for the PC itself: /volume, /brightness, /theme, /windows (open windows), /screen, /camera.

Instant (no AI, no tokens) and button-driven: the message updates in place to show the new state.
Closing an app that could lose unsaved work asks first; a photo or screenshot is audit-logged."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from delta.system import pc_control as pc
from delta.system.pc_control import PCControlError

VOL_ROWS = [[("🔇 Mute", "sys:vol:mute"), ("🔈 Unmute", "sys:vol:unmute"),
             ("➖", "sys:vol:-10"), ("➕", "sys:vol:+10")],
            [("25%", "sys:vol:=25"), ("50%", "sys:vol:=50"), ("75%", "sys:vol:=75"), ("100%", "sys:vol:=100")]]
BRI_ROWS = [[("➖", "sys:bri:-10"), ("➕", "sys:bri:+10")],
            [("25%", "sys:bri:=25"), ("50%", "sys:bri:=50"), ("75%", "sys:bri:=75"), ("100%", "sys:bri:=100")]]

THEME_ROWS = [[("☀️ Light", "sys:theme:light"), ("🌙 Dark", "sys:theme:dark"), ("🔁 Switch", "sys:theme:toggle")]]


def _theme_text() -> str:
    return "🌙 Windows is in dark mode" if pc.windows_theme() == "dark" else "☀️ Windows is in light mode"


class PCMenu:
    """Mixed into TelegramChannel; uses its _buttons, allowed list and audit (self.c.audit)."""

    _windows_shown: dict[str, tuple[int, str, str]]  # token -> (hwnd, title, app) from the last /windows

    def _pc_init(self) -> None:
        self._windows_shown = {}

    # --- volume / brightness ---------------------------------------------------------------------------
    @staticmethod
    def _vol_text() -> str:
        level, muted = pc.volume()
        return f"{'🔇' if muted else '🔊'} Volume {level}%{' (muted)' if muted else ''}"

    async def _volume_cmd(self, update: Any, ctx: Any) -> None:
        if not self._ok(update.effective_user.id):
            return
        if ctx.args and ctx.args[0].rstrip("%").isdigit():  # /volume 30
            await asyncio.to_thread(pc.set_volume, int(ctx.args[0].rstrip("%")))
        await self._pc_reply(update.message.reply_text, self._vol_text, VOL_ROWS)

    async def _brightness_cmd(self, update: Any, ctx: Any) -> None:
        if not self._ok(update.effective_user.id):
            return
        if ctx.args and ctx.args[0].rstrip("%").isdigit():
            await asyncio.to_thread(pc.set_brightness, int(ctx.args[0].rstrip("%")))
        await self._pc_reply(update.message.reply_text, lambda: f"☀️ Brightness {pc.brightness()}%", BRI_ROWS)

    async def _theme_cmd(self, update: Any, ctx: Any) -> None:
        if not self._ok(update.effective_user.id):
            return
        if ctx.args and ctx.args[0].lower() in ("light", "dark", "toggle"):  # /theme dark
            await asyncio.to_thread(pc.set_windows_theme, ctx.args[0].lower())
        await self._pc_reply(update.message.reply_text, _theme_text, THEME_ROWS)

    async def _pc_reply(self, say: Any, text_fn: Any, rows: list) -> None:
        try:
            text = await asyncio.to_thread(text_fn)
        except PCControlError as e:
            await say(f"✗ {e}")
            return
        await say(text, reply_markup=self._buttons(rows))  # type: ignore[attr-defined]

    # --- open apps -------------------------------------------------------------------------------------
    async def _apps_cmd(self, update: Any, ctx: Any) -> None:
        if not self._ok(update.effective_user.id):
            return
        await self._show_apps(update.message.reply_text)

    async def _show_apps(self, say: Any) -> None:
        wins = await asyncio.to_thread(pc.open_windows)
        if not wins:
            await say("Nothing is open on the PC right now.")
            return
        self._windows_shown = {}
        rows = []
        for i, w in enumerate(wins[:20]):
            token = str(i)
            self._windows_shown[token] = (w.hwnd, w.title, w.app)
            name = w.app.removesuffix(".exe").removesuffix(".EXE")
            rows.append([(f"✖ {name}: {w.title}"[:60], f"sys:close:{token}")])
        rows.append([("🔄 Refresh", "sys:apps:refresh"), ("📸 Screenshot", "sys:screen:now")])
        await say(f"Open on the PC now ({len(wins)}). Tap one to close it:", reply_markup=self._buttons(rows))  # type: ignore[attr-defined]

    async def _close(self, q: Any, token: str, mode: str, uid: str) -> None:
        shown = self._windows_shown.get(token)
        if not shown:
            await q.edit_message_text("That list is old. Send /windows again.")
            return
        hwnd, title, app = shown
        current = {w.hwnd: w for w in await asyncio.to_thread(pc.open_windows)}
        w = current.get(hwnd)
        if w is None or w.title != title:
            await q.edit_message_text(f"“{title}” is already closed or has changed. Send /windows again.")
            return
        if mode == "ask" and pc.may_lose_work(w):
            await q.edit_message_text(
                f"⚠ {app} may have unsaved work:\n“{title}”\nClose it anyway?",
                reply_markup=self._buttons([[("Close (it may ask to save)", f"sys:closeok:{token}")],  # type: ignore[attr-defined]
                                            [("Force close (unsaved work is lost)", f"sys:kill:{token}")],
                                            [("Cancel", "sys:apps:refresh")]]))
            return
        try:
            result = await asyncio.to_thread(pc.close_window, hwnd, mode == "kill")
        except PCControlError as e:
            await q.edit_message_text(f"✗ {e}")
            return
        self.c.audit.record("pc.close", app=app, title=title, forced=mode == "kill", result=result, by=uid)  # type: ignore[attr-defined]
        if result == "closed":
            await q.edit_message_text(f"✅ Closed {app.removesuffix('.exe')}: “{title}”")
        else:
            await q.edit_message_text(
                f"⚠ “{title}” is still open: it is probably asking something (like 'Save changes?').",
                reply_markup=self._buttons([[("Force close (unsaved work is lost)", f"sys:kill:{token}")],  # type: ignore[attr-defined]
                                            [("Leave it open", "sys:apps:refresh")]]))

    # --- pictures --------------------------------------------------------------------------------------
    async def _screen_cmd(self, update: Any, ctx: Any) -> None:
        if not self._ok(update.effective_user.id):
            return
        uid, chat = str(update.effective_user.id), update.effective_chat.id
        await self._guarded(uid, "screen", update.message.reply_text,  # type: ignore[attr-defined]
                            lambda: self._send_picture(chat, "screen", uid))

    async def _camera_cmd(self, update: Any, ctx: Any) -> None:
        if not self._ok(update.effective_user.id):
            return
        uid, chat = str(update.effective_user.id), update.effective_chat.id

        async def shoot() -> None:
            await self.app.bot.send_message(chat, "📷 Taking a photo with the PC's camera (its light comes on)…")  # type: ignore[attr-defined]
            await self._send_picture(chat, "camera", uid)
        await self._guarded(uid, "camera", update.message.reply_text, shoot)  # type: ignore[attr-defined]

    async def _send_picture(self, chat_id: int, kind: str, uid: str) -> None:
        bot = self.app.bot  # type: ignore[attr-defined]
        try:
            path = await asyncio.to_thread(pc.screenshot if kind == "screen" else pc.camera_photo)
        except PCControlError as e:
            await bot.send_message(chat_id, f"✗ {e}")
            return
        except Exception as e:  # missing display, driver trouble: say so instead of going silent
            await bot.send_message(chat_id, f"✗ I couldn't take the {kind} picture ({type(e).__name__}).")
            return
        self.c.audit.record(f"pc.{kind}", by=uid)  # type: ignore[attr-defined]
        caption = ("Screen" if kind == "screen" else "Camera") + time.strftime(" · %d %b %H:%M:%S")
        try:
            with path.open("rb") as f:
                await bot.send_photo(chat_id, f, caption=caption)
        finally:
            path.unlink(missing_ok=True)  # nothing is kept on the PC

    # --- buttons -----------------------------------------------------------------------------------------
    async def _on_pc_button(self, update: Any, ctx: Any) -> None:
        q = update.callback_query
        uid = str(q.from_user.id)
        if not self._ok(uid):
            await q.answer("not authorised")
            return
        _, kind, value = q.data.split(":", 2)
        await q.answer()
        try:
            if kind in ("vol", "bri"):
                setter = pc.set_volume if kind == "vol" else pc.set_brightness
                if value == "mute":
                    await asyncio.to_thread(pc.set_volume, None, 0, True)
                elif value == "unmute":
                    await asyncio.to_thread(pc.set_volume, None, 0, False)
                elif value.startswith("="):
                    await asyncio.to_thread(setter, int(value[1:]))
                else:
                    await asyncio.to_thread(setter, None, int(value))
                self.c.audit.record(f"pc.{'volume' if kind == 'vol' else 'brightness'}", change=value, by=uid)  # type: ignore[attr-defined]
                if kind == "vol":
                    text, rows = await asyncio.to_thread(self._vol_text), VOL_ROWS
                else:
                    text, rows = f"☀️ Brightness {await asyncio.to_thread(pc.brightness)}%", BRI_ROWS
                await q.edit_message_text(text, reply_markup=self._buttons(rows))  # type: ignore[attr-defined]
            elif kind == "theme":
                await asyncio.to_thread(pc.set_windows_theme, value)
                self.c.audit.record("pc.theme", change=value, by=uid)  # type: ignore[attr-defined]
                await q.edit_message_text(await asyncio.to_thread(_theme_text),
                                          reply_markup=self._buttons(THEME_ROWS))  # type: ignore[attr-defined]
            elif kind in ("close", "closeok", "kill"):
                mode = {"close": "ask", "closeok": "close", "kill": "kill"}[kind]
                await self._guarded(uid, "close_app", q.message.reply_text,  # type: ignore[attr-defined]
                                    lambda: self._close(q, value, mode, uid))
            elif kind == "apps":
                await self._show_apps(q.edit_message_text)
            elif kind == "screen":
                chat = q.message.chat.id
                await self._guarded(uid, "screen", q.message.reply_text,  # type: ignore[attr-defined]
                                    lambda: self._send_picture(chat, "screen", uid))
        except PCControlError as e:
            await q.edit_message_text(f"✗ {e}")

    def _ok(self, uid: Any) -> bool:
        from delta.channels.base import is_allowed

        guard = getattr(self.c, "guard", None)  # type: ignore[attr-defined]
        return is_allowed(uid, self.allowed) and not (guard and guard.locked)  # type: ignore[attr-defined]
