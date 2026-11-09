"""Settings page API: read and change what Delta may do, how it learns, where it saves, which
add-ons run, and which keys are stored.

Changes are validated, saved to the user's settings file (never the repo config) and applied
to the running agent immediately; only add-on changes need a restart (the response says so).
"""

from __future__ import annotations

import asyncio
from datetime import time
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from delta.addons.builtin import builtin_addons
from delta.addons.loader import discover
from delta.addons.mcp_settings import MCP
from delta.bootstrap import Container, make_policy
from delta.config import QuietHours, save_dir, write_user_settings
from delta.safety.policy import ALWAYS_DENIED, norm_path
from delta.setup.checks import KEY_INFO
from delta.system.secrets import delete_secret, get_secret


class QuietIn(BaseModel):
    enabled: bool = False
    start: str = "23:30"
    end: str = "07:00"


class BoundariesIn(BaseModel):
    autonomy: Literal["ask-everything", "ask-risky", "trusted", "autopilot"] | None = None
    allowed_paths: list[str] | None = None
    denied_paths: list[str] | None = None
    denied_apps: list[str] | None = None
    denied_domains: list[str] | None = None
    allowed_domains: list[str] | None = None
    quiet_hours: QuietIn | None = None


class MemoryIn(BaseModel):
    enabled: bool | None = None
    auto_extract: bool | None = None
    learn_skills: bool | None = None


class BrowserIn(BaseModel):
    # The four-way choice (delta.tools.browser_choice): default | chrome | edge | private, and which browser
    # runs Delta's private one. Older clients keep sending mode/prefer, which work exactly as before.
    choice: Literal["default", "chrome", "edge", "private"] | None = None
    engine: Literal["auto", "chrome", "edge"] | None = None
    mode: Literal["your-browser", "delta-profile"] | None = None
    prefer: Literal["auto", "chrome", "edge"] | None = None
    orange_cursor: bool | None = None


class AttachmentsIn(BaseModel):
    vision: str | None = Field(default=None, max_length=60)          # auto | off | one of llm.providers
    vision_model: str | None = Field(default=None, max_length=200)
    max_mb: float | None = Field(default=None, ge=1, le=200)
    retention_days: float | None = Field(default=None, ge=0, le=3650)


class StudentIn(BaseModel):
    join_year: int | None = Field(default=None, ge=0, le=2100)
    year_of_study: int | None = Field(default=None, ge=0, le=8)


class SigninIn(BaseModel):
    url: str = Field(default="", max_length=2000)


class AutostartIn(BaseModel):
    enabled: bool


class SettingsIn(BaseModel):
    browser: BrowserIn | None = None
    boundaries: BoundariesIn | None = None
    memory: MemoryIn | None = None
    save_dir: str | None = Field(default=None, max_length=300)
    addons_enabled: list[str] | None = None
    attachments: AttachmentsIn | None = None
    student: StudentIn | None = None


def _clean(items: list[str]) -> list[str]:
    return list(dict.fromkeys(i.strip().strip('"') for i in items if i.strip()))


