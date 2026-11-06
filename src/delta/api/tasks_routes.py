"""Task history and run timeline for the dashboard (delta.engine).

Same bearer token as every other route. The dashboard is this PC (a local channel); phones resume through
Telegram, where Remote Guard asks for the PIN (delta.engine.resume).

  GET  /api/tasks                       recent tasks; filters: state (comma list), channel, q, since_hours
  GET  /api/tasks/{id}/timeline         one task: its steps from the event log, state changes, checks
  POST /api/tasks/{id}/resume           {"confirm_last"?: "done" | "not_done"} -> started, or what to ask first
  POST /api/tasks/{id}/discard          an interrupted task: nothing more runs for it
  GET  /tasks.js                        the dashboard's Tasks panel
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from delta.engine.resume import discard, prepare_resume
from delta.engine.states import TaskState

STATIC = Path(__file__).parent / "static"


class ResumeIn(BaseModel):
    confirm_last: str | None = Field(default=None, pattern="^(done|not_done)$")


def _short(task: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "text", "channel", "state", "created", "updated", "finished", "steps", "tokens_in", "tokens_out",
            "calls", "provider", "resumes", "autonomy", "chat_id")
    out = {k: task.get(k) for k in keep}
    out["answer"] = str(task.get("answer") or "")[:300]
    end = task.get("finished") or task.get("updated") or task.get("created") or 0
    out["wall_s"] = round(float(end) - float(task.get("created") or end), 1)
    return out


def register(app: FastAPI, c: Any, auth: Callable[..., None], launch: Callable[..., dict[str, str]]) -> None:
    def engine() -> Any:
        if getattr(c, "engine", None) is None:
            raise HTTPException(503, "the task engine is switched off (engine.enabled in settings)")
        return c.engine

    @app.get("/tasks.js")
    async def tasks_js() -> FileResponse:
        return FileResponse(STATIC / "tasks.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/api/tasks", dependencies=[Depends(auth)])
    async def list_tasks(state: str = "", channel: str = "", q: str = "", since_hours: float = 0,
                         limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if getattr(c, "engine", None) is None:
            return {"enabled": False, "tasks": [], "states": [s.value for s in TaskState]}
        states = [s.strip() for s in state.split(",") if s.strip()]
        if any(s not in {t.value for t in TaskState} for s in states):
            raise HTTPException(400, "unknown state")
        since = time.time() - since_hours * 3600 if since_hours > 0 else 0
        rows = c.engine.store.list(states, channel.strip(), q.strip()[:200], since, limit, offset)
        return {"enabled": True, "tasks": [_short(t) for t in rows], "states": [s.value for s in TaskState]}

    @app.get("/api/tasks/{task_id}/timeline", dependencies=[Depends(auth)])
    async def timeline(task_id: str) -> dict[str, Any]:
        if not task_id.isalnum():
            raise HTTPException(404, "unknown task")
        data = engine().timeline(task_id)
        if data is None:
            raise HTTPException(404, "unknown task")
        data["task"] = _short(data["task"]) | {"answer": str(data["task"].get("answer") or "")}
        return dict(data)

    @app.post("/api/tasks/{task_id}/resume", dependencies=[Depends(auth)])
    async def resume(task_id: str, body: ResumeIn | None = None) -> dict[str, Any]:
        engine()
        decision = prepare_resume(c, task_id, "api", "owner", body.confirm_last if body else None)
        out = decision.to_dict()
        if decision.ok and decision.request is not None:
            out |= launch(decision.request, decision.request.chat_id, decision.autonomy_cap)
        return out

    @app.post("/api/tasks/{task_id}/discard", dependencies=[Depends(auth)])
    async def discard_task(task_id: str) -> dict[str, Any]:
        engine()
        ok, message = discard(c, task_id, "api")
        return {"ok": ok, "message": message}
