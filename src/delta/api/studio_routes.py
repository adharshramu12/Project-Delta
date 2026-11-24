"""Delta Build routes (the Build panel). Every route needs the dashboard token; nothing here is reachable from
Telegram or Slack. Live progress arrives on the dashboard's one WebSocket as studio.* events.

  GET    /api/studio                         projects, and the folder new projects are made in
  POST   /api/studio/projects                {"name"} new project | {"path", "name"?} open an existing folder
  GET    /api/studio/projects/{id}           files, memory, chat, recent changes, running or not
  DELETE /api/studio/projects/{id}           remove from the list (the files stay)
  GET    /api/studio/projects/{id}/file      ?path=  one file's text
  POST   /api/studio/projects/{id}/run       {"text"} start a request
  POST   /api/studio/projects/{id}/stop
  POST   /api/studio/projects/{id}/undo      put back the files of the latest request
  GET    /api/studio/projects/{id}/diff      ?turn=  what a request changed
  DELETE /api/studio/projects/{id}/history   clear the chat (files and memory stay)
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from delta.studio.service import Studio, StudioError


class ProjectIn(BaseModel):
    name: str = Field(default="", max_length=80)
    path: str = Field(default="", max_length=1000)


class RunIn(BaseModel):
    text: str = Field(min_length=1, max_length=40_000)


def register(app: FastAPI, c: Any, auth: Callable[..., None]) -> None:
    def studio() -> Studio:
        s = getattr(c, "studio", None)
        if s is None:
            raise HTTPException(503, "Delta Build is switched off (studio.enabled in settings).")
        return s

    def safe(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except StudioError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/studio", dependencies=[Depends(auth)])
    async def overview() -> dict[str, Any]:
        s = studio()
        return {"home": str(s.projects_home),
                "projects": [p.public() | {"running": s.running(p.id), "exists": Path(p.path).is_dir()}
                             for p in s.store.projects()]}

    @app.post("/api/studio/projects", dependencies=[Depends(auth)])
    async def add(body: ProjectIn) -> dict[str, Any]:
        s = studio()
        p = safe(lambda: s.open(body.path, body.name) if body.path.strip() else s.create(body.name))
        return p.public()

    @app.get("/api/studio/projects/{pid}", dependencies=[Depends(auth)])
    async def project(pid: str) -> dict[str, Any]:
        return safe(lambda: studio().summary(pid))

    @app.delete("/api/studio/projects/{pid}", dependencies=[Depends(auth)])
    async def forget(pid: str) -> dict[str, bool]:
        return {"ok": bool(safe(lambda: studio().forget(pid)))}

    @app.get("/api/studio/projects/{pid}/file", dependencies=[Depends(auth)])
    async def read(pid: str, path: str) -> dict[str, Any]:
        return safe(lambda: studio().read(pid, path))

    @app.post("/api/studio/projects/{pid}/run", dependencies=[Depends(auth)])
    async def run(pid: str, body: RunIn) -> dict[str, str]:
        return {"turn": safe(lambda: studio().start(pid, body.text))}

    @app.post("/api/studio/projects/{pid}/stop", dependencies=[Depends(auth)])
    async def stop(pid: str) -> dict[str, bool]:
        return {"ok": studio().stop(pid)}

    @app.post("/api/studio/projects/{pid}/undo", dependencies=[Depends(auth)])
    async def undo(pid: str) -> dict[str, Any]:
        return safe(lambda: studio().undo(pid))

    @app.get("/api/studio/projects/{pid}/diff", dependencies=[Depends(auth)])
    async def diff(pid: str, turn: str) -> list[dict[str, Any]]:
        return safe(lambda: studio().diff(pid, turn))

    @app.delete("/api/studio/projects/{pid}/history", dependencies=[Depends(auth)])
    async def clear(pid: str) -> dict[str, bool]:
        s = studio()
        safe(lambda: s._project(pid))
        s.store.clear_history(pid)
        return {"ok": True}
