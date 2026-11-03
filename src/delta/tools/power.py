"""Power tools: what makes a coding agent good at real work on a PC. Search inside files, change one
exact piece of a file, and run a short Python script for anything no other tool covers (maths,
renaming many files, converting a CSV, reading Excel).

Same safety as everything else: the policy decides per call. A script's risk is judged from its code:
deleting or killing things is destructive, network use sends data out, anything else is a change.
An edited file's old version goes to the Recycle Bin first, so edits stay undoable."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from delta.safety.policy import norm_path
from delta.tools.base import Risk, Tool, ToolResult

TEXT_LIMIT = 2_000_000  # files bigger than this are skipped by search (logs, databases)
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "AppData", "$Recycle.Bin"}


class SearchArgs(BaseModel):
    path: str = Field(description="Folder (or one file) to search")
    pattern: str = Field(description="Text or regular expression to find, e.g. 'rent agreement' or 'TODO|FIXME'")
    glob: str = Field(default="*", description="Only these files, e.g. '*.txt' or '*.py'")
    ignore_case: bool = True
    max_results: int = Field(default=50, ge=1, le=300)


class SearchFiles(Tool[SearchArgs]):
    name = "search_files"
    description = ("Find text inside files under a folder (like grep): returns file:line: matching line. "
                   "Use it to locate notes, documents or code by content before opening them.")
    risk = Risk.READ
    Args = SearchArgs

    def paths(self, args: SearchArgs) -> list[str]:
        return [args.path]

    async def run(self, args: SearchArgs) -> ToolResult:
        root = norm_path(args.path)
        if not root.exists():
            return ToolResult(False, f"not found: {root}")
        try:
            rx = re.compile(args.pattern, re.I if args.ignore_case else 0)
        except re.error:
            rx = re.compile(re.escape(args.pattern), re.I if args.ignore_case else 0)
        return await asyncio.to_thread(self._search, root, rx, args)

    def _search(self, root: Path, rx: re.Pattern[str], args: SearchArgs) -> ToolResult:
        hits: list[str] = []
        files = [root] if root.is_file() else (
            p for p in root.rglob(args.glob) if p.is_file() and not SKIP_DIRS & set(p.parts))
        scanned = 0
        for f in files:
            try:
                if f.stat().st_size > TEXT_LIMIT:
                    continue
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            scanned += 1
            for n, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f}:{n}: {line.strip()[:200]}")
                    if len(hits) >= args.max_results:
                        return ToolResult(True, "\n".join(hits) + "\n…[more matches; narrow the search]",
                                          {"matches": len(hits), "files_scanned": scanned})
        return ToolResult(True, "\n".join(hits) or f"no matches in {scanned} files",
                          {"matches": len(hits), "files_scanned": scanned})


class EditArgs(BaseModel):
    path: str
    old: str = Field(description="The exact text to replace (copy it from read_file, including spaces)")
    new: str = Field(description="The text to put in its place")
    replace_all: bool = Field(default=False, description="Replace every occurrence, not just one")


class EditFile(Tool[EditArgs]):
    name = "edit_file"
    description = ("Change one exact piece of a text file, leaving the rest untouched (safer than rewriting the "
                   "whole file). 'old' must match exactly once unless replace_all. The old version goes to the "
                   "Recycle Bin.")
    risk = Risk.DESTRUCTIVE
    Args = EditArgs

    def paths(self, args: EditArgs) -> list[str]:
        return [args.path]

    def undoable(self, args: EditArgs) -> bool:
        return True

    def summary(self, args: EditArgs) -> str:
        return f"edit {args.path}: {args.old[:60]!r} -> {args.new[:60]!r}"

    async def run(self, args: EditArgs) -> ToolResult:
        p = norm_path(args.path)
        if not p.is_file():
            return ToolResult(False, f"file not found: {p}")
        text = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace")
        count = text.count(args.old)
        if count == 0:
            return ToolResult(False, "the 'old' text is not in the file. read_file it and copy the exact text.")
        if count > 1 and not args.replace_all:
            return ToolResult(False, f"the 'old' text appears {count} times: include more surrounding text so "
                                     "it is unique, or set replace_all=true.")
        await asyncio.to_thread(_backup_to_recycle_bin, p)
        new_text = text.replace(args.old, args.new) if args.replace_all else text.replace(args.old, args.new, 1)
        await asyncio.to_thread(p.write_text, new_text, encoding="utf-8")
        return ToolResult(True, f"edited {p} ({count if args.replace_all else 1} change(s))",
                          {"path": str(p), "replacements": count if args.replace_all else 1})


def _backup_to_recycle_bin(p: Path) -> None:
    """Put a copy of the current file in the Recycle Bin under its own name (restorable from there)."""
    from send2trash import send2trash

    tmp = Path(tempfile.mkdtemp(prefix="delta-backup-")) / p.name
    shutil.copy2(p, tmp)
    send2trash(str(tmp))


DESTRUCTIVE_CODE = [r"\bos\.(remove|unlink|rmdir|removedirs|kill)\b", r"\bshutil\.(rmtree|move)\b",
                    r"\.(unlink|rmdir)\(", r"\bsend2trash\b", r"\bsubprocess\b", r"\bos\.system\b",
                    r"\bwrite_text\(|\bopen\([^)]*['\"][wa]", r"\bwinreg\b", r"\bctypes\b"]
EXTERNAL_CODE = [r"\brequests\b", r"\burllib\b", r"\bhttpx\b", r"\bsocket\b", r"\bsmtplib\b", r"\bftplib\b"]


def classify_code(code: str) -> Risk:
    if any(re.search(p, code) for p in EXTERNAL_CODE):
        return Risk.EXTERNAL
    if any(re.search(p, code) for p in DESTRUCTIVE_CODE):
        return Risk.DESTRUCTIVE
    return Risk.WRITE


class PythonArgs(BaseModel):
    code: str = Field(description="A short Python 3.12 script. print() what the user needs to see.")
    cwd: str | None = Field(default=None, description="Folder the script runs in (default: a scratch folder)")
    timeout_s: int = Field(default=60, ge=1, le=600)


class RunPython(Tool[PythonArgs]):
    name = "run_python"
    description = ("Run a short Python script and get its printed output: calculations, bulk renames, CSV/Excel/"
                   "JSON processing, anything no other tool does in one step. Prefer it over many small steps.")
    risk = Risk.WRITE
    Args = PythonArgs

    def risk_for(self, args: PythonArgs) -> Risk:
        return classify_code(args.code)

    def paths(self, args: PythonArgs) -> list[str]:
        return [args.cwd] if args.cwd else []

    def summary(self, args: PythonArgs) -> str:
        return f"run Python ({len(args.code.splitlines())} lines)" + (f" in {args.cwd}" if args.cwd else "") + \
            ":\n" + args.code[:400]

    async def run(self, args: PythonArgs) -> ToolResult:
        cwd = str(norm_path(args.cwd)) if args.cwd else tempfile.mkdtemp(prefix="delta-py-")
        script = Path(tempfile.mkdtemp(prefix="delta-script-")) / "task.py"
        script.write_text(args.code, encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(script), cwd=cwd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        try:
            out, err = await asyncio.wait_for(proc.communicate(), args.timeout_s)
        except TimeoutError:
            proc.kill()
            return ToolResult(False, f"timed out after {args.timeout_s}s", {"exit_code": None})
        text = out.decode("utf-8", "replace")
        if err:
            text += "\n[stderr]\n" + err.decode("utf-8", "replace")
        code = proc.returncode
        return ToolResult(code == 0, f"exit {code}\n{text.strip()[:8000]}", {"exit_code": code})


def power_tools() -> list[Tool]:
    return [SearchFiles(), EditFile(), RunPython()]
