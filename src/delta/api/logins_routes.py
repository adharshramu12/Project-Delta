"""Saved logins and watchers for the dashboard (Settings > Logins and the Watchers card). Same bearer token as every
other route; the dashboard is this PC (a local channel). A password arrives once, in the body of POST /api/logins,
is sealed with DPAPI and is never sent back: no route returns it, and the server keeps no access log.

  GET    /api/logins                       saved logins (no passwords) and whether DPAPI works here
  POST   /api/logins                       {name, site, username, password, login_url?, permission?, allow_http?}
  PUT    /api/logins/{id}/permission       {permission: ask | always}
  POST   /api/logins/{id}/test             sign in once in Delta's private browser (this click is the permission)
  DELETE /api/logins/{id}
  GET    /api/watchers                     watchers, and items offered as reminders
  POST   /api/watchers                     {login, what: [..], every_hours?, notify?, digest_time?, name?}
  POST   /api/watchers/{id}/run            check now
  DELETE /api/watchers/{id}
  POST   /api/watchers/offers/{id}         {when: "YYYY-MM-DDTHH:MM"} -> a reminder; {dismiss: true} -> gone
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from delta.logins.store import LoginError


class LoginIn(BaseModel):
    name: str = Field(max_length=60)
    site: str = Field(max_length=300)
    username: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=500)
    login_url: str = Field(default="", max_length=500)
    permission: str = "ask"
    allow_http: bool = False
    replace: bool = False


class PermissionIn(BaseModel):
    permission: str


class WatchIn(BaseModel):
    login: str
    what: list[str] = Field(min_length=1, max_length=8)
    every_hours: float = Field(default=6, ge=1, le=168)
    notify: str = "instant"
    digest_time: str = Field(default="18:00", pattern=r"^\d{2}:\d{2}$")
    name: str = ""


class OfferIn(BaseModel):
    when: str = ""
    dismiss: bool = False


def register(app: FastAPI, c: Any, auth: Callable[..., None]) -> None:
    def logins() -> Any:
        if getattr(c, "logins", None) is None:
            raise HTTPException(503, "saved logins are not available")
        return c.logins

    def watchers() -> Any:
        if getattr(c, "watchers", None) is None:
            raise HTTPException(503, "watchers are not available")
        return c.watchers

    @app.get("/api/logins", dependencies=[Depends(auth)])
    async def list_logins() -> dict[str, Any]:
        svc = logins()
        return {"logins": [x.public() for x in svc.store.list()], "vault": svc.store.available(),
                "browser": getattr(getattr(c, "browsers", None), "mgr", None) is not None}

    @app.post("/api/logins", dependencies=[Depends(auth)])
    async def add_login(body: LoginIn) -> dict[str, Any]:
        svc = logins()
        try:
            login = svc.store.add(body.name, body.site, body.username, body.password, login_url=body.login_url,
                                  permission=body.permission, allow_http=body.allow_http, replace=body.replace)
        except LoginError as e:
            raise HTTPException(400, str(e)) from None
        c.audit.record("login.saved", login=login.id, site=login.origin, by="dashboard")
        return {"ok": True, "login": login.public(),
                "message": f"Saved {login.name} for {login.origin}, encrypted with Windows DPAPI."}

    @app.put("/api/logins/{lid}/permission", dependencies=[Depends(auth)])
    async def set_permission(lid: str, body: PermissionIn) -> dict[str, Any]:
        try:
            login = logins().store.set_permission(lid, body.permission)
        except LoginError as e:
            raise HTTPException(400, str(e)) from None
        c.audit.record("login.permission", login=login.id, permission=login.permission, by="dashboard")
        return {"ok": True, "login": login.public()}

    @app.delete("/api/logins/{lid}", dependencies=[Depends(auth)])
    async def remove_login(lid: str) -> dict[str, Any]:
        gone = logins().store.remove(lid)
        if gone is None:
            raise HTTPException(404, "no such login")
        c.audit.record("login.removed", login=gone.id, by="dashboard")
        return {"ok": True}

    @app.post("/api/logins/{lid}/test", dependencies=[Depends(auth)])
    async def test_login(lid: str) -> dict[str, Any]:
        svc = logins()
        mgr = getattr(getattr(c, "browsers", None), "mgr", None)
        if mgr is None:
            raise HTTPException(503, "Delta's private browser is not available on this PC")
        if svc.store.get(lid) is None:
            raise HTTPException(404, "no such login")
        async with c._task_lock:  # not while a task uses the browser
            page = await mgr.page()
            result = await svc.sign_in(lid, page, ask=False)
        return {"ok": result.ok, "outcome": result.outcome, "message": result.message}

    @app.get("/api/watchers", dependencies=[Depends(auth)])
    async def list_watchers() -> dict[str, Any]:
        svc = watchers()
        return {"watchers": [w.public() for w in svc.store.list()],
                "offers": [{"id": o["id"], "watcher_id": o["watcher_id"], "created": o["created"],
                            "section": o["item"].get("section", ""), "line": o["item"].get("line", "")}
                           for o in svc.offers()]}

    @app.post("/api/watchers", dependencies=[Depends(auth)])
    async def add_watcher(body: WatchIn) -> dict[str, Any]:
        try:
            w = watchers().create(body.name, body.login, body.what, interval_minutes=int(body.every_hours * 60),
                                  notify=body.notify, digest_time=body.digest_time, owner_channel="api")
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return {"ok": True, "watcher": w.public()}

    @app.post("/api/watchers/{wid}/run", dependencies=[Depends(auth)])
    async def run_watcher(wid: str) -> dict[str, Any]:
        svc = watchers()
        w = svc.store.get(wid)
        if w is None:
            raise HTTPException(404, "no such watcher")
        report = await svc.check(w, trigger="manual", channel="api")
        return {"ok": report.status in ("done", "skipped"), "status": report.status,
                "message": report.text or report.summary}

    @app.delete("/api/watchers/{wid}", dependencies=[Depends(auth)])
    async def remove_watcher(wid: str) -> dict[str, Any]:
        if watchers().remove(wid) is None:
            raise HTTPException(404, "no such watcher")
        return {"ok": True}

    @app.post("/api/watchers/offers/{pid}", dependencies=[Depends(auth)])
    async def answer_offer(pid: int, body: OfferIn) -> dict[str, Any]:
        svc = watchers()
        if body.dismiss:
            return {"ok": svc.store.set_pending_state(pid, "dismissed")}
        try:
            sid = svc.accept_offer(pid, body.when, tz=str(c.settings.scheduler.timezone or ""))
        except Exception as e:
            raise HTTPException(400, str(e)) from None
        return {"ok": True, "schedule": sid, "message": "Reminder added."}
