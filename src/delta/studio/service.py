"""The Build service: projects, one run at a time per project, Stop, Undo, and live events for the dashboard.

Runs are independent of Delta's own task lock: a Build run never blocks Delta's PC agent, and Delta's tasks never
block a Build run. Both use the same AI keys, so they share each provider's rate limits.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import time
import uuid
from pathlib import Path
from typing import Any

from delta.core.orchestrator import friendly_model_error
from delta.llm.schemas import LLMError
from delta.studio import agent, claude_engine, commands, preview
from delta.studio.jail import MEMORY_FILE, JailError, check_file, check_project_root
from delta.studio.store import Project, StudioStore
from delta.studio.tools import Workspace
from delta.system.logging import log

ASK_TIMEOUT_S = 600  # a command nobody answers in 10 minutes is taken as "no"
DIFF_CHARS = 60_000
VIEW_CHARS = 200_000


class StudioError(ValueError):
    """Something the user asked for that cannot be done; the message is for the user."""


class Studio:
    def __init__(self, c: Any, data_dir: Path, app_root: Path, projects_home: Path,
                 extra_folders: list[Path] | None = None) -> None:
        self.c = c
        self.data_dir, self.app_root = data_dir, app_root
        self.projects_home = projects_home  # where "New project" folders are made (Documents/Delta Projects)
        # where a project may be (delta.studio.jail.check_project_root)
        self.allowed = [Path.home(), projects_home, *(extra_folders or [])]
        self.store = StudioStore(data_dir / "studio")
        self._runs: dict[str, asyncio.Task[None]] = {}
        # Commands waiting for the user's yes: ask id -> (project id, what is asked, the answer to come)
        self._asks: dict[str, tuple[str, dict[str, Any], asyncio.Future[tuple[bool, bool]]]] = {}
        self._previews: dict[str, preview.Preview] = {}  # project id -> its running preview

    # ---- projects
    def _project(self, pid: str) -> tuple[Project, Path]:
        p = self.store.get(pid) if pid.isalnum() else None
        if p is None:
            raise StudioError("That project is not in the list.")
        try:
            root = check_project_root(p.path, self.data_dir, self.app_root, self.allowed)
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
        root = check_project_root(folder, self.data_dir, self.app_root, self.allowed)
        p = self.store.add(clean, root)
        self._audit("studio.project_created", project=p.id, path=str(root))
        return p

    def open(self, path: str, name: str = "") -> Project:
        try:
            root = check_project_root(path, self.data_dir, self.app_root, self.allowed)
        except JailError as e:
            raise StudioError(str(e)) from e
        p = self.store.add(name or root.name, root)
        self._audit("studio.project_opened", project=p.id, path=str(root))
        return p

    async def forget(self, pid: str) -> bool:
        if pid in self._runs:
            raise StudioError("Stop the running request first.")
        await self.stop_preview(pid)
        return self.store.forget(pid)

    def summary(self, pid: str) -> dict[str, Any]:
        p, root = self._project(pid)
        self.store.touch(pid)
        ws = Workspace(root, self.store, pid, _NoTurn())  # type: ignore[arg-type]
        return {**p.public(), "running": pid in self._runs, "tree": ws.tree(depth=6),
                "asks": [ask for owner, ask, fut in self._asks.values() if owner == pid and not fut.done()],
                "engines": {"claude_code": claude_engine.executable() is not None},
                "preview": pv.public() if (pv := self._previews.get(pid)) is not None and pv.alive else None,
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

        async def runner(tool: str, args: dict[str, Any]) -> str:
            if tool == "run_command":
                return await self._command(p.id, root, args, emit)
            return await self._preview_tool(p.id, root, tool, args, emit)

        started = time.monotonic()
        status, answer = "done", ""
        try:
            if p.engine == "claude_code":
                res = await claude_engine.run(root, self.store.root, p.id, turn.id, request, history, emit)
                ws.changed.update(self._engine_changes(p.id, turn.id, root))
            else:
                res = await agent.run(self.c.router, ws, p.name, request, history, emit, runner=runner)
            answer = res.answer
            status = "done" if not res.stopped else "stopped"
        except asyncio.CancelledError:
            if p.engine == "claude_code":
                ws.changed.update(self._engine_changes(p.id, turn.id, root))
            self._drop_asks(p.id)
            status, answer = "cancelled", "Stopped. " + ("The files changed so far can be undone." if ws.changed
                                                         else "Nothing was changed.")
        except LLMError as e:  # no AI could answer: every key and backup is out of credit, at its limit or down
            log.warning("studio.no_ai", project=p.id, error=str(e)[:300])
            status = "failed"
            answer = no_ai_message(e)
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

    # ---- commands (milestone 2)
    async def _command(self, pid: str, root: Path, args: dict[str, Any], emit: Any) -> str:
        """Run one command for the agent: refused, asked, or allowed; then run in the project folder."""
        command = " ".join(str(args.get("command") or "").split())
        why_refused, sends = commands.check(command)
        if why_refused:
            self._audit("studio.command_refused", project=pid, command=command[:300])
            return f"ERROR: {why_refused}"
        if not await self._approved(pid, command, str(args.get("why") or ""), sends, emit):
            return ("ERROR: the user did not allow this command. Carry on without it, and tell them what to run "
                    "themselves if it matters.")
        await emit("studio.command", {"command": command})
        outcome = await commands.run(command, root, float(args.get("timeout_s") or commands.DEFAULT_TIMEOUT_S))
        self._audit("studio.command", project=pid, command=command[:300], exit=outcome.exit_code,
                    timed_out=outcome.timed_out, seconds=round(outcome.seconds, 1))
        await emit("studio.command_done", {"command": command, "exit": outcome.exit_code,
                                           "timed_out": outcome.timed_out, "seconds": round(outcome.seconds, 1),
                                           "tail": outcome.output[-1500:]})
        return outcome.for_model()

    async def _approved(self, pid: str, command: str, why: str, sends: bool, emit: Any, kind: str = "command") -> bool:
        """The user's yes for a command (asked in the Build page), unless it is always allowed in this project."""
        p = self.store.get(pid)
        if not sends and p is not None and command in p.allowed:
            return True
        ask_id = uuid.uuid4().hex[:12]
        ask = {"id": ask_id, "command": command, "why": why[:200], "sends": sends, "kind": kind}
        fut: asyncio.Future[tuple[bool, bool]] = asyncio.get_running_loop().create_future()
        self._asks[ask_id] = (pid, ask, fut)
        await emit("studio.approve", ask)
        try:
            allow, always = await asyncio.wait_for(asyncio.shield(fut), ASK_TIMEOUT_S)
        except TimeoutError:
            allow, always = False, False
        finally:
            self._asks.pop(ask_id, None)
        await emit("studio.approved", {"id": ask_id, "allow": allow})
        self._audit("studio.command_answer", project=pid, command=command[:300], allow=allow, always=always,
                    kind=kind)
        if allow and always and not sends:
            self.store.allow(pid, command)
        return allow

    # ---- live preview (milestone 3)
    async def _preview_tool(self, pid: str, root: Path, tool: str, args: dict[str, Any], emit: Any) -> str:
        if tool == "start_preview":
            command = " ".join(str(args.get("command") or "").split())
            if command:
                why_refused, sends = commands.check(command)
                if why_refused:
                    return f"ERROR: {why_refused}"
                why = str(args.get("why") or "start the dev server for the live preview")
                if not await self._approved(pid, command, why, sends, emit, kind="preview"):
                    return "ERROR: the user did not allow starting this server. Tell them the command instead."
            try:
                started = await self.start_preview(pid, command, emit)
            except (RuntimeError, OSError, StudioError) as e:
                return f"ERROR: {e}"
            return (f"The preview is running at {started.url} and is shown to the user.\n"
                    + await preview.page_check(started.url))
        pv = self._previews.get(pid)
        if tool == "stop_preview":
            await self.stop_preview(pid, emit)
            return "stopped" if pv else "there was no preview running"
        if pv is None or not pv.alive:
            return "ERROR: no preview is running. Start one with start_preview."
        if tool == "check_preview":
            path = str(args.get("path") or "").lstrip("/")
            return await preview.page_check(pv.url + path)
        return pv.tail(int(args.get("lines") or 60)) or "(no output yet)"  # preview_logs

    async def start_preview(self, pid: str, command: str = "", emit: Any = None) -> preview.Preview:
        """Start (or restart) the project's preview. Without a command, Delta serves the folder itself."""
        _, root = self._project(pid)
        await self.stop_preview(pid, emit)
        pv = await preview.start_server(command, root) if command else preview.start_static(root)
        self._previews[pid] = pv
        self._audit("studio.preview", project=pid, kind=pv.kind, url=pv.url, command=command[:300])
        if emit is not None:
            await emit("studio.preview", pv.public())
        else:
            await self._publish(pid, "studio.preview", pv.public())
        return pv

    async def stop_preview(self, pid: str, emit: Any = None) -> bool:
        pv = self._previews.pop(pid, None)
        if pv is None:
            return False
        await pv.stop()
        if emit is not None:
            await emit("studio.preview", {"alive": False})
        else:
            await self._publish(pid, "studio.preview", {"alive": False})
        return True

    def preview_logs(self, pid: str) -> dict[str, Any]:
        pv = self._previews.get(pid)
        return {"preview": pv.public() if pv else None, "logs": pv.tail(200) if pv else ""}

    async def _publish(self, pid: str, event: str, data: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await self.c.events.publish(event, project=pid, **data)

    async def close(self) -> None:
        """At Delta's shutdown: stop every run and preview."""
        for task in list(self._runs.values()):
            task.cancel()
        for pid in list(self._previews):
            with contextlib.suppress(Exception):
                await self.stop_preview(pid)

    def _engine_changes(self, pid: str, turn_id: str, root: Path) -> dict[str, str]:
        """What Claude Code changed, from the backups its guard made: a backed-up file now missing was deleted."""
        out = {}
        for rel, kind in self.store.changed_in(pid, turn_id).items():
            out[rel] = "deleted" if kind == "changed" and not (root / rel).exists() else kind
        return {rel: kind for rel, kind in out.items() if not (kind == "created" and not (root / rel).exists())}

    def set_engine(self, pid: str, engine: str) -> dict[str, Any]:
        self._project(pid)
        if pid in self._runs:
            raise StudioError("Wait for the running request to finish before changing the engine.")
        if engine == "claude_code" and claude_engine.executable() is None:
            raise StudioError("Claude Code is not installed on this PC.")
        try:
            self.store.set_engine(pid, engine)
        except ValueError as e:
            raise StudioError("Choose Delta or Claude Code.") from e
        self._audit("studio.engine", project=pid, engine=engine)
        return {"engine": engine}

    def answer(self, pid: str, ask_id: str, allow: bool, always: bool = False) -> bool:
        """The user's answer to a command waiting in the Build page."""
        found = self._asks.get(ask_id)
        if found is None or found[0] != pid or found[2].done():
            raise StudioError("That question is no longer waiting (it was answered, timed out or stopped).")
        found[2].set_result((bool(allow), bool(always)))
        return True

    def disallow(self, pid: str, command: str) -> None:
        self._project(pid)
        self.store.allow(pid, command, on=False)

    def _drop_asks(self, pid: str) -> None:
        for ask_id, (owner, _ask, fut) in list(self._asks.items()):
            if owner == pid:
                if not fut.done():
                    fut.cancel()
                self._asks.pop(ask_id, None)

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


def no_ai_message(e: LLMError) -> str:
    """Every AI the user has is out of credit, at its limit or down: say which, why, and what to do, in plain words."""
    errors = list(getattr(e, "errors", []) or [])
    kinds = list(getattr(e, "kinds", []) or [])
    lines = []
    for err, kind in zip(errors, kinds, strict=False):
        name, _, why = err.partition(": ")
        why = why.split(". ")[0][:120] or kind
        tag = {"quota": "out of credit or at its limit for today", "rate_limit": "busy (too many requests just now)",
               "auth": "its key was refused", "unreachable": "not running or not reachable"}.get(kind, "failed")
        lines.append(f"- {name}: {tag} ({why})")
    limited = bool(kinds) and all(k in ("quota", "rate_limit", "unreachable") for k in kinds)
    head = ("All your AIs are out of credit or at their limit right now, so this request stopped:" if limited and lines
            else friendly_model_error(e) if not lines else "No AI could answer, so this request stopped:")
    return "\n".join([head, *lines, "",
                      "Files changed before it stopped are kept and can be undone. What you can do:",
                      "- Wait and send it again: free limits reset (per minute, or each day).",
                      "- Add backup AIs (Apps > AI models): Delta moves to the next one by itself when one runs out.",
                      "- Add another free key (Settings > Keys): Gemini, Groq, NVIDIA and OpenRouter all have one.",
                      "- Or switch this project's engine to Claude Code, which uses your Claude plan."])


class _NoTurn:
    """summary() only lists files: a workspace without a turn, which is never asked to write."""

    def __init__(self) -> None:
        self.id = ""
        self.changed: list[str] = []
