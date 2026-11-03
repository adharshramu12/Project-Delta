"""Open applications, folders, files and URLs; list running apps."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from typing import Any

import psutil
from pydantic import BaseModel, Field

from delta.config import AppEntry
from delta.tools.base import Risk, Tool, ToolResult

_start_apps: tuple[float, list[dict[str, str]]] | None = None


def start_apps() -> list[dict[str, str]]:
    """Apps in the Start menu ({"Name", "AppID"}), Store apps like Calculator included. Cached 10 min."""
    global _start_apps
    if os.name != "nt":
        return []
    if _start_apps and time.monotonic() - _start_apps[0] < 600:
        return _start_apps[1]
    apps: list[dict[str, str]] = []
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"],
                             capture_output=True, text=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(out.stdout or "[]")
        apps = [a for a in (data if isinstance(data, list) else [data]) if a.get("Name") and a.get("AppID")]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    _start_apps = (time.monotonic(), apps)
    return apps


def match_start_app(name: str, apps: list[dict[str, str]]) -> dict[str, str] | None:
    """Exact Start-menu name first, then one that starts with / contains it (shortest wins)."""
    key = name.strip().lower()
    for test in (lambda n: n == key, lambda n: n.startswith(key), lambda n: key in n):
        hits = [a for a in apps if test(a["Name"].lower())]
        if hits:
            return min(hits, key=lambda a: len(a["Name"]))
    return None


def _windows() -> dict[int, str]:
    """Visible top-level windows with a title: {handle: title}."""
    if os.name != "nt":
        return {}
    import ctypes
    from ctypes import wintypes

    user32, found = ctypes.windll.user32, {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd: int, _: int) -> bool:
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            if user32.GetWindowTextW(hwnd, buf, 256) and buf.value not in ("Program Manager",):
                found[int(hwnd)] = buf.value
        return True

    user32.EnumWindows(visit, 0)
    return found


def _bring_front(hwnd: int) -> None:
    import ctypes

    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.keybd_event(0x12, 0, 0, 0)  # a lone Alt lets a background process take the foreground
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)


def wait_for_app_window(before: dict[int, str], hints: list[str], seconds: float = 8) -> str | None:
    """The window the launch opened (new since `before`), else an existing one whose title names the
    app (single-instance apps reuse theirs). Brought to the front; returns its title."""
    deadline = time.monotonic() + seconds
    while True:
        now = _windows()
        new = [(h, t) for h, t in now.items() if h not in before]
        named = [(h, t) for h, t in now.items() if any(x and x in t.lower() for x in hints)]
        pick = new[0] if new else (named[0] if named and time.monotonic() > deadline - seconds / 2 else None)
        if pick:
            _bring_front(pick[0])
            return pick[1]
        if time.monotonic() > deadline:
            return None
        time.sleep(0.25)


class OpenAppArgs(BaseModel):
    name: str = Field(description="App name or alias, e.g. 'antigravity', 'file explorer', 'vscode'")
    argument: str | None = Field(default=None, description="Optional file/folder to open with it")


class OpenApp(Tool[OpenAppArgs]):
    name = "open_app"
    description = ("Launch a desktop application by its name as in the Start menu (e.g. 'Calculator', "
                   "'WhatsApp'), a friendly name from apps.yaml, or an executable. Waits for its window, "
                   "brings it to the front and returns the window title to use with the window tools.")
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
        hints = [key, args.name.strip().lower()]
        if entry and entry.path:
            target = os.path.expandvars(entry.path)
            if not os.path.exists(target):
                return ToolResult(False, f"{key} not found at {target}; fix config/apps.yaml")
            cmd = [target, *extra]
        else:
            exe = entry.command if entry and entry.command else key
            found = shutil.which(exe)
            if found:
                cmd = [found, *extra]
            elif not extra and (sa := match_start_app(args.name, await asyncio.to_thread(start_apps))):
                # Store and Start-menu apps (Calculator, WhatsApp...) have no .exe on PATH.
                cmd = ["explorer.exe", "shell:AppsFolder\\" + sa["AppID"]]
                key = sa["Name"]
                hints.append(key.lower())
            else:
                return ToolResult(False, f"could not find '{args.name}' in the Start menu or on PATH; "
                                         "add it to config/apps.yaml")
        before = await asyncio.to_thread(_windows)
        await asyncio.to_thread(
            subprocess.Popen, cmd,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        title = await asyncio.to_thread(wait_for_app_window, before, hints)
        exe_name = os.path.basename(cmd[0]).lower()
        running = any((p.info["name"] or "").lower() == exe_name for p in psutil.process_iter(["name"]))
        if title:
            return ToolResult(True, f"launched {key}; its window '{title}' is open and in front",
                              {"app": key, "process_running": running, "window": title})
        return ToolResult(True, f"launched {key} (no window seen yet; check with list_windows)",
                          {"app": key, "process_running": running})


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
