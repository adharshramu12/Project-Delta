"""File tools. Deletes go to the Recycle Bin, never a hard delete."""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field
from send2trash import send2trash

from delta.safety.policy import norm_path
from delta.tools.base import Risk, Tool, ToolResult

MAX_READ = 200_000


class PathArgs(BaseModel):
    path: str = Field(description="Absolute path or ~-relative path")


class ListDir(Tool[PathArgs]):
    name = "list_dir"
    description = "List files and folders in a directory."
    risk = Risk.READ
    Args = PathArgs

    def paths(self, args: PathArgs) -> list[str]:
        return [args.path]

    async def run(self, args: PathArgs) -> ToolResult:
        p = norm_path(args.path)
        if not p.is_dir():
            return ToolResult(False, f"not a directory: {p}")
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))[:500]
        lines = [f"{'[dir] ' if e.is_dir() else '      '}{e.name}" for e in entries]
        return ToolResult(True, "\n".join(lines) or "(empty)", {"path": str(p), "count": len(entries)})


class ReadFile(Tool[PathArgs]):
    name = "read_file"
    description = "Read a text file (first 200 KB)."
    risk = Risk.READ
    Args = PathArgs

    def paths(self, args: PathArgs) -> list[str]:
        return [args.path]

    async def run(self, args: PathArgs) -> ToolResult:
        p = norm_path(args.path)
        if not p.is_file():
            return ToolResult(False, f"file not found: {p}")
        text = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace")
        return ToolResult(True, text[:MAX_READ], {"path": str(p), "size": p.stat().st_size})


class PathInfo(Tool[PathArgs]):
    name = "path_info"
    description = "Check whether a file/folder exists and get its size and modified time. Good for verifying."
    risk = Risk.READ
    Args = PathArgs

    def paths(self, args: PathArgs) -> list[str]:
        return [args.path]

    async def run(self, args: PathArgs) -> ToolResult:
        p = norm_path(args.path)
        if not p.exists():
            return ToolResult(True, f"does not exist: {p}", {"path": str(p), "exists": False})
        st = p.stat()
        info = {"path": str(p), "exists": True, "is_dir": p.is_dir(), "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")}
        return ToolResult(True, str(info), info)


class WriteArgs(BaseModel):
    path: str
    content: str
    append: bool = False


class WriteFile(Tool[WriteArgs]):
    name = "write_file"
    description = "Create a text file, overwrite it, or append to it. Creates parent folders."
    risk = Risk.WRITE
    Args = WriteArgs

    def risk_for(self, args: WriteArgs) -> Risk:
        exists = norm_path(args.path).exists()
        return Risk.DESTRUCTIVE if exists and not args.append else Risk.WRITE

    def paths(self, args: WriteArgs) -> list[str]:
        return [args.path]

    def summary(self, args: WriteArgs) -> str:
        verb = "append to" if args.append else ("OVERWRITE" if norm_path(args.path).exists() else "create")
        return f"{verb} {args.path} ({len(args.content)} chars)"

    async def run(self, args: WriteArgs) -> ToolResult:
        p = norm_path(args.path)
        p.parent.mkdir(parents=True, exist_ok=True)

        def write() -> None:
            with p.open("a" if args.append else "w", encoding="utf-8") as f:
                f.write(args.content)

        await asyncio.to_thread(write)
        return ToolResult(True, f"wrote {p}", {"path": str(p), "size": p.stat().st_size})


class MoveArgs(BaseModel):
    source: str
    destination: str


class MovePath(Tool[MoveArgs]):
    name = "move_path"
    description = "Move or rename a file or folder."
    risk = Risk.WRITE
    Args = MoveArgs

    def risk_for(self, args: MoveArgs) -> Risk:
        return Risk.DESTRUCTIVE if norm_path(args.destination).exists() else Risk.WRITE

    def paths(self, args: MoveArgs) -> list[str]:
        return [args.source, args.destination]

    def summary(self, args: MoveArgs) -> str:
        return f"move {args.source} -> {args.destination}"

    async def run(self, args: MoveArgs) -> ToolResult:
        src, dst = norm_path(args.source), norm_path(args.destination)
        if not src.exists():
            return ToolResult(False, f"source not found: {src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.move, str(src), str(dst))
        return ToolResult(True, f"moved to {dst}", {"source_exists": src.exists(), "destination": str(dst)})


class MakeDir(Tool[PathArgs]):
    name = "make_dir"
    description = "Create a folder (and parents)."
    risk = Risk.WRITE
    Args = PathArgs

    def paths(self, args: PathArgs) -> list[str]:
        return [args.path]

    async def run(self, args: PathArgs) -> ToolResult:
        p = norm_path(args.path)
        p.mkdir(parents=True, exist_ok=True)
        return ToolResult(True, f"created {p}", {"path": str(p), "exists": True})


class DeletePath(Tool[PathArgs]):
    name = "delete_path"
    description = "Move a file or folder to the Recycle Bin (recoverable)."
    risk = Risk.DESTRUCTIVE
    Args = PathArgs

    def paths(self, args: PathArgs) -> list[str]:
        return [args.path]

    def summary(self, args: PathArgs) -> str:
        return f"move to Recycle Bin: {args.path}"

    async def run(self, args: PathArgs) -> ToolResult:
        p = norm_path(args.path)
        if not p.exists():
            return ToolResult(False, f"not found: {p}")
        await asyncio.to_thread(send2trash, str(p))
        return ToolResult(True, f"recycled {p}", {"path": str(p), "exists": Path(p).exists()})


def filesystem_tools() -> list[Tool]:
    return [ListDir(), ReadFile(), PathInfo(), WriteFile(), MovePath(), MakeDir(), DeletePath()]
