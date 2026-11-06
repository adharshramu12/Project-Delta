"""Local HTTP + WebSocket API. One API for the dashboard, the desktop app, remote clients and tests.

Binds to 127.0.0.1 only. Every request needs the bearer token stored in <data_dir>/api_token
(created on first run). The dashboard receives it in the URL fragment, which browsers never send
to the server or write to logs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from delta.attachments.kinds import DEFAULT_REQUEST
from delta.bootstrap import Container
from delta.core.state import TASK_CRASHED, TaskRequest, TaskResult
from delta.safety.approvals import ApprovalRequest
from delta.system.logging import log
from delta.system.vault import VaultUnavailableError

STATIC = Path(__file__).parent / "static"
# The dashboard's own files (styles and scripts), served by exact name from a list made at start-up: nothing in a
# request is ever used as a path. Types are fixed here because Windows can map .js to text/plain in the registry.
ASSET_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}
ASSETS = {p.name: p for p in STATIC.iterdir() if p.suffix in ASSET_TYPES} if STATIC.is_dir() else {}
# The dashboard runs only its own files: no inline scripts, nothing from other sites. Blob URLs are the
# screenshots and document previews it fetched with the token; ws: is its own live connection.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; "
       "frame-src blob:; object-src 'self' blob:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


SEND_TIMEOUT_S = 2.0  # one stalled dashboard (asleep laptop, full network buffer) must never hold up the rest


def load_token(data_dir: Path) -> str:
    """The API token, created once. Two starts racing (the daemon and `delta app`) agree on one token: the file
    is created exclusively with owner-only permissions (where the OS has them), written in full, then moved
    into place, so nobody ever reads an empty or half-written token."""
    path = data_dir / "api_token"
    for _ in range(50):
        with contextlib.suppress(FileNotFoundError):
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token
            with contextlib.suppress(OSError):  # an empty token file (an old crash) is never a valid token
                path.unlink()
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = data_dir / f"api_token.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(secrets.token_urlsafe(32))
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, path)  # never replaces a token another start already put there
            except FileExistsError:
                continue  # someone else won the race: use theirs
            except OSError:  # a file system without hard links
                if path.exists():
                    continue
                os.replace(tmp, path)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
    raise RuntimeError(f"could not create the API token in {data_dir}")


def same_token(given: str | None, token: str) -> bool:
    """Constant-time compare on bytes: a non-ASCII value is simply wrong (401), never a crash."""
    return secrets.compare_digest((given or "").encode("utf-8"), token.encode("utf-8"))


class TaskIn(BaseModel):
    text: str = ""
    chat_id: str = ""  # empty: a new chat is started
    attachments: list[str] = Field(default_factory=list, max_length=10)  # ids from PUT /api/attachments


class ChatPatch(BaseModel):
    title: str | None = None
    pinned: bool | None = None


class ApprovalIn(BaseModel):
    approved: bool
    for_task: bool = False  # "Allow for this task" (only honoured when the request offered it)
    edit: str | None = Field(default=None, max_length=2000)  # "Edit" (an email draft): what to change


class SkillStatusIn(BaseModel):
    status: str = Field(pattern="^(candidate|active|retired)$")


class LearnedIn(BaseModel):
    action: str = Field(pattern="^(keep|undo)$")


class MemoryIn(BaseModel):
    content: str
    kind: str = "profile"


class LearnActionIn(BaseModel):
    action: str = Field(pattern="^(promote|reject)$")


class Hub:
    """Fan-out of events and approval requests to connected WebSocket clients."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def broadcast(self, message: dict[str, Any]) -> None:
        # A snapshot: a client can connect or disconnect while we await a send (#8). Sends run side by side,
        # each with a time limit, so one stalled client cannot block the event bus (and with it the task).
        clients = list(self.clients)
        if not clients:
            return
        results = await asyncio.gather(*(self._send(ws, message) for ws in clients))
        for ws, ok in zip(clients, results, strict=True):
            if not ok:
                self.clients.discard(ws)

    @staticmethod
    async def _send(ws: WebSocket, message: dict[str, Any]) -> bool:
        try:
            await asyncio.wait_for(ws.send_json(message), SEND_TIMEOUT_S)
            return True
        except Exception:  # closed, broken or too slow (TimeoutError): drop it; the dashboard reconnects
            log.info("ws.client_dropped")
            return False


