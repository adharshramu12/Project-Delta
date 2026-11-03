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
    chat_id: str = ""  # empty: a new chat is started


class ChatPatch(BaseModel):
    title: str | None = None
    pinned: bool | None = None


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
    v = r.verification
    return {"task_id": r.task_id, "status": r.status, "answer": r.answer, "attempts": r.attempts,
            "provider": r.provider, "steps": [{"tool": s.tool, "ok": s.ok} for s in r.steps],
            "checks": v.checks if v else [], "seen_screen": bool(v and v.screenshot), "tokens": r.tokens,
            "fallback": r.fallback,
            "options": r.options, "questions": r.questions, "asked_about": r.asked_about}


def create_app(c: Container, token: str) -> FastAPI:
    app = FastAPI(title="Delta", docs_url=None, redoc_url=None, openapi_url=None)
    hub = Hub()
    results: dict[str, dict[str, Any]] = {}
    by_id: dict[str, asyncio.Task[None]] = {}  # running tasks, so the dashboard can stop one
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
        # Never cached: after an update the app window otherwise kept showing the old page.
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/favicon.ico")
    async def favicon() -> FileResponse:
        return FileResponse(STATIC / "delta.ico")

    @app.get("/api/status", dependencies=[Depends(auth)])
    async def get_status() -> dict[str, Any]:
        return {"name": c.settings.agent.name, "governor": c.governor.status(),
                "providers": c.router.usable(), "last_provider": c.router.last_provider,
                "tools": c.registry.names(), "autonomy": c.settings.boundaries.autonomy,
                "pending_approvals": len(c.approvals.pending()), "warnings": c.warnings}

    @app.post("/api/tasks", dependencies=[Depends(auth)])
    async def create_task(body: TaskIn) -> dict[str, str]:
        chats = c.chats
        chat_id = body.chat_id
        history = ""
        if chats is not None:
            if not chat_id or chats.get(chat_id) is None:
                chat_id = chats.create().id
            history = chats.context_for(chat_id)  # before this message is added
            chats.add(chat_id, "user", body.text)
        req = TaskRequest(body.text, channel="api", chat_id=chat_id, history=history)
        results[req.id] = {"task_id": req.id, "status": "running", "chat_id": chat_id}

        async def work() -> None:
            try:
                res = await c.submit(req)
            except asyncio.CancelledError:
                # Stopped from the dashboard: nothing more runs for this task (clean-up already ran).
                res = TaskResult(req.id, "cancelled", "Stopped. I won't do anything more for this request.")
            results[req.id] = _result_json(res) | {"chat_id": chat_id}
            if chats is not None and chats.get(chat_id) is not None:  # the chat may have been deleted meanwhile
                chats.add(chat_id, "delta", res.answer, {k: v for k, v in results[req.id].items()
                                                        if k not in ("answer", "chat_id")})
            await hub.broadcast({"type": "result", "result": results[req.id]})

        t = asyncio.create_task(work(), name=f"task-{req.id}")
        running.add(t)
        by_id[req.id] = t
        t.add_done_callback(running.discard)
        t.add_done_callback(lambda _: by_id.pop(req.id, None))
        return {"task_id": req.id, "chat_id": chat_id}

    # --- chats (sidebar) -----------------------------------------------------------------------
    def _chats() -> Any:
        if c.chats is None:
            raise HTTPException(503, "chats are not available")
        return c.chats

    def _running_chats() -> set[str]:
        return {r.get("chat_id", "") for r in results.values() if r.get("status") == "running"}

    @app.get("/api/chats", dependencies=[Depends(auth)])
    async def list_chats(q: str = "") -> list[dict[str, Any]]:
        busy = _running_chats()
        return [ch.to_dict() | {"running": ch.id in busy} for ch in _chats().list(q)]

    @app.post("/api/chats", dependencies=[Depends(auth)])
    async def new_chat() -> dict[str, Any]:
        return dict(_chats().create().to_dict())

    @app.get("/api/chats/{chat_id}", dependencies=[Depends(auth)])
    async def get_chat(chat_id: str) -> dict[str, Any]:
        ch = _chats().get(chat_id)
        if ch is None:
            raise HTTPException(404, "unknown chat")
        running = [r["task_id"] for r in results.values()
                   if r.get("status") == "running" and r.get("chat_id") == chat_id]
        return {"chat": ch.to_dict(), "messages": _chats().messages(chat_id), "running": running}

    @app.patch("/api/chats/{chat_id}", dependencies=[Depends(auth)])
    async def patch_chat(chat_id: str, body: ChatPatch) -> dict[str, Any]:
        ch = _chats().update(chat_id, body.title, body.pinned)
        if ch is None:
            raise HTTPException(404, "unknown chat")
        return dict(ch.to_dict())

    @app.delete("/api/chats/{chat_id}", dependencies=[Depends(auth)])
    async def delete_chat(chat_id: str) -> dict[str, bool]:
        for r in list(results.values()):  # deleting a chat stops its work too
            if r.get("chat_id") == chat_id and r.get("status") == "running" and (t := by_id.get(r["task_id"])):
                t.cancel()
        return {"ok": _chats().delete(chat_id)}

    @app.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(auth)])
    async def cancel_task(task_id: str) -> dict[str, bool]:
        t = by_id.get(task_id)
        if t is None or t.done():
            return {"ok": False}
        c.audit.record("task.cancel_requested", task=task_id)
        t.cancel()
        return {"ok": True}

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

    from delta.api.apps_routes import register as register_apps
    from delta.api.settings_routes import register as register_settings
    from delta.api.setup_routes import register as register_setup

    register_setup(app, c, auth)
    register_settings(app, c, auth)
    register_apps(app, c, auth)

    @app.get("/api/runs/unfinished", dependencies=[Depends(auth)])
    async def unfinished_run() -> dict[str, Any]:
        proof = c.orchestrator.proof
        return {"run": proof.unfinished() if proof else None}

    @app.post("/api/runs/{task_id}/dismiss", dependencies=[Depends(auth)])
    async def dismiss_run(task_id: str) -> dict[str, bool]:
        proof = c.orchestrator.proof
        return {"ok": bool(proof and proof.close_unfinished(task_id))}

    @app.get("/api/runs/{task_id}", dependencies=[Depends(auth)])
    async def run_proof(task_id: str) -> dict[str, Any]:
        proof = c.orchestrator.proof
        if proof is None or not task_id.isalnum():
            raise HTTPException(404, "no proof")
        return {"images": proof.files(task_id)}

    @app.get("/api/runs/{task_id}/{name}", dependencies=[Depends(auth)])
    async def run_image(task_id: str, name: str) -> FileResponse:
        proof = c.orchestrator.proof
        if proof is None or not task_id.isalnum() or name not in proof.files(task_id):
            raise HTTPException(404, "no such image")  # only listed files: no path tricks
        return FileResponse(proof.root / task_id / name, media_type="image/jpeg")

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
