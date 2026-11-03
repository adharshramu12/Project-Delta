"""First-run setup API: hardware check, key validation and saving, user choices.

Everything chosen here is saved to the user's own settings file in the data dir (never the
repo's config/) and applied to the running agent at once, except chat channels, which need a
restart (the response says so).
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from delta.bootstrap import Container
from delta.config import ChannelConfig, read_user_settings, write_user_settings
from delta.setup.checks import KEY_INFO, check_hardware, validate_key
from delta.system.secrets import get_secret, set_secret


class KeyIn(BaseModel):
    name: str
    value: str = Field(min_length=1, max_length=500)


class PinIn(BaseModel):
    pin: str = Field(min_length=4, max_length=8, pattern=r"^\d+$")


class SetupIn(BaseModel):
    owner_name: str = Field(default="", max_length=60)
    autonomy: Literal["ask-everything", "ask-risky", "trusted", "autopilot"] = "ask-risky"
    telegram_enabled: bool = False
    telegram_user_ids: list[str] = Field(default_factory=list)


def register(app: FastAPI, c: Container, auth: Any) -> None:
    @app.get("/api/setup", dependencies=[Depends(auth)])
    async def get_setup() -> dict[str, Any]:
        local = [p for p in c.router.providers.values() if p.local]
        running = await asyncio.to_thread(lambda: any(callable(f := getattr(p, "ping", None)) and f() for p in local))
        hw = await asyncio.to_thread(check_hardware, running)
        tg = c.settings.channels.get("telegram") or ChannelConfig()
        user = read_user_settings(c.settings.data_dir)
        keys = await asyncio.to_thread(lambda: {k: bool(get_secret(k)) for k in KEY_INFO})
        return {
            "completed": bool(user.get("setup", {}).get("completed")),
            "hardware": hw.to_dict(),
            "keys": {k: {"label": KEY_INFO[k], "set": v} for k, v in keys.items()},
            "models_ready": c.router.usable(),
            "owner_name": c.settings.agent.owner_name,
            "autonomy": c.settings.boundaries.autonomy,
            "telegram": {"enabled": tg.enabled, "user_ids": tg.allowed_user_ids},
        }

    @app.post("/api/setup/key", dependencies=[Depends(auth)])
    async def save_key(body: KeyIn) -> dict[str, Any]:
        if body.name not in KEY_INFO:
            raise HTTPException(400, "unknown key name")
        ok, message = await validate_key(body.name, body.value)
        if not ok:
            return {"ok": False, "message": message}  # nothing stored
        await asyncio.to_thread(set_secret, body.name, body.value.strip())
        c.router.refresh()  # usable right away, no restart
        c.warnings[:] = [w for w in c.warnings if "No AI model" not in w]
        c.audit.record("setup.key", name=body.name)  # never the value
        return {"ok": True, "message": message, "models_ready": c.router.usable()}

    # --- Remote security (Remote Guard): only from this PC's dashboard, never from a phone -------------
    @app.get("/api/security", dependencies=[Depends(auth)])
    async def security_status() -> dict[str, Any]:
        return await asyncio.to_thread(c.guard.status) if c.guard else {"has_pin": False, "locked": False}

    @app.put("/api/security/pin", dependencies=[Depends(auth)])
    async def security_pin(body: PinIn) -> dict[str, Any]:
        if c.guard is None:
            raise HTTPException(400, "Remote security is not available.")
        try:
            await asyncio.to_thread(c.guard.set_pin, body.pin, "dashboard")
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "message": "PIN saved. Your phone asks for it before sensitive actions."}

    @app.post("/api/security/unlock", dependencies=[Depends(auth)])
    async def security_unlock() -> dict[str, Any]:
        if c.guard:
            c.guard.unlock_from_pc("dashboard")
        return {"ok": True, "message": "Remote control is unlocked."}

    @app.post("/api/security/lock", dependencies=[Depends(auth)])
    async def security_lock() -> dict[str, Any]:
        if c.guard:
            c.guard.lock("locked on the PC", by="dashboard")
        return {"ok": True, "message": "Remote control is locked."}

    @app.get("/api/telegram", dependencies=[Depends(auth)])
    async def telegram_status() -> dict[str, Any]:
        ch = c.live_channels.get("telegram")
        tg = c.settings.channels.get("telegram") or ChannelConfig()
        return {"has_token": bool(get_secret("telegram_bot_token")), "connected": ch is not None,
                "bot": getattr(ch, "bot_name", ""), "users": list(getattr(ch, "allowed", tg.allowed_user_ids))}

    @app.post("/api/telegram/pair", dependencies=[Depends(auth)])
    async def telegram_pair() -> dict[str, Any]:
        """Connect the bot now (no restart) and show a one-time code to send it from the phone."""
        from delta.daemon import start_channel

        if not get_secret("telegram_bot_token"):
            raise HTTPException(400, "Save your bot token from @BotFather first.")
        try:
            ch = await start_channel(c, "telegram")
        except ImportError as e:
            raise HTTPException(400, "Telegram support is not installed. Run: uv sync --extra channels") from e
        except Exception as e:
            raise HTTPException(400, f"Could not connect to Telegram: {e}. Check the bot token.") from e
        code = ch.new_pairing_code()  # type: ignore[attr-defined]
        bot = ch.bot_name  # type: ignore[attr-defined]
        return {"code": code, "bot": bot, "link": f"https://t.me/{bot}?start={code}" if bot else ""}

    @app.post("/api/setup/complete", dependencies=[Depends(auth)])
    async def complete(body: SetupIn) -> dict[str, Any]:
        ids = [i.strip() for i in body.telegram_user_ids if i.strip()]
        if body.telegram_enabled and not ids:
            raise HTTPException(400, "Add your Telegram user id, otherwise nobody could use the bot.")
        old_tg = c.settings.channels.get("telegram") or ChannelConfig()
        write_user_settings(c.settings.data_dir, {
            "setup": {"completed": True},
            "agent": {"owner_name": body.owner_name.strip()},
            "boundaries": {"autonomy": body.autonomy},
            "channels": {"telegram": {"enabled": body.telegram_enabled, "allowed_user_ids": ids}},
        })
        # apply to the running agent
        c.settings.agent.owner_name = body.owner_name.strip()
        c.settings.boundaries.autonomy = body.autonomy
        c.orchestrator.executor.policy.b.autonomy = body.autonomy
        c.settings.channels["telegram"] = ChannelConfig(enabled=body.telegram_enabled, allowed_user_ids=ids)
        restart = body.telegram_enabled != old_tg.enabled or (body.telegram_enabled and ids != old_tg.allowed_user_ids)
        c.audit.record("setup.complete", autonomy=body.autonomy, telegram=body.telegram_enabled)
        return {"ok": True, "restart_needed": restart, "models_ready": c.router.usable()}
