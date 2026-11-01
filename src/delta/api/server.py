"""Local HTTP + WebSocket API. One API for the dashboard, the desktop app, remote clients and tests.

Binds to 127.0.0.1 only. Every request needs the bearer token stored in <data_dir>/api_token
(created on first run). The dashboard receives it in the URL fragment, which browsers never send
to the server or write to logs.
"""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from delta.bootstrap import Container
from delta.core.state import TaskRequest, TaskResult
from delta.safety.approvals import ApprovalRequest

STATIC = Path(__file__).parent / "static"


def load_token(data_dir: Path) -> str:
    path = data_dir / "api_token"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32))
    return path.read_text().strip()


class TaskIn(BaseModel):
    text: str


class ApprovalIn(BaseModel):
    approved: bool


class SkillStatusIn(BaseModel):
    status: str = Field(pattern="^(candidate|active|retired)$")


class LearnedIn(BaseModel):
    action: str = Field(pattern="^(keep|undo)$")


class MemoryIn(BaseModel):
    content: str
    kind: str = "profile"


class Hub:
    """Fan-out of events and approval requests to connected WebSocket clients."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def broadcast(self, message: dict[str, Any]) -> None:
        dead = []
        for ws in self.clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


def _approval_json(r: ApprovalRequest) -> dict[str, Any]:
    return {"id": r.id, "tool": r.tool, "summary": r.summary, "risk": r.risk, "reason": r.reason,
            "task_id": r.task_id}


def _result_json(r: TaskResult) -> dict[str, Any]:
    return {"task_id": r.task_id, "status": r.status, "answer": r.answer, "attempts": r.attempts,
            "provider": r.provider, "steps": [{"tool": s.tool, "ok": s.ok} for s in r.steps]}


def create_app(c: Container, token: str) -> FastAPI:
    app = FastAPI(title="Delta", docs_url=None, redoc_url=None, openapi_url=None)
    hub = Hub()
    results: dict[str, dict[str, Any]] = {}
    running: set[asyncio.Task[None]] = set()
    bearer = HTTPBearer(auto_error=False)

    def auth(cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:  # noqa: B008
        if not cred or not secrets.compare_digest(cred.credentials, token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad token")

    async def on_event(event: str, data: dict[str, Any]) -> None:
        await hub.broadcast({"type": "event", "event": event, "data": data})

    async def on_approval(req: ApprovalRequest) -> None:
        await hub.broadcast({"type": "approval", "approval": _approval_json(req)})

    c.events.subscribe(on_event)
    c.approvals.add_presenter("api", on_approval)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status", dependencies=[Depends(auth)])
    async def get_status() -> dict[str, Any]:
        return {"name": c.settings.agent.name, "governor": c.governor.status(),
                "providers": c.router.usable(), "last_provider": c.router.last_provider,
                "tools": c.registry.names(), "autonomy": c.settings.boundaries.autonomy,
                "pending_approvals": len(c.approvals.pending()), "warnings": c.warnings}

    @app.post("/api/tasks", dependencies=[Depends(auth)])
    async def create_task(body: TaskIn) -> dict[str, str]:
        req = TaskRequest(body.text, channel="api")
        results[req.id] = {"task_id": req.id, "status": "running"}

        async def work() -> None:
            res = await c.submit(req)
            results[req.id] = _result_json(res)
            await hub.broadcast({"type": "result", "result": results[req.id]})

        t = asyncio.create_task(work())
        running.add(t)
        t.add_done_callback(running.discard)
        return {"task_id": req.id}

    @app.get("/api/tasks/{task_id}", dependencies=[Depends(auth)])
    async def get_task(task_id: str) -> dict[str, Any]:
        if task_id not in results:
            raise HTTPException(404, "unknown task")
        return results[task_id]

    @app.get("/api/approvals", dependencies=[Depends(auth)])
    async def list_approvals() -> list[dict[str, Any]]:
        return [_approval_json(r) for r in c.approvals.pending()]

    @app.post("/api/approvals/{approval_id}", dependencies=[Depends(auth)])
    async def answer_approval(approval_id: str, body: ApprovalIn) -> dict[str, bool]:
        if not c.approvals.resolve(approval_id, body.approved, by="dashboard"):
            raise HTTPException(404, "no such pending approval")
        return {"ok": True}

    @app.get("/api/memory", dependencies=[Depends(auth)])
    async def list_memory() -> list[dict[str, Any]]:
        return [m.__dict__ for m in c.memory.list()]

    @app.post("/api/memory", dependencies=[Depends(auth)])
    async def add_memory(body: MemoryIn) -> dict[str, int]:
        return {"id": c.memory.add(body.content, body.kind, source="user")}

    @app.delete("/api/memory/{mem_id}", dependencies=[Depends(auth)])
    async def delete_memory(mem_id: int) -> dict[str, bool]:
        return {"ok": c.memory.delete(mem_id)}

    @app.get("/api/skills", dependencies=[Depends(auth)])
    async def list_skills() -> list[dict[str, Any]]:
        store = c.orchestrator.skills
        return [s.to_dict() for s in store.list()] if store else []

    @app.post("/api/skills/{skill_id}/status", dependencies=[Depends(auth)])
    async def set_skill_status(skill_id: int, body: SkillStatusIn) -> dict[str, bool]:
        store = c.orchestrator.skills
        if not store or not store.set_status(skill_id, body.status):
            raise HTTPException(404, "no such skill")
        c.audit.record("skill", skill=skill_id, change=f"user set {body.status}")
        return {"ok": True}

    @app.delete("/api/skills/{skill_id}", dependencies=[Depends(auth)])
    async def delete_skill(skill_id: int) -> dict[str, bool]:
        store = c.orchestrator.skills
        return {"ok": bool(store and store.delete(skill_id))}

    @app.get("/api/skills/export", dependencies=[Depends(auth)])
    async def export_skills() -> list[dict[str, Any]]:
        store = c.orchestrator.skills
        return store.export() if store else []

    @app.get("/api/learned", dependencies=[Depends(auth)])
    async def list_learned() -> list[dict[str, Any]]:
        store = c.orchestrator.learning
        return [i.to_dict() for i in store.list()] if store else []

    @app.post("/api/learned/{item_id}", dependencies=[Depends(auth)])
    async def answer_learned(item_id: int, body: LearnedIn) -> dict[str, Any]:
        store = c.orchestrator.learning
        item = store.set_status(item_id, "active" if body.action == "keep" else "undone") if store else None
        if not item:
            raise HTTPException(404, "no such learned item")
        c.audit.record("learned", item=item_id, user_action=body.action, kind=item.kind)
        return item.to_dict()

    @app.get("/api/addons", dependencies=[Depends(auth)])
    async def list_addons() -> list[dict[str, Any]]:
        return [i.__dict__ for i in c.addon_infos]

    @app.get("/api/boundaries", dependencies=[Depends(auth)])
    async def get_boundaries() -> dict[str, Any]:
        return c.settings.boundaries.model_dump()

    @app.get("/api/audit", dependencies=[Depends(auth)])
    async def get_audit(n: int = 30) -> list[dict[str, Any]]:
        return c.audit.tail(min(n, 200))

    @app.post("/api/shutdown", dependencies=[Depends(auth)])
    async def shutdown() -> dict[str, bool]:
        c.lifecycle.request_stop()
        return {"ok": True}

    from delta.api.settings_routes import register as register_settings
    from delta.api.setup_routes import register as register_setup

    register_setup(app, c, auth)
    register_settings(app, c, auth)

    @app.websocket("/api/ws")
    async def ws(socket: WebSocket) -> None:
        if not secrets.compare_digest(socket.query_params.get("token", ""), token):
            await socket.close(code=4401)
            return
        await socket.accept()
        hub.clients.add(socket)
        for r in c.approvals.pending():
            await socket.send_json({"type": "approval", "approval": _approval_json(r)})
        try:
            while True:
                await socket.receive_text()  # keepalive; clients act through the REST endpoints
        except WebSocketDisconnect:
            hub.clients.discard(socket)

    return app
