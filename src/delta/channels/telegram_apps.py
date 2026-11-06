"""Telegram: connected apps (delta.connectors).

  /apps                 the connected apps and their state (plus a button for the PC's open windows, which /apps
                        showed before 0.3; that list is now also /windows)
  /connect <app>        needs the PIN. Starts the sign-in IN THE BROWSER ON THE PC: the OAuth answer comes back to
                        127.0.0.1 on the PC, so it cannot be finished on the phone. Apps that need a token, a key or
                        an OAuth client id are connected on the PC (dashboard > Apps or `delta apps connect`):
                        Delta never takes tokens over Telegram and never sends one.
  /disconnect <app>     needs the PIN. Revokes the sign-in at the app when it can, and deletes it from the PC.
"""

from __future__ import annotations

import asyncio
from html import escape
from typing import Any

from delta.channels.base import is_allowed
from delta.system.logging import log


def attach(ch: Any) -> None:
    from telegram.ext import CommandHandler

    async def apps(update: Any, ctx: Any) -> None:
        await apps_cmd(ch, update)

    async def connect(update: Any, ctx: Any) -> None:
        await connect_cmd(ch, update, list(getattr(ctx, "args", None) or []))

    async def disconnect(update: Any, ctx: Any) -> None:
        await disconnect_cmd(ch, update, list(getattr(ctx, "args", None) or []))

    ch.app.add_handler(CommandHandler("apps", apps))
    ch.app.add_handler(CommandHandler("connect", connect))
    ch.app.add_handler(CommandHandler("disconnect", disconnect))


def _manager(ch: Any) -> Any:
    layer = getattr(ch.c, "connectors", None)
    if layer is None:
        return None
    layer.manager.refresh_if_changed()
    return layer.manager


async def apps_cmd(ch: Any, update: Any) -> None:
    if not is_allowed(update.effective_user.id, ch.allowed):
        return
    m = _manager(ch)
    windows = [[("Open windows on the PC", "sys:apps:refresh")]]
    if m is None:
        await update.message.reply_text("Connected apps are switched off in Delta's settings.",
                                        reply_markup=ch._buttons(windows))
        return
    rows = m.status()
    connected = [r for r in rows if r["connected"]]
    lines = [f"<b>{escape(r['name'])}</b>: {escape(r['state'])} ({r['tools']} tools)" for r in connected]
    others = ", ".join(r["id"] for r in rows if not r["connected"])
    text = ("<b>Connected apps</b>\n" + ("\n".join(lines) if lines else "None yet.")
            + f"\n\nAvailable: {escape(others)}\nConnect one with /connect &lt;app&gt; (needs your PIN; you finish "
              "the sign-in on the PC). Then just ask, e.g. \"search my Notion for the launch plan\".")
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=ch._buttons(windows))


def _pc_only_reason(spec: Any) -> str:
    if spec.secrets or spec.inputs:
        return "it needs settings or a key typed on the PC"
    if spec.auth.type == "token":
        return f"it needs {spec.auth.label or 'a token'}, and I never take tokens over Telegram"
    if spec.auth.type == "oauth" and spec.auth.client == "user":
        return "it needs the client id of an OAuth app you create, typed on the PC"
    if spec.status in ("custom", "community"):
        return "a server that is not from the app's vendor is confirmed on the PC"
    return ""


async def connect_cmd(ch: Any, update: Any, args: list[str]) -> None:
    if not is_allowed(update.effective_user.id, ch.allowed):
        return
    uid = str(update.effective_user.id)
    say = update.message.reply_text
    m = _manager(ch)
    if m is None:
        await say("Connected apps are switched off in Delta's settings.")
        return
    if not args:
        await say("Which app? For example: /connect notion. See /apps for the list.")
        return
    spec = m.find_app(" ".join(args))
    if spec is None:
        await say(f"I don't know an app called \"{' '.join(args)}\". See /apps for the list.")
        return
    if why := _pc_only_reason(spec):
        await say(f"{spec.name} has to be connected on the PC because {why}: open Delta's dashboard > Apps, or run "
                  f"`delta apps connect {spec.id}` there.")
        return

    async def run() -> None:
        result = await m.connect(spec.id, by=f"telegram:{uid}", open_browser=True)
        if result["status"] != "sign_in":
            await ch.send_text(uid, ("\u2713 " if result["status"] == "connected" else "\u2717 ") + result["message"])
            return
        await ch.send_text(uid, f"I opened the {spec.name} sign-in page in the browser on your PC. Finish signing in "
                                "there: it cannot be finished from the phone, because the sign-in answer comes back "
                                "to the PC itself. If no window opened, use Apps in the dashboard on the PC. I'll tell "
                                "you when it's done.")

        async def report() -> None:
            try:
                done = await m.wait_connect(spec.id)
            except Exception as e:  # time-out or cancelled
                done = {"status": "error", "message": f"Connecting {spec.name} did not finish ({type(e).__name__})."}
            await ch.send_text(uid, ("\u2713 " if done["status"] == "connected" else "\u2717 ") + done["message"])

        task = asyncio.create_task(report(), name=f"tg-connect-{spec.id}")
        ch._bg_tasks = getattr(ch, "_bg_tasks", set())
        ch._bg_tasks.add(task)
        task.add_done_callback(ch._bg_tasks.discard)

    try:
        await ch._guarded(uid, "apps", say, run)
    except Exception:
        log.exception("telegram.connect_failed", app=spec.id)
        await say(f"\u2717 Connecting {spec.name} failed. Try again from the PC (dashboard > Apps).")


async def disconnect_cmd(ch: Any, update: Any, args: list[str]) -> None:
    if not is_allowed(update.effective_user.id, ch.allowed):
        return
    uid = str(update.effective_user.id)
    say = update.message.reply_text
    m = _manager(ch)
    if m is None or not args:
        await say("Which app? For example: /disconnect notion")
        return
    spec = m.find_app(" ".join(args))
    if spec is None or m.store.get(spec.id) is None:
        await say("That app is not connected. See /apps.")
        return

    async def run() -> None:
        result = await m.disconnect(spec.id, by=f"telegram:{uid}")
        await ch.send_text(uid, ("\u2713 " if result["status"] == "disconnected" else "\u2717 ") + result["message"])

    await ch._guarded(uid, "apps", say, run)
