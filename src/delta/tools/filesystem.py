"""File tools. Deletes go to the Recycle Bin, never a hard delete."""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from delta.safety.policy import norm_path
from delta.tools.base import Risk, Tool, ToolResult

MAX_READ = 200_000


class PathArgs(BaseModel):
    path: str = Field(description="Absolute path or ~-relative path")


class ListDirArgs(PathArgs):
    sort: Literal["name", "size", "modified"] = Field(
        default="name", description="size = biggest first, modified = newest first")
    limit: int = Field(default=200, ge=1, le=500, description="At most this many entries")
    recursive: bool = Field(default=False, description="Include files in subfolders (files only)")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class ListDir(Tool[ListDirArgs]):
    name = "list_dir"
    description = ("List a folder with sizes and dates, in one call: e.g. the 5 biggest files "
                   "(sort='size', limit=5, recursive=true) or the newest (sort='modified').")
    risk = Risk.READ
    Args = ListDirArgs
    MAX_SCAN = 20000  # recursive scans of huge trees stop here and say so

    def paths(self, args: ListDirArgs) -> list[str]:
        return [args.path]

    async def run(self, args: ListDirArgs) -> ToolResult:
        p = norm_path(args.path)
        if not p.is_dir():
            return ToolResult(False, f"not a directory: {p}")
        return await asyncio.to_thread(self._list, p, args)

    def _list(self, p: Path, args: ListDirArgs) -> ToolResult:
        found, capped = [], False
        it = (e for e in p.rglob("*") if e.is_file()) if args.recursive else p.iterdir()
        for e in it:
            try:
                st = e.stat()
            except OSError:
                continue
            found.append((e, e.is_dir(), st.st_size, st.st_mtime))
            if len(found) >= self.MAX_SCAN:
                capped = True
                break
        if args.sort == "size":
            found.sort(key=lambda f: -f[2])
        elif args.sort == "modified":
            found.sort(key=lambda f: -f[3])
        else:
            found.sort(key=lambda f: (not f[1], f[0].name.lower()))
        rows = []
        for e, is_dir, size, mtime in found[:args.limit]:
            name = str(e.relative_to(p)) if args.recursive else e.name
            when = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            rows.append(f"[dir]  {name}" if is_dir else f"{human_size(size):>9}  {when}  {name}")
        if len(found) > args.limit:
            rows.append(f"…{len(found) - args.limit} more")
        if capped:
            rows.append(f"(stopped after {self.MAX_SCAN} files)")
        return ToolResult(True, "\n".join(rows) or "(empty)", {"path": str(p), "count": len(found)})


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

    def risk_for(self, args: PathArgs) -> Risk:
        # A folder that already exists is not changed: nothing to approve.
        return Risk.READ if norm_path(args.path).is_dir() else Risk.WRITE

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
        from send2trash import send2trash  # loaded only when something is deleted

        await asyncio.to_thread(send2trash, str(p))
        return ToolResult(True, f"recycled {p}", {"path": str(p), "exists": Path(p).exists()})


def filesystem_tools() -> list[Tool]:
    return [ListDir(), ReadFile(), PathInfo(), WriteFile(), MovePath(), MakeDir(), DeletePath()]
