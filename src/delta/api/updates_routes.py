"""Updates and the first-run tour, for the dashboard.

/api/updates: this version and the newest one (from the last check; never a network call), the banner, "What's
new" after an update, Later and Skip this version, the update settings, Check now, and Update now. Update now never
starts while a task runs unless the person said to stop it, and installs only the exact commit and files that were
checked (delta.updates).

/api/tour: whether the first-run tour was shown, saved in the user's settings file so it shows once per PC.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from delta.bootstrap import Container
from delta.config import read_user_settings, write_user_settings
from delta.updates import install
from delta.updates.checker import UpdateError
from delta.updates.service import UpdateService

LATER_S = 24 * 3600


class UpdateSettingsIn(BaseModel):
    check: bool | None = None
    channel: Literal["stable", "beta"] | None = None
    telegram: bool | None = None


class VersionIn(BaseModel):
    version: str = Field(default="", max_length=64)


class InstallIn(BaseModel):
    version: str = Field(default="", max_length=64)  # the version the banner showed: never install a different one
    stop_tasks: bool = False


class TourIn(BaseModel):
    done: bool


def _busy(c: Container) -> int:
    tasks = {id(t): t for t in [c.running, *getattr(c, "active", {}).values()] if t is not None and not t.done()}
    return len(tasks)


def register(app: FastAPI, c: Container, auth: Any) -> None:
    def svc() -> UpdateService:
        if c.updates is None:
            c.updates = UpdateService(c.settings)
        return c.updates

    def view() -> dict[str, Any]:
        v = svc().checker.view()
        v["can_install"] = install.supported() and not install.is_dev_checkout()
        v["dev_checkout"] = install.is_dev_checkout()
        return v

    @app.get("/api/updates", dependencies=[Depends(auth)])
    async def updates_get() -> dict[str, Any]:
        return await asyncio.to_thread(view)

    @app.post("/api/updates/check", dependencies=[Depends(auth)])
    async def updates_check() -> dict[str, Any]:
        await asyncio.to_thread(svc().checker.check, True)
        v = await asyncio.to_thread(view)
        await svc().tell(v)
        return v

    @app.post("/api/updates/later", dependencies=[Depends(auth)])
    async def updates_later() -> dict[str, Any]:
        await asyncio.to_thread(svc().checker.update_state, later_until=time.time() + LATER_S)
        return await asyncio.to_thread(view)

    @app.post("/api/updates/skip", dependencies=[Depends(auth)])
    async def updates_skip(body: VersionIn) -> dict[str, Any]:
        version = body.version or (svc().checker.view().get("latest") or "")
        await asyncio.to_thread(svc().checker.update_state, skipped=version)
        c.audit.record("update.skipped", version=version)
        return await asyncio.to_thread(view)

    @app.post("/api/updates/whats-new/seen", dependencies=[Depends(auth)])
    async def whats_new_seen() -> dict[str, bool]:
        await asyncio.to_thread(svc().checker.update_state, whats_new=None)
        return {"ok": True}

    @app.put("/api/updates/settings", dependencies=[Depends(auth)])
    async def updates_settings(body: UpdateSettingsIn) -> dict[str, Any]:
        changes = body.model_dump(exclude_none=True)
        if changes:
            cfg = c.settings.updates
            turned_on = changes.get("check") is True and not cfg.check
            wake = turned_on or changes.get("channel", cfg.channel) != cfg.channel
            write_user_settings(c.settings.data_dir, {"updates": changes})
            for k, v in changes.items():
                setattr(cfg, k, v)
            c.audit.record("settings.changed", sections=["updates"])
            if wake:
                svc().wake()
        return await asyncio.to_thread(view)

    @app.post("/api/updates/install", dependencies=[Depends(auth)])
    async def updates_install(body: InstallIn) -> Any:
        if install.is_dev_checkout():
            raise HTTPException(400, "This is a developer copy of Delta. Update it with git pull.")
        if not install.supported():
            raise HTTPException(400, "Updating from the dashboard works on Windows only.")
        checker = svc().checker
        rel = checker.latest_release()
        v = checker.view()
        if rel is None or not v["newer"]:
            raise HTTPException(400, "You already have the newest version of Delta.")
        if body.version and body.version != rel.version:
            raise HTTPException(409, f"Delta {rel.version} is the newest now. Look at it, then press Update now again.")
        busy = _busy(c)
        if busy and not body.stop_tasks:
            return JSONResponse({"busy": busy, "detail": "A task is running. Stop it first, or update when it ends."},
                                status_code=409)
        if busy:
            from delta.control import stop_all

            stop_all(c, by="update")
            for _ in range(60):  # stopping lets go of the mouse and keyboard and records the result: a moment
                if not _busy(c):
                    break
                await asyncio.sleep(0.25)
            if _busy(c):
                raise HTTPException(409, "The task did not stop yet. Try Update now again in a moment.")
        try:
            target = await asyncio.to_thread(checker.resolve, rel)
        except UpdateError as e:
            c.audit.record("update.refused", version=rel.version, why=e.kind)
            raise HTTPException(502 if e.kind != "integrity" else 409, str(e)) from e
        await asyncio.to_thread(checker.update_state, whats_new={
            "version": target.version, "notes": rel.notes, "highlights": rel.highlights, "url": rel.url})
        try:
            await asyncio.to_thread(install.launch, target)
        except OSError as e:
            raise HTTPException(500, f"Windows did not start the update: {e}") from e
        c.audit.record("update.started", version=target.version, commit=target.sha, repo=target.repo)
        return {"ok": True, "version": target.version, "commit": target.sha,
                "message": f"Updating to Delta {target.version}. A window shows each step. Delta closes, updates "
                           "and opens again by itself. Your chats, settings and keys are kept."}

    @app.get("/api/tour", dependencies=[Depends(auth)])
    async def tour_get() -> dict[str, bool]:
        # the tour follows setup: it never covers the setup wizard on a brand-new PC
        completed = bool(read_user_settings(c.settings.data_dir).get("setup", {}).get("completed"))
        return {"done": c.settings.tour.done, "setup_completed": completed}

    @app.put("/api/tour", dependencies=[Depends(auth)])
    async def tour_put(body: TourIn) -> dict[str, bool]:
        write_user_settings(c.settings.data_dir, {"tour": {"done": body.done}})
        c.settings.tour.done = body.done
        return {"done": body.done}