def register(app: FastAPI, c: Container, auth: Any) -> None:
    def available_addons() -> list[str]:
        return sorted({*builtin_addons(), *discover(c.settings.data_dir / "addons")})

    def listed_addons() -> list[str]:
        """What Settings > Add-ons shows: MCP lives on the Apps page now (delta.addons.mcp_settings)."""
        return [a for a in available_addons() if a != MCP]

    @app.get("/api/settings", dependencies=[Depends(auth)])
    async def get_settings() -> dict[str, Any]:
        s = c.settings
        keys = await asyncio.to_thread(lambda: {k: bool(get_secret(k)) for k in KEY_INFO})
        from delta.control import browser_state
        from delta.tools.user_browser import default_browser

        found = await asyncio.to_thread(default_browser, s.browser.prefer)
        state = await asyncio.to_thread(browser_state, c)
        return {
            "browser": {**s.browser.model_dump(), "using": found[0] if found else None, **state},
            "boundaries": s.boundaries.model_dump(),
            "memory": {"enabled": s.memory.enabled, "auto_extract": s.memory.auto_extract,
                       "learn_skills": s.memory.learn_skills},
            "save_dir": s.files.save_dir,
            "save_dir_resolved": str(save_dir(s.files, create=False)),
            "addons": {"available": listed_addons(), "enabled": [a for a in s.addons.enabled if a != MCP]},
            "keys": {k: {"label": KEY_INFO[k], "set": v} for k, v in keys.items()},
            "always_protected": [*ALWAYS_DENIED, str(s.data_dir)],
            "attachments": {**s.attachments.model_dump(), "providers": sorted(s.llm.providers),
                            "can_see_pictures": _can_see()},
            "student": {**s.student.model_dump(), "now": _academic_now()},
        }

    def _academic_now() -> str:
        from delta.tools.academic import from_settings

        return from_settings(c.settings).line()

    def _can_see() -> bool:
        svc = getattr(c.orchestrator, "attachments", None)
        return bool(svc is not None and svc.can_see())

    @app.put("/api/settings", dependencies=[Depends(auth)])
    async def put_settings(body: SettingsIn) -> dict[str, Any]:
        s = c.settings
        changes: dict[str, Any] = {}
        restart = False
        browser_note = ""
        if body.browser:
            br = body.browser.model_dump(exclude_none=True)
            choice, engine = br.pop("choice", None), br.pop("engine", None)
            from delta.tools.browser_choice import from_config

            now = from_config(s.browser)
            if choice == now.choice and engine in (None, now.engine):
                choice = engine = None  # the page sends the whole form: an unchanged choice is no change
            if choice or engine:
                from delta.control import set_browser, set_private_engine

                try:  # validated (installed?), saved, audited and applied from the next task
                    if choice:
                        browser_note = await asyncio.to_thread(set_browser, c, choice, "dashboard", engine)
                    else:
                        browser_note = await asyncio.to_thread(set_private_engine, c, str(engine), "dashboard")
                except ValueError as e:
                    raise HTTPException(400, str(e)) from None
            if body.browser.choice:  # the four-way choice decides mode/prefer; old keys alongside are ignored
                br.pop("mode", None)
                br.pop("prefer", None)
            if br.get("mode", s.browser.mode) != s.browser.mode and getattr(c, "browsers", None) is None:
                restart = True  # without the browser switch, the tools are chosen at start-up
            if br:
                changes["browser"] = br
        if body.boundaries:
            b = body.boundaries.model_dump(exclude_none=True)
            for key in ("allowed_paths", "denied_paths", "denied_apps", "denied_domains", "allowed_domains"):
                if key in b:
                    b[key] = _clean(b[key])
            for p in b.get("allowed_paths", []):
                if len(norm_path(p).parts) < 2:
                    raise HTTPException(400, f"'{p}' is a whole drive. Allow specific folders instead.")
            if "quiet_hours" in b:
                try:
                    time.fromisoformat(b["quiet_hours"]["start"])
                    time.fromisoformat(b["quiet_hours"]["end"])
                except ValueError:
                    raise HTTPException(400, "Quiet hours must look like 23:30.") from None
            changes["boundaries"] = b
        if body.memory:
            changes["memory"] = body.memory.model_dump(exclude_none=True)
        if body.save_dir is not None:
            raw = body.save_dir.strip() or "auto"
            if raw != "auto":
                target = norm_path(raw)
                if target.is_relative_to(norm_path(s.data_dir)) or len(target.parts) < 2:
                    raise HTTPException(400, "Choose a normal folder for saved files (not a drive or Delta's "
                                             "own data folder).")
            changes["files"] = {"save_dir": raw}
        if body.addons_enabled is not None:
            unknown = set(body.addons_enabled) - set(available_addons())
            if unknown:
                raise HTTPException(400, f"Unknown add-on(s): {', '.join(sorted(unknown))}")
            # The page no longer lists "mcp": its state is kept as it is. An older page or script that still sends
            # "mcp" switches the MCP servers on, as that tick always did.
            wanted = [a for a in body.addons_enabled if a != MCP]
            restart = sorted(wanted) != sorted(a for a in s.addons.enabled if a != MCP)
            changes["addons"] = {"enabled": wanted}
            if MCP in body.addons_enabled and not s.addons.config.get(MCP, {}).get("enabled", False):
                changes["addons"]["config"] = {MCP: {"enabled": True}}
                restart = True
        if body.attachments:
            att = body.attachments.model_dump(exclude_none=True)
            if "vision" in att:
                att["vision"] = att["vision"].strip() or "auto"
                if att["vision"] not in ("auto", "off", *s.llm.providers):
                    raise HTTPException(400, "Pictures: choose auto, off, or one of your AI providers.")
            if "vision_model" in att:
                att["vision_model"] = att["vision_model"].strip()
            if att != {k: getattr(s.attachments, k) for k in att}:
                changes["attachments"] = att
        if body.student:
            st = body.student.model_dump(exclude_none=True)
            if st.get("join_year") and not 1990 <= st["join_year"] <= 2100:
                raise HTTPException(400, "Year you joined: a year such as 2023, or leave it empty.")
            if "year_of_study" in st:
                from datetime import date

                from delta.tools.academic import year_label, year_start

                # remembered with the academic year it was said in, so it moves on by itself next year
                st["year_of_study_in"] = year_label(year_start(date.today())) if st["year_of_study"] else ""
                if st["year_of_study"] == s.student.year_of_study and s.student.year_of_study_in:
                    st["year_of_study_in"] = s.student.year_of_study_in  # unchanged: keep when it was said
            if st != {k: getattr(s.student, k) for k in st}:
                changes["student"] = st
        if not changes:
            return {"ok": True, "restart_needed": False, **({"browser_note": browser_note} if browser_note else {})}

        write_user_settings(s.data_dir, changes)
        # apply live
        if new_bounds := changes.get("boundaries"):
            for k, v in new_bounds.items():
                setattr(s.boundaries, k, QuietHours(**v) if k == "quiet_hours" else v)
        for k, v in changes.get("memory", {}).items():
            setattr(s.memory, k, v)
        if "files" in changes:
            s.files.save_dir = changes["files"]["save_dir"]
        if "addons" in changes:
            s.addons.enabled = changes["addons"]["enabled"]
            if "config" in changes["addons"]:
                s.addons.config.setdefault(MCP, {})["enabled"] = True
        if "browser" in changes:
            for k, v in changes["browser"].items():
                setattr(s.browser, k, v)
            if getattr(c, "browsers", None) is None:  # without the switch: change Chrome/Edge right away
                ub = getattr(c.registry.get("browser_open"), "ub", None)
                if ub is not None and "prefer" in changes["browser"]:
                    ub.use(s.browser.prefer)
            cursor = getattr(getattr(c.registry.get("click_at"), "ctl", None), "cursor", None)
            if cursor is not None:  # the "Delta is in control" ring and border (the stop hotkeys stay on)
                cursor.set_enabled(s.browser.orange_cursor)
        for k, v in changes.get("student", {}).items():
            setattr(s.student, k, v)
        if "attachments" in changes:
            for k, v in changes["attachments"].items():
                setattr(s.attachments, k, v)
            if (svc := getattr(c.orchestrator, "attachments", None)) is not None:
                from delta.bootstrap import pictures_router

                svc.vision_router = pictures_router(s)  # the pictures-only model, if one is named
                svc.store.max_mb = s.attachments.max_mb
                svc.store.max_bytes = int(s.attachments.max_mb * 1024 * 1024)
                svc.store.retention_days = s.attachments.retention_days
        c.orchestrator.executor.policy = make_policy(s)  # next action uses the new rules
        c.audit.record("settings.changed", sections=sorted(changes))
        return {"ok": True, "restart_needed": restart, "save_dir_resolved": str(save_dir(s.files, create=False)),
                **({"browser_note": browser_note} if browser_note else {})}

    # Settings > Setup and shutdown: "Start Delta when I sign in to Windows" (delta.system.autostart). Applied at
    # once, not with Save changes: it is a Windows setting, not one of Delta's.
    @app.get("/api/autostart", dependencies=[Depends(auth)])
    async def autostart_get() -> dict[str, Any]:
        from delta.system import autostart

        return (await asyncio.to_thread(autostart.status)).to_dict()

    @app.put("/api/autostart", dependencies=[Depends(auth)])
    async def autostart_put(body: AutostartIn) -> dict[str, Any]:
        from delta.system import autostart

        try:
            st = await asyncio.to_thread(autostart.set_enabled, body.enabled)
        except OSError as e:
            raise HTTPException(400, f"Windows did not accept the change: {e}") from e
        if not st.supported:
            raise HTTPException(400, "Starting with Windows is only available on Windows.")
        c.audit.record("settings.autostart", enabled=st.enabled)
        message = ("Delta starts by itself when you sign in to Windows (in the background, no window), so Telegram "
                   "keeps working after a restart." if st.enabled else
                   "Delta no longer starts when you sign in. Open it from its icon when you need it.")
        return {"ok": True, **st.to_dict(), "message": message}

    @app.post("/api/browser/signin", dependencies=[Depends(auth)])
    async def browser_signin(body: SigninIn) -> dict[str, Any]:
        """'Sign in to my sites': open Delta's private browser on this PC; the person signs in there."""
        from delta.control import open_signin_browser

        ok, message = await open_signin_browser(c, body.url, by="dashboard")
        return {"ok": ok, "message": message}

    @app.delete("/api/settings/key/{name}", dependencies=[Depends(auth)])
    async def remove_key(name: str) -> dict[str, Any]:
        if name not in KEY_INFO:
            raise HTTPException(400, "unknown key name")
        await asyncio.to_thread(delete_secret, name)
        c.router.refresh()
        c.audit.record("setup.key_removed", name=name)
        return {"ok": True, "models_ready": c.router.usable()}
