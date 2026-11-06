"""Schedules API for the dashboard's Schedules panel and the `delta schedule run` command.

Same bearer token as every other route. The dashboard is this PC (a local channel), so no PIN is asked
here; phones go through the agent tools, where Remote Guard asks for the PIN.

  GET    /api/schedules                list (with recent runs)
  GET    /api/schedules/{id}           one, with its run history
  POST   /api/schedules/preview        {"text"} -> the parsed schedule in plain words, nothing saved
  POST   /api/schedules                {"text", "name"?, "confirm": true} -> saved
  PATCH  /api/schedules/{id}           {"enabled"?, "name"?, "prompt"?, "text"? (new timing)}
  POST   /api/schedules/{id}/pause | /resume | /run
  DELETE /api/schedules/{id}
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from delta.scheduler.parse import ParseError, parse_schedule
from delta.scheduler.rules import describe, fmt_when, iso, next_run, now_utc, zone_label
from delta.scheduler.store import Schedule

STATIC = Path(__file__).parent / "static"


class PreviewIn(BaseModel):
    text: str


class CreateIn(BaseModel):
    text: str
    name: str = ""
    confirm: bool = False  # the dashboard shows the preview first; saving needs this yes


class PatchIn(BaseModel):
    enabled: bool | None = None
    name: str | None = None
    prompt: str | None = None
    text: str | None = None  # new timing in plain words, e.g. "every day at 8 am"


def register(app: FastAPI, c: Any, auth: Callable[..., None]) -> None:
    def scheduler() -> Any:
        if getattr(c, "scheduler", None) is None:
            raise HTTPException(503, "schedules are not available")
        return c.scheduler

    def tz() -> str:
        return str(c.settings.scheduler.timezone or "")

    def get_or_404(sid: str) -> Schedule:
        s = scheduler().store.get(sid, with_history=True)
        if s is None:
            raise HTTPException(404, "no such schedule")
        return s

    def as_json(s: Schedule) -> dict[str, Any]:
        return s.to_dict() | {"running": scheduler().is_running(s.id), "plain": s.plain()}

    def parse_or_400(text: str) -> Any:
        try:
            return parse_schedule(text, tz())
        except ParseError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/schedules.js")
    async def schedules_js() -> FileResponse:
        return FileResponse(STATIC / "schedules.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/schedules", dependencies=[Depends(auth)])
    async def list_schedules() -> list[dict[str, Any]]:
        sch = scheduler()
        out = []
        for s in sch.store.list():
            s.history = sch.store.history(s.id, 5)
            out.append(as_json(s))
        return out

    @app.get("/api/schedules/{sid}", dependencies=[Depends(auth)])
    async def get_schedule(sid: str) -> dict[str, Any]:
        return as_json(get_or_404(sid))

    @app.post("/api/schedules/preview", dependencies=[Depends(auth)])
    async def preview_schedule(body: PreviewIn) -> dict[str, Any]:
        p = parse_or_400(body.text)
        nxt = next_run(p.rule, tz(), now_utc())
        what = f"remind you to {p.prompt}" if p.kind == "reminder" else p.prompt
        when = describe(p.rule, tz())
        return {"name": p.name, "prompt": p.prompt, "kind": p.kind, "rule": p.rule.to_dict(), "when": when,
                "next_run": iso(nxt), "next_run_text": fmt_when(nxt, tz()), "tz_label": zone_label(tz()),
                "plain": f"{when[0].upper()}{when[1:]}: {what}. Next run: {fmt_when(nxt, tz())}."}

    @app.post("/api/schedules", dependencies=[Depends(auth)])
    async def create_schedule(body: CreateIn) -> dict[str, Any]:
        if not body.confirm:
            raise HTTPException(400, "Check the preview first, then save with confirm=true.")
        p = parse_or_400(body.text)
        autonomy = str(c.orchestrator.executor.policy.b.autonomy)
        s = scheduler().store.add(body.name or p.name, p.prompt, p.rule, kind=p.kind, tz=tz(), owner_channel="api",
                                  owner_user="dashboard", autonomy=autonomy)
        c.audit.record("schedule.created", schedule=s.id, by="dashboard", when=s.when, kind=s.kind)
        scheduler().poke()
        return as_json(s)

    @app.patch("/api/schedules/{sid}", dependencies=[Depends(auth)])
    async def patch_schedule(sid: str, body: PatchIn) -> dict[str, Any]:
        get_or_404(sid)
        store = scheduler().store
        # only the timing is taken from `text`; a placeholder task keeps the parser happy
        rule = parse_or_400(body.text + " (timing)").rule if body.text else None
        store.edit(sid, name=body.name, prompt=body.prompt, rule=rule)
        if body.enabled is not None:
            try:
                store.set_enabled(sid, body.enabled)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
        c.audit.record("schedule.changed", schedule=sid, by="dashboard",
                       fields=sorted(k for k, v in body.model_dump().items() if v is not None))
        scheduler().poke()
        return as_json(get_or_404(sid))

    @app.post("/api/schedules/{sid}/pause", dependencies=[Depends(auth)])
    async def pause_schedule(sid: str) -> dict[str, Any]:
        return await patch_schedule(sid, PatchIn(enabled=False))

    @app.post("/api/schedules/{sid}/resume", dependencies=[Depends(auth)])
    async def resume_schedule(sid: str) -> dict[str, Any]:
        return await patch_schedule(sid, PatchIn(enabled=True))

    @app.post("/api/schedules/{sid}/run", dependencies=[Depends(auth)])
    async def run_schedule(sid: str) -> dict[str, Any]:
        s = get_or_404(sid)
        ok, message = scheduler().run_now(s, "dashboard", "dashboard")
        c.audit.record("schedule.run_now", schedule=sid, by="dashboard", started=ok)
        return {"ok": ok, "message": message}

    @app.delete("/api/schedules/{sid}", dependencies=[Depends(auth)])
    async def delete_schedule(sid: str) -> dict[str, bool]:
        ok = scheduler().store.delete(sid)
        if ok:
            c.audit.record("schedule.deleted", schedule=sid, by="dashboard")
        return {"ok": ok}
