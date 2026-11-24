"""The coding tools of a Build run. Every path goes through delta.studio.jail; every change is backed up first
(delta.studio.store.StudioStore.back_up), so Undo can put it back. There is no tool that runs a program, reaches
the network or touches anything outside the project folder.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from delta.studio.jail import MEMORY_FILE, SKIP_DIRS, JailError, check_file, inside, is_secret, relative
from delta.studio.store import StudioStore, Turn

READ_CHARS = 24_000      # one read_file call returns at most this much
WRITE_CHARS = 400_000    # a file Delta writes is never bigger than this
TREE_ENTRIES = 400       # list_files stops here
SEARCH_HITS = 60
SEARCH_FILE_BYTES = 1_000_000  # bigger files are skipped by search


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


SPECS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "list_files", "description": "List the project's files and folders (generated folders such as "
        "node_modules are left out). Use a sub-folder to look deeper.",
        "parameters": _schema({"folder": {"type": "string", "description": "Folder inside the project; '.' = top"},
                               "depth": {"type": "integer", "description": "1 to 6, default 3"}}, [])}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a text file of the project, with line numbers. Long files come in "
        "parts: pass start_line to read further.",
        "parameters": _schema({"path": {"type": "string"},
                               "start_line": {"type": "integer", "description": "1 = from the top"}}, ["path"])}},
    {"type": "function", "function": {
        "name": "search", "description": "Find text in the project's files (case-insensitive, plain text or a "
        "regular expression). Returns file:line: text.",
        "parameters": _schema({"query": {"type": "string"},
                               "folder": {"type": "string", "description": "Search only here; default the whole "
                                                                          "project"}}, ["query"])}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Create a file, or replace a whole file, with the given content. "
        "Folders are created as needed. For a small change to an existing file use edit_file.",
        "parameters": _schema({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])}},
    {"type": "function", "function": {
        "name": "edit_file", "description": "Change part of an existing file: old_text must appear exactly once "
        "in it (copy it from read_file, without the line numbers) and is replaced by new_text.",
        "parameters": _schema({"path": {"type": "string"}, "old_text": {"type": "string"},
                               "new_text": {"type": "string"}}, ["path", "old_text", "new_text"])}},
    {"type": "function", "function": {
        "name": "delete_file", "description": "Delete one file of the project (it can be undone).",
        "parameters": _schema({"path": {"type": "string"}}, ["path"])}},
    {"type": "function", "function": {
        "name": "update_memory", "description": f"Replace the project memory ({MEMORY_FILE}): what the project "
        "is, its stack and how to run it, decisions made, and the next steps. Keep it short (at most 80 lines).",
        "parameters": _schema({"content": {"type": "string"}}, ["content"])}},
]


@dataclass
class Workspace:
    """One run's view of one project."""
    root: Path
    store: StudioStore
    project_id: str
    turn: Turn
    on_change: Callable[[str, str], None] = lambda rel, kind: None
    changed: dict[str, str] = field(default_factory=dict)  # rel -> created | changed | deleted

    # ---- helpers
    def _walk(self, folder: Path, depth: int) -> list[str]:
        out: list[str] = []
        base_depth = len(folder.relative_to(self.root).parts) if folder != self.root else 0
        for dirpath, dirnames, filenames in os.walk(folder):
            here = Path(dirpath)
            level = len(here.relative_to(self.root).parts) - base_depth if here != self.root else 0
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not (here / d).is_symlink())
            if level >= depth:
                dirnames[:] = []
            for d in dirnames:
                out.append(relative(self.root, here / d) + "/")
            for f in sorted(filenames):
                out.append(relative(self.root, here / f))
            if len(out) >= TREE_ENTRIES:
                break
        return sorted(out)[:TREE_ENTRIES]

    def tree(self, depth: int = 3) -> list[str]:
        return self._walk(self.root, depth)

    def _record(self, rel: str, path: Path, kind: str) -> None:
        self.store.back_up(self.project_id, self.turn, rel, path)
        first = self.changed.get(rel)
        self.changed[rel] = "created" if first == "created" and kind != "deleted" else kind
        if first == "created" and kind == "deleted":
            self.changed.pop(rel)
        self.on_change(rel, kind)

    # ---- the tools
    def list_files(self, folder: str = ".", depth: int = 3) -> str:
        path = inside(self.root, folder)
        if not path.is_dir():
            raise JailError(f"{folder} is not a folder in the project.")
        items = self._walk(path, max(1, min(6, int(depth or 3))))
        more = f" (stopped at {TREE_ENTRIES}: look inside one folder for more)" if len(items) >= TREE_ENTRIES else ""
        return ("\n".join(items) or "(empty)") + more

    def read_file(self, path: str, start_line: int = 1) -> str:
        real, rel = check_file(self.root, path)
        if not real.is_file():
            raise JailError(f"{rel} does not exist.")
        data = real.read_bytes()
        if b"\x00" in data[:4096]:
            return f"{rel} is a binary file ({len(data):,} bytes): it cannot be read as text."
        lines = data.decode("utf-8", errors="replace").splitlines()
        start = max(1, int(start_line or 1))
        out, used = [], 0
        for n in range(start, len(lines) + 1):
            line = f"{n}\t{lines[n - 1]}"
            if used + len(line) > READ_CHARS:
                out.append(f"... (more: read again with start_line={n}; {len(lines)} lines in all)")
                break
            out.append(line)
            used += len(line) + 1
        return f"{rel} ({len(lines)} lines)\n" + "\n".join(out)

    def search(self, query: str, folder: str = ".") -> str:
        base = inside(self.root, folder)
        try:
            rx = re.compile(query, re.I)
        except re.error:
            rx = re.compile(re.escape(query), re.I)
        hits: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not (Path(dirpath) / d).is_symlink()]
            for name in filenames:
                p = Path(dirpath) / name
                rel = relative(self.root, p)
                if is_secret(rel) or p.is_symlink():
                    continue
                try:
                    if p.stat().st_size > SEARCH_FILE_BYTES:
                        continue
                    text = p.read_text(encoding="utf-8", errors="strict")
                except (OSError, UnicodeDecodeError):
                    continue
                for n, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{rel}:{n}: {line.strip()[:200]}")
                        if len(hits) >= SEARCH_HITS:
                            return "\n".join(hits) + f"\n(stopped at {SEARCH_HITS} matches)"
        return "\n".join(hits) or "no matches"

    def write_file(self, path: str, content: str) -> str:
        real, rel = check_file(self.root, path)
        if len(content) > WRITE_CHARS:
            raise JailError(f"That is {len(content):,} characters; one file is at most {WRITE_CHARS:,}.")
        if real.is_dir():
            raise JailError(f"{rel} is a folder.")
        existed = real.is_file()
        self._record(rel, real, "changed" if existed else "created")
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text(content, encoding="utf-8", newline="")
        return f"{'replaced' if existed else 'created'} {rel} ({content.count(chr(10)) + 1} lines)"

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        real, rel = check_file(self.root, path)
        if not real.is_file():
            raise JailError(f"{rel} does not exist: use write_file to create it.")
        text = real.read_text(encoding="utf-8", errors="strict")
        crlf = "\r\n" in text
        body = text.replace("\r\n", "\n")
        old, new = old_text.replace("\r\n", "\n"), new_text.replace("\r\n", "\n")
        count = body.count(old) if old else 0
        if count != 1:
            why = "was not found" if count == 0 else f"appears {count} times"
            raise JailError(f"old_text {why} in {rel}. Read the file again and copy the exact text (one place only).")
        self._record(rel, real, "changed")
        body = body.replace(old, new)
        real.write_text(body.replace("\n", "\r\n") if crlf else body, encoding="utf-8", newline="")
        return f"edited {rel}"

    def delete_file(self, path: str) -> str:
        real, rel = check_file(self.root, path)
        if not real.is_file():
            raise JailError(f"{rel} does not exist.")
        self._record(rel, real, "deleted")
        real.unlink()
        return f"deleted {rel}"

    def update_memory(self, content: str) -> str:
        lines = content.strip().splitlines()[:80]
        return self.write_file(MEMORY_FILE, "\n".join(lines) + "\n").replace("created", "saved").replace(
            "replaced", "saved")

    def memory(self) -> str:
        path = self.root / MEMORY_FILE
        try:
            return path.read_text(encoding="utf-8")[:6000]
        except OSError:
            return ""

    def call(self, name: str, args: dict[str, Any]) -> str:
        fn = {s["function"]["name"]: getattr(self, s["function"]["name"]) for s in SPECS}.get(name)
        if fn is None:
            raise JailError(f"There is no tool called {name}.")
        return str(fn(**{k: v for k, v in (args or {}).items()
                         if k in SPECS_ARGS[name]}))


SPECS_ARGS = {s["function"]["name"]: set(s["function"]["parameters"]["properties"]) for s in SPECS}
