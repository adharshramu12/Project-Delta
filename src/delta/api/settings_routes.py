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
    autonomy: Literal["ask-everything", "ask-risky", "trusted"] | None = None
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


class SettingsIn(BaseModel):
    boundaries: BoundariesIn | None = None
    memory: MemoryIn | None = None
    save_dir: str | None = Field(default=None, max_length=300)
    addons_enabled: list[str] | None = None


def _clean(items: list[str]) -> list[str]:
    return list(dict.fromkeys(i.strip().strip('"') for i in items if i.strip()))


def register(app: FastAPI, c: Container, auth: Any) -> None:
    def available_addons() -> list[str]:
        return sorted({*builtin_addons(), *discover(c.settings.data_dir / "addons")})

    @app.get("/api/settings", dependencies=[Depends(auth)])
    async def get_settings() -> dict[str, Any]:
        s = c.settings
        keys = await asyncio.to_thread(lambda: {k: bool(get_secret(k)) for k in KEY_INFO})
        return {
            "boundaries": s.boundaries.model_dump(),
            "memory": {"enabled": s.memory.enabled, "auto_extract": s.memory.auto_extract,
                       "learn_skills": s.memory.learn_skills},
            "save_dir": s.files.save_dir,
            "save_dir_resolved": str(save_dir(s.files, create=False)),
            "addons": {"available": available_addons(), "enabled": s.addons.enabled},
            "keys": {k: {"label": KEY_INFO[k], "set": v} for k, v in keys.items()},
            "always_protected": [*ALWAYS_DENIED, str(s.data_dir)],
        }

    @app.put("/api/settings", dependencies=[Depends(auth)])
    async def put_settings(body: SettingsIn) -> dict[str, Any]:
        s = c.settings
        changes: dict[str, Any] = {}
        restart = False
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
            restart = sorted(body.addons_enabled) != sorted(s.addons.enabled)
            changes["addons"] = {"enabled": body.addons_enabled}
        if not changes:
            return {"ok": True, "restart_needed": False}

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
        c.orchestrator.executor.policy = make_policy(s)  # next action uses the new rules
        c.audit.record("settings.changed", sections=sorted(changes))
        return {"ok": True, "restart_needed": restart, "save_dir_resolved": str(save_dir(s.files, create=False))}

    @app.delete("/api/settings/key/{name}", dependencies=[Depends(auth)])
    async def remove_key(name: str) -> dict[str, Any]:
        if name not in KEY_INFO:
            raise HTTPException(400, "unknown key name")
        await asyncio.to_thread(delete_secret, name)
        c.router.refresh()
        c.audit.record("setup.key_removed", name=name)
        return {"ok": True, "models_ready": c.router.usable()}
