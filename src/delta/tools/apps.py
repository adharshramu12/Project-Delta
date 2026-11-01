"""Open applications, folders, files and URLs; list running apps."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from typing import Any

import psutil
from pydantic import BaseModel, Field

from delta.config import AppEntry
from delta.tools.base import Risk, Tool, ToolResult


class OpenAppArgs(BaseModel):
    name: str = Field(description="App name or alias, e.g. 'antigravity', 'file explorer', 'vscode'")
    argument: str | None = Field(default=None, description="Optional file/folder to open with it")


class OpenApp(Tool[OpenAppArgs]):
    name = "open_app"
    description = "Launch a desktop application by friendly name (from apps.yaml) or executable name."
    risk = Risk.ACT
    Args = OpenAppArgs

    def __init__(self, apps: dict[str, AppEntry]) -> None:
        self.apps = apps

    def spec(self) -> dict[str, Any]:
        spec = super().spec()
        spec["function"]["description"] += f" Known apps: {', '.join(sorted(self.apps))}."
        return spec

    def resolve(self, name: str) -> tuple[str, AppEntry | None]:
        key = name.strip().lower()
        for k, entry in self.apps.items():
            if key == k.lower() or key in (a.lower() for a in entry.aliases):
                return k, entry
        return key, None

    def app(self, args: OpenAppArgs) -> str | None:
        return self.resolve(args.name)[0]

    def paths(self, args: OpenAppArgs) -> list[str]:
        return [args.argument] if args.argument and not args.argument.startswith("http") else []

    async def run(self, args: OpenAppArgs) -> ToolResult:
        key, entry = self.resolve(args.name)
        extra = [args.argument] if args.argument else []
        if entry and entry.path:
            target = os.path.expandvars(entry.path)
            if not os.path.exists(target):
                return ToolResult(False, f"{key} not found at {target}; fix config/apps.yaml")
            cmd = [target, *extra]
        else:
            exe = entry.command if entry and entry.command else key
            found = shutil.which(exe)
            if not found:
                return ToolResult(False, f"could not find '{exe}' on PATH; add it to config/apps.yaml")
            cmd = [found, *extra]
        await asyncio.to_thread(
            subprocess.Popen, cmd,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        await asyncio.sleep(1.5)
        exe_name = os.path.basename(cmd[0]).lower()
        running = any((p.info["name"] or "").lower() == exe_name for p in psutil.process_iter(["name"]))
        return ToolResult(True, f"launched {key}", {"app": key, "process_running": running})


class OpenTargetArgs(BaseModel):
    target: str = Field(description="Folder, file or URL to open with its default handler")


class OpenTarget(Tool[OpenTargetArgs]):
    name = "open_target"
    description = "Open a folder in File Explorer, a file with its default app, or a URL in the browser."
    risk = Risk.ACT
    Args = OpenTargetArgs

    def paths(self, args: OpenTargetArgs) -> list[str]:
        return [] if "://" in args.target else [args.target]

    def domains(self, args: OpenTargetArgs) -> list[str]:
        return [args.target] if "://" in args.target else []

    async def run(self, args: OpenTargetArgs) -> ToolResult:
        target = args.target if "://" in args.target else os.path.expandvars(os.path.expanduser(args.target))
        if "://" not in target and not os.path.exists(target):
            return ToolResult(False, f"not found: {target}")
        await asyncio.to_thread(os.startfile, target)
        return ToolResult(True, f"opened {target}", {"target": target})


class NoArgs(BaseModel):
    pass


class ListRunningApps(Tool[NoArgs]):
    name = "list_running_apps"
    description = "List running applications (process names with memory use). Useful to verify an app opened."
    risk = Risk.READ
    Args = NoArgs

    async def run(self, args: NoArgs) -> ToolResult:
        seen: dict[str, float] = {}
        for p in psutil.process_iter(["name", "memory_info"]):
            name, mem = p.info["name"], p.info["memory_info"]
            if name and mem:
                seen[name] = seen.get(name, 0) + mem.rss / 1e6
        top = sorted(seen.items(), key=lambda kv: -kv[1])[:40]
        return ToolResult(True, "\n".join(f"{n}: {mb:.0f} MB" for n, mb in top), {"apps": [n for n, _ in top]})


def app_tools(apps: dict[str, AppEntry]) -> list[Tool]:
    return [OpenApp(apps), OpenTarget(), ListRunningApps()]