def _approval_json(r: ApprovalRequest) -> dict[str, Any]:
    return {"id": r.id, "tool": r.tool, "summary": r.summary, "risk": r.risk, "reason": r.reason,
            "task_id": r.task_id, "for_task": r.for_task, "can_edit": bool(getattr(r, "can_edit", False))}


def _result_json(r: TaskResult) -> dict[str, Any]:
    v = r.verification
    return {"task_id": r.task_id, "status": r.status, "answer": r.answer, "attempts": r.attempts,
            "provider": r.provider, "steps": [{"tool": s.tool, "ok": s.ok} for s in r.steps],
            "checks": v.checks if v else [], "seen_screen": bool(v and v.screenshot), "tokens": r.tokens,
            "usage": r.usage, "wall_s": r.wall_s, "fallback": r.fallback,
            "options": r.options, "questions": r.questions, "asked_about": r.asked_about, "image": r.image}


def create_app(c: Container, token: str) -> FastAPI:
    app = FastAPI(title="Delta", docs_url=None, redoc_url=None, openapi_url=None)
    hub = Hub()
    results: dict[str, dict[str, Any]] = {}
    by_id: dict[str, asyncio.Task[None]] = {}  # running tasks, so the dashboard can stop one
    running: set[asyncio.Task[None]] = set()
    bearer = HTTPBearer(auto_error=False)

    def auth(cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:  # noqa: B008
        if not cred or not same_token(cred.credentials, token):
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
        return FileResponse(STATIC / "index.html", headers={
            "Cache-Control": "no-store", "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer"})

    @app.get("/ui/{name}")
    async def ui_asset(name: str) -> FileResponse:
        path = ASSETS.get(name)
        if path is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such file")
        return FileResponse(path, media_type=ASSET_TYPES[path.suffix],
                            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

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
        files = _attached(body.attachments, chat_id)
        text = body.text.strip() or (DEFAULT_REQUEST if files else "")
        if not text:
            raise HTTPException(400, "Type a message or attach a file.")
        if chats is not None:
            try:
                if not chat_id or chats.get(chat_id) is None:
                    chat_id = chats.create().id
                history = chats.context_for(chat_id, query=text)  # before this message is added
                files = [c.orchestrator.attachments.store.move(a, chat_id) for a in files]
                chats.add(chat_id, "user", text, {"attachments": [a.public() for a in files]} if files else None)
            except VaultUnavailableError as e:  # never stored unencrypted: say why instead of a bare 500
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e
        req = TaskRequest(text, channel="api", chat_id=chat_id, history=history, attachments=[a.id for a in files])
        return launch(req, chat_id) | ({"text": text} if files and not body.text.strip() else {})

    def _attached(ids: list[str], chat_id: str) -> list[Any]:
        """The files named in a message: uploaded here (not yet sent, or in this same chat), still on disk."""
        if not ids:
            return []
        svc = getattr(c.orchestrator, "attachments", None)
        if svc is None:
            raise HTTPException(503, "Attachments are switched off in Delta's settings (attachments.enabled).")
        if len(ids) > svc.cfg.max_files_per_message:
            raise HTTPException(400, f"Attach at most {svc.cfg.max_files_per_message} files to one message.")
        from delta.attachments.store import UNSENT, folder_for

        found = []
        for i in dict.fromkeys(ids):
            att = svc.store.get(i)
            if att is None or att.folder not in (UNSENT, folder_for(chat_id)):
                raise HTTPException(400, "An attached file is no longer available. Attach it again.")
            found.append(att)
        return found

    def launch(req: TaskRequest, chat_id: str, autonomy_cap: str | None = None) -> dict[str, str]:
        """Run a request in the background like a typed message (a new one, or a resumed task)."""
        chats = c.chats
        results[req.id] = {"task_id": req.id, "status": "running", "chat_id": chat_id}

        async def work() -> None:
            """Whatever happens, the task ends with a result in `results`, the chat and every open
            dashboard: it is never left "running"."""
            cancelled = False
            try:
                res = await (c.submit(req) if autonomy_cap is None else c.submit(req, autonomy_cap=autonomy_cap))
            except asyncio.CancelledError:
                # Stopped from the dashboard: nothing more runs for this task (clean-up already ran).
                cancelled = True
                res = TaskResult(req.id, "cancelled", "Stopped. I won't do anything more for this request.")
            except Exception as e:
                log.exception("api.task_crashed", task=req.id)
                c.audit.record("task.error", task=req.id, error=type(e).__name__, where="api")
                res = TaskResult(req.id, "failed", TASK_CRASHED)
            results[req.id] = _result_json(res) | {"chat_id": chat_id}
            try:
                if chats is not None and chats.get(chat_id) is not None:  # the chat may have been deleted
                    chats.add(chat_id, "delta", res.answer, {k: v for k, v in results[req.id].items()
                                                            if k not in ("answer", "chat_id", "image")})
            except Exception:
                log.exception("api.chat_save_failed", task=req.id)
            try:
                await hub.broadcast({"type": "result", "result": results[req.id]})
            except Exception:
                log.exception("api.broadcast_failed", task=req.id)
            if cancelled:
                raise asyncio.CancelledError  # finish as cancelled, after the result is recorded

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
        ok = _chats().delete(chat_id)
        if ok and (svc := getattr(c.orchestrator, "attachments", None)) is not None:
            svc.store.delete_chat(chat_id)  # its attached files go with it
        return {"ok": ok}

    @app.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(auth)])
    async def cancel_task(task_id: str) -> dict[str, bool]:
        from delta.control import let_go, stop_task

        t = by_id.get(task_id)
        if t is None or t.done():  # asked from Telegram, a schedule or another channel: stop it by its id
            return {"ok": stop_task(c, task_id, by="dashboard")}
        c.audit.record("task.cancel_requested", task=task_id)
        current = c.running is t
        if current:
            let_go(c)  # let go of the mouse and keyboard, hide the in-control ring
        t.cancel()
        return {"ok": True}

    @app.post("/api/tasks/stop-all", dependencies=[Depends(auth)])
    async def stop_all_tasks() -> dict[str, int]:
        from delta.control import stop_all

        queued = list(getattr(c, "active", {}).values())
        extra = 0
        for t in list(by_id.values()):  # dashboard tasks still getting ready (attachments) stop too
            if not t.done() and t not in queued and t is not c.running:
                t.cancel()
                extra += 1
        return {"stopped": stop_all(c, by="dashboard") + extra}

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
        if body.edit is not None:
            settled, why = c.approvals.edit(approval_id, body.edit, by="dashboard")
            if not settled:
                raise HTTPException(404 if why == "already answered or expired" else 400, why)
        elif not c.approvals.resolve(approval_id, body.approved, by="dashboard", for_task=body.for_task):
            raise HTTPException(404, "no such pending approval")
        return {"ok": True}

    @app.get("/api/memory", dependencies=[Depends(auth)])
    async def list_memory(trust: str = "", kind: str = "") -> list[dict[str, Any]]:
        return [m.to_dict() for m in c.memory.list(kind or None, trust=trust or None)]

    @app.get("/api/memory/stats", dependencies=[Depends(auth)])
    async def memory_stats() -> dict[str, int]:
        return c.memory.stats()

    @app.post("/api/memory/forget", dependencies=[Depends(auth)])
    async def forget_memory(body: dict[str, Any]) -> dict[str, int]:
        """Forget by ids, by task, or by trust: {"ids": [..]}, {"task_id": ".."} or {"trust": "untrusted"}."""
        ids = [int(i) for i in body.get("ids") or [] if str(i).isdigit()]
        trust = str(body.get("trust") or "")
        if trust and trust not in ("untrusted", "agent", "trusted"):
            raise HTTPException(400, "trust must be untrusted, agent or trusted")
        n = c.memory.forget(ids, task_id=str(body.get("task_id") or ""), trust=trust)
        c.audit.record("memory.forget", count=n, by="dashboard")
        return {"forgotten": n}

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
        from delta.learning.store import UntrustedPromotionError

        store = c.orchestrator.learning
        try:
            item = store.set_status(item_id, "active" if body.action == "keep" else "undone",
                                    by="dashboard") if store else None
        except UntrustedPromotionError as e:
            raise HTTPException(409, "it came from outside content and can never become a rule") from e
        if not item:
            raise HTTPException(404, "no such learned item")
        c.audit.record("learned", item=item_id, user_action=body.action, kind=item.kind)
        return item.to_dict()

    @app.get("/api/learn", dependencies=[Depends(auth)])
    async def list_learn(status: str = "") -> list[dict[str, Any]]:
        gate = c.orchestrator.gate
        return gate.items(status or None) if gate else []

    @app.get("/api/learn/history", dependencies=[Depends(auth)])
    async def learn_history(item: str = "") -> list[dict[str, Any]]:
        gate = c.orchestrator.gate
        return gate.history(item or None) if gate else []

    @app.post("/api/learn/{ref}", dependencies=[Depends(auth)])
    async def learn_action(ref: str, body: LearnActionIn) -> dict[str, Any]:
        from delta.learning.store import UntrustedPromotionError

        gate = c.orchestrator.gate
        if gate is None:
            raise HTTPException(404, "learning is switched off")
        try:
            change = gate.promote(ref, by="dashboard") if body.action == "promote" else gate.reject(ref, by="dashboard")
        except UntrustedPromotionError as e:
            raise HTTPException(409, "it came from outside content and can never become a rule") from e
        except (KeyError, ValueError) as e:
            raise HTTPException(404, "no such learning item") from e
        c.audit.record("learn", item=ref, user_action=body.action, change=change.change)
        return {"ref": change.ref, "change": change.change, "summary": change.summary}

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
    from delta.api.schedule_routes import register as register_schedules
    from delta.api.settings_routes import register as register_settings
    from delta.api.setup_routes import register as register_setup

    register_setup(app, c, auth)
    register_settings(app, c, auth)
    register_apps(app, c, auth)
    from delta.api.attachments_routes import register as register_attachments
    from delta.api.documents_routes import register as register_documents

    register_attachments(app, c, auth)  # files and pictures in the chat (delta.attachments)

    register_documents(app, c, auth)

    register_schedules(app, c, auth)
    from delta.api.connectors_routes import register as register_connectors

    register_connectors(app, c, auth)  # connected apps (delta.connectors): the Apps page
    from delta.api.logins_routes import register as register_logins

    register_logins(app, c, auth)  # saved site logins and watchers (delta.logins, delta.watchers)
    from delta.api.tasks_routes import register as register_tasks

    register_tasks(app, c, auth, launch)  # task history, run timeline, resume / discard (delta.engine)

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
        if not same_token(socket.query_params.get("token", ""), token):
            await socket.close(code=4401)
            return
        await socket.accept()
        hub.clients.add(socket)
        try:  # whatever ends this socket (disconnect, error, shutdown), it leaves the hub
            for r in c.approvals.pending():
                await asyncio.wait_for(socket.send_json({"type": "approval", "approval": _approval_json(r)}),
                                       SEND_TIMEOUT_S)
            while True:
                await socket.receive_text()  # keepalive; clients act through the REST endpoints
        except (WebSocketDisconnect, TimeoutError):
            pass
        finally:
            hub.clients.discard(socket)

    return app
