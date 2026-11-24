"""The guard Claude Code runs before each file action of a Build request (a PreToolUse hook, milestone 4).

Claude Code calls `python -m delta.studio.hook --root R --store S --project P --turn T` with the planned action as
JSON on stdin. This program:
- refuses (exit code 2, the reason on stderr, which Claude Code shows the model) any path outside the project
  folder, any secret file and anything in .git: the same jail as Delta's own engine (delta.studio.jail);
- before a change (Edit, Write, MultiEdit, NotebookEdit), saves the file as it was into the request's backups
  (delta.studio.store), so Undo puts it back exactly as with Delta's own engine;
- refuses every tool that is not a file tool (the engine is started with file tools only; this is a second lock).

It must never crash open: any error refuses the action.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from delta.studio.jail import JailError, check_file, inside, relative
from delta.studio.store import StudioStore

READING = {"Read", "Glob", "Grep", "LS"}
CHANGING = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def paths_of(tool: str, data: dict[str, Any]) -> list[str]:
    """Every path the action names (none for a search of the whole project)."""
    keys = ("file_path", "notebook_path", "path")
    return [str(data[k]) for k in keys if isinstance(data.get(k), str) and data[k].strip()]


def decide(event: dict[str, Any], root: Path, store: StudioStore, project: str, turn_id: str) -> str:
    """"" = allowed; otherwise the reason it is refused."""
    tool = str(event.get("tool_name") or "")
    data = event.get("tool_input") or {}
    if not isinstance(data, dict):
        return "This action is not valid."
    if tool not in READING | CHANGING:
        return f"Delta Build allows only file tools inside the project; {tool or 'this tool'} is not allowed."
    for raw in paths_of(tool, data):
        try:
            if tool in ("Glob", "Grep", "LS"):
                target = inside(root, raw)  # a folder to search: inside the project
                if target != root:
                    rel = relative(root, target)
                    if any(part == ".git" for part in Path(rel).parts):
                        return "The .git folder is git's own: Delta Build never reads or changes it."
                continue
            path, rel = check_file(root, raw)
        except JailError as e:
            return str(e)
        if tool in CHANGING:
            turn, _ = store._manifest(project, turn_id)
            store.back_up(project, turn, rel, path)
    if tool in ("Glob", "Grep") and str(data.get("pattern", "")).replace("\\", "/").startswith(("/", "..")):
        return "Search patterns must stay inside the project."
    return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    for name in ("--root", "--store", "--project", "--turn"):
        ap.add_argument(name, required=True)
    args = ap.parse_args(argv)
    try:
        event = json.loads(sys.stdin.read() or "{}")
        reason = decide(event, Path(args.root).resolve(), StudioStore(Path(args.store)), args.project, args.turn)
    except Exception as e:  # never fail open
        reason = f"Delta Build could not check this action ({type(e).__name__}), so it was not done."
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
