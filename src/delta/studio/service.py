"""The Build service: projects, one run at a time per project, Stop, Undo, and live events for the dashboard.

Runs are independent of Delta's own task lock: a Build run never blocks Delta's PC agent, and Delta's tasks never
block a Build run. Both use the same AI keys, so they share each provider's rate limits.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import time
from pathlib import Path
from typing import Any

from delta.studio import agent
from delta.studio.jail import MEMORY_FILE, JailError, check_file, check_project_root
from delta.studio.store import Project, StudioStore
from delta.studio.tools import Workspace
from delta.system.logging import log

DIFF_CHARS = 60_000
VIEW_CHARS = 200_000


class StudioError(ValueError):
    """Something the user asked for that cannot be done; the message is for the user."""


class Studio:
    def __init__(self, c: Any, data_dir: Path, app_root: Path, projects_home: Path) -> None:
        self.c = c
        self.data_dir, self.app_root = data_dir, app_root
        self.projects_home = projects_home  # where "New project" folders are made (Documents/Delta Projects)
        self.store = StudioStore(data_dir / "studio")
        self._runs: dict[str, asyncio.Task[None]] = {}

    # ---- projects
    def _project(self, pid: str) -> tuple[Project, Path]:
        p = self.store.get(pid) if pid.isalnum() else None
        if p is None:
            raise StudioError("That project is not in the list.")
        try:
            root = check_project_root(p.path, self.data_dir, self.app_root)
        except JailError as e:
            raise StudioError(f"The project folder cannot be used: {e}") from e
        return p, root

    def create(self, name: str) -> Project:
        clean = "".join(ch for ch in name.strip() if ch.isalnum() or ch in " -_.").strip(" .")[:60]
        if not clean:
            raise StudioError("Give the new project a name (letters, numbers, spaces, - or _).")
        folder = self.projects_home / clean
        if folder.exists() and any(folder.iterdir()):
            raise StudioError(f"{folder} already exists and is not empty: open it as an existing project instead.")
        folder.mkdir(parents=True, exist_ok=True)
        root = check_project_root(folder, self.data_dir, self.app_root)
        p = self.store.add(clean, root)
        self._audit("studio.project_created", project=p.id, path=str(root))
        return p

    def open(self, path: str, name: str = "") -> Project:
        try:
            root = check_project_root(path, self.data_dir, self.app_root)
        except JailError as e:
            raise StudioError(str(e)) from e
        p = self.store.add(name or root.name, root)
        self._audit("studio.project_opened", project=p.id, path=str(root))
        return p

    def forget(self, pid: str) -> bool:
        if pid in self._runs:
            raise StudioError("Stop the running request first.")
        return self.store.forget(pid)

    def summary(self, pid: str) -> dict[str, Any]:
        p, root = self._project(pid)
        self.store.touch(pid)
        ws = Workspace(root, self.store, pid, _NoTurn())  # type: ignore[arg-type]
        return {**p.public(), "running": pid in self._runs, "tree": ws.tree(depth=6),
                "memory": ws.memory(), "history": self.store.history(pid),
                "turns": [t.__dict__ for t in self.store.turns(pid)[:10]]}

    def read(self, pid: str, rel: str) -> dict[str, Any]:
        _, root = self._project(pid)
        try:
            path, rel = check_file(root, rel)
        except JailError as e:
            raise StudioError(str(e)) from e
        if not path.is_file():
            raise StudioError(f"{rel} does not exist.")
        data = path.read_bytes()
        if b"\x00" in data[:4096]:
            return {"path": rel, "binary": True, "size": len(data), "text": ""}
        text = data.decode("utf-8", errors="replace")
        return {"path": rel, "binary": False, "size": len(data), "text": text[:VIEW_CHARS],
                "cut": len(text) > VIEW_CHARS}

    def diff(self, pid: str, turn_id: str) -> list[dict[str, Any]]:
        _, root = self._project(pid)
        turn = next((t for t in self.store.turns(pid) if t.id == turn_id), None)
        if turn is None:
            raise StudioError("That change is no longer kept.")
        out, used = [], 0
        for rel in turn.changed:
            before = self.store.original(pid, turn_id, rel)
            path = root / rel
            after = path.read_bytes() if path.is_file() else None
            a = (before or b"").decode("utf-8", errors="replace").splitlines()
            b = (after or b"").decode("utf-8", errors="replace").splitlines()
            text = "\n".join(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}", lineterm="", n=3))
            text = text[:max(0, DIFF_CHARS - used)]
            used += len(text)
            out.append({"path": rel, "kind": "created" if before is None else "deleted" if after is None else
                        "changed", "diff": text})
        return out

    # ---- runs
    def running(self, pid: str) -> bool:
        return pid in self._runs

    def start(self, pid: str, text: str) -> str:
        p, root = self._project(pid)
        request = " ".join(text.split()) if len(text) < 200 else text.strip()
        if not request:
            raise StudioError("Write what Delta should build or change.")
        if pid in self._runs:
            raise StudioError("Delta is still working on this project. Wait, or press Stop.")
        turn = self.store.new_turn(pid, request)
        history = self.store.history(pid)
        self.store.say(pid, "user", request[:20_000], turn=turn.id)
        task = asyncio.create_task(self._run(p, root, turn, request, history), name=f"studio-{pid}")
        self._runs[pid] = task
        task.add_done_callback(lambda _t: self._runs.pop(pid, None))
        return turn.id

    def stop(self, pid: str) -> bool:
        task = self._runs.get(pid)
        if task is None:
            return False
        task.cancel()
        return True

    async def _run(self, p: Project, root: Path, turn: Any, request: str, history: list[dict[str, Any]]) -> None:
        async def emit(event: str, data: dict[str, Any]) -> None:
            with contextlib.suppress(Exception):
                await self.c.events.publish(event, project=p.id, turn=turn.id, **data)

        ws = Workspace(root, self.store, p.id, turn,
                       on_change=lambda rel, kind: self._audit("studio.file", project=p.id, path=rel, change=kind))
        started = time.monotonic()
        status, answer = "done", ""
        try:
            res = await agent.run(self.c.router, ws, p.name, request, history, emit)
            answer = res.answer
            status = "done" if not res.stopped else "stopped"
        except asyncio.CancelledError:
            status, answer = "cancelled", "Stopped. " + ("The files changed so far can be undone." if ws.changed
                                                         else "Nothing was changed.")
        except Exception as e:
            log.exception("studio.run_failed", project=p.id)
            status = "failed"
            answer = f"This request could not finish: {getattr(e, 'user_message', '') or e}"
        changed = dict(ws.changed)
        if changed and MEMORY_FILE not in changed:
            self._log_to_memory(ws, request, changed)
        self.store.say(p.id, "assistant", answer, turn=turn.id, status=status, changed=changed,
                       seconds=round(time.monotonic() - started, 1))
        self.store.touch(p.id, ran=True)
        self._audit("studio.run", project=p.id, turn=turn.id, status=status, files=len(changed))
        await emit("studio.done", {"status": status, "answer": answer, "changed": changed})

    @staticmethod
    def _log_to_memory(ws: Workspace, request: str, changed: dict[str, str]) -> None:
        """When the model changed files but not the memory, keep a dated line of it there (no AI call)."""
        try:
            text = ws.memory().rstrip()
            if "## Log" not in text:
                text = (text + "\n\n" if text else "# Project memory\n\n") + "## Log"
            files = ", ".join(sorted(changed)[:8]) + (" …" if len(changed) > 8 else "")
            line = f"- {time.strftime('%Y-%m-%d')}: {' '.join(request.split())[:120]} ({files})"
            lines = text.splitlines()
            head = lines[:lines.index("## Log") + 1]
            log_lines = [*[x for x in lines[len(head):] if x.strip()][-19:], line]
            ws.write_file(MEMORY_FILE, "\n".join(head + log_lines) + "\n")
        except Exception:
            log.exception("studio.memory_log_failed")

    def undo(self, pid: str) -> dict[str, Any]:
        if pid in self._runs:
            raise StudioError("Wait for the running request to finish (or Stop it) before undoing.")
        _, root = self._project(pid)
        turn, restored = self.store.undo(pid, root, check_file)
        if turn is None:
            raise StudioError("There is nothing to undo.")
        self.store.say(pid, "system", f"Undid: {turn.request[:120]} ({len(restored)} file(s) put back).")
        self._audit("studio.undo", project=pid, turn=turn.id, files=len(restored))
        return {"turn": turn.id, "restored": restored}

    def _audit(self, event: str, **data: Any) -> None:
        with contextlib.suppress(Exception):
            self.c.audit.record(event, **data)


class _NoTurn:
    """summary() only lists files: a workspace without a turn, which is never asked to write."""

    def __init__(self) -> None:
        self.id = ""
        self.changed: list[str] = []
