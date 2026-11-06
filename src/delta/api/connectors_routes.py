"""Connected apps API for the dashboard's Apps page (delta.connectors).

Same bearer token as every other route. The dashboard is this PC (a local channel), so no PIN is asked here;
from a phone, connecting goes through Telegram /connect, which needs the PIN. Tokens, keys and client secrets
the user pastes are sealed with DPAPI at once and never sent back by any route.

  GET    /api/connectors                      the catalog with each app's state
  GET    /api/connectors/{id}/tools           the app's tools with risk and on/off
  POST   /api/connectors/{id}/connect         {token?, client_id?, client_secret?, inputs?, secrets?}
  POST   /api/connectors/custom               {id, name, url | command, auth, confirm}: confirm first, then add
  POST   /api/connectors/{id}/disconnect      revokes the sign-in at the app (when it can) and deletes it here
  POST   /api/connectors/{id}/tools/{tool}    {enabled?, risk?}
  POST   /api/connectors/{id}/trust           {trust}: believe the app's own read-only labels
  POST   /api/connectors/{id}/health          ping the app now
  GET    /api/mcp-servers                     Apps > Advanced: servers under addons.config.mcp.servers, and if on
  POST   /api/mcp-servers                     {enabled}: switch them on or off (applies after a restart)
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

STATIC = Path(__file__).parent / "static"


class ConnectIn(BaseModel):
    token: str = Field(default="", max_length=4000)
    client_id: str = Field(default="", max_length=400)
    client_secret: str = Field(default="", max_length=400)
    inputs: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)


class CustomIn(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    name: str = Field(default="", max_length=80)
    url: str = Field(default="", max_length=500)
    command: str = Field(default="", max_length=1000)
    auth: str = Field(default="none", pattern="^(none|oauth)$")
    confirm: bool = False


class ToolIn(BaseModel):
    enabled: bool | None = None
    risk: str | None = Field(default=None, pattern="^(|read|write|external|destructive)$")


class TrustIn(BaseModel):
    trust: bool


class McpServersIn(BaseModel):
    enabled: bool


def register(app: FastAPI, c: Any, auth: Callable[..., None]) -> None:
    def tools_layer() -> Any:
        if getattr(c, "connectors", None) is None:
            raise HTTPException(503, "connected apps are switched off (connectors.enabled in settings)")
        return c.connectors

    def manager() -> Any:
        m = tools_layer().manager
        m.refresh_if_changed()
        return m

    @app.get("/apps.js")
    async def apps_js() -> FileResponse:
        return FileResponse(STATIC / "apps.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/api/mcp-servers", dependencies=[Depends(auth)])
    async def mcp_servers() -> dict[str, Any]:
        from delta.addons import mcp_settings

        a = c.settings.addons
        infos = getattr(c, "addon_infos", None) or []
        return {"servers": mcp_settings.describe(a), "enabled": mcp_settings.legacy_on(a),
                "running": any(i.name == mcp_settings.MCP and i.status == "running" for i in infos)}

    @app.post("/api/mcp-servers", dependencies=[Depends(auth)])
    async def set_mcp_servers(body: McpServersIn) -> dict[str, Any]:
        from delta.addons import mcp_settings
        from delta.config import write_user_settings

        s = c.settings
        write_user_settings(s.data_dir, {"addons": {"config": {"mcp": {"enabled": body.enabled}}}})
        s.addons.config.setdefault("mcp", {})["enabled"] = body.enabled
        c.audit.record("settings.changed", sections=["addons.mcp"], enabled=body.enabled)
        return {"ok": True, "enabled": mcp_settings.legacy_on(s.addons), "restart_needed": True}

    @app.get("/api/connectors", dependencies=[Depends(auth)])
    async def list_apps() -> dict[str, Any]:
        m = manager()
        return {"apps": m.status(), "checked": m.catalog.checked, "problems": m.catalog.problems,
                "secrets_ok": m.secrets.available()}

    @app.get("/api/connectors/{app_id}/tools", dependencies=[Depends(auth)])
    async def app_tools(app_id: str) -> dict[str, Any]:
        from delta.connectors.tools import resolve_risk, tool_enabled

        m = manager()
        spec, conn = m.spec(app_id), m.store.get(app_id)
        if spec is None or conn is None:
            raise HTTPException(404, "that app is not connected")
        rows = []
        for t in conn.tools:
            risk = resolve_risk(spec, conn, t)
            ann = t.get("annotations") or {}
            rows.append({"name": t["name"], "title": t.get("title", ""), "description": t["description"][:400],
                         "risk": str(risk), "enabled": tool_enabled(conn, t, risk),
                         "user_risk": (conn.choices.get(t["name"]) or {}).get("risk", ""),
                         "read_only_hint": ann.get("readOnlyHint") is True,
                         "destructive_hint": ann.get("destructiveHint") is True})
        return {"app": app_id, "name": spec.name, "tools": rows, "truncated": conn.truncated,
                "trust_read_only_hints": conn.trust_read_only_hints}

    @app.post("/api/connectors/custom", dependencies=[Depends(auth)])
    async def add_custom(body: CustomIn) -> dict[str, Any]:
        m = manager()
        r: dict[str, Any] = await m.connect(body.id, custom=body.model_dump(), confirm=body.confirm, by="dashboard")
        return r

    @app.post("/api/connectors/{app_id}/connect", dependencies=[Depends(auth)])
    async def connect(app_id: str, body: ConnectIn) -> dict[str, Any]:
        m = manager()
        if m.spec(app_id) is None:
            raise HTTPException(404, "no such app")
        r: dict[str, Any] = await m.connect(app_id, token=body.token, client_id=body.client_id,
                                            client_secret=body.client_secret, inputs=body.inputs,
                                            secret_values=body.secrets, confirm=True, by="dashboard")
        return r

    @app.post("/api/connectors/{app_id}/disconnect", dependencies=[Depends(auth)])
    async def disconnect(app_id: str) -> dict[str, Any]:
        r: dict[str, Any] = await manager().disconnect(app_id, by="dashboard")
        return r

    @app.post("/api/connectors/{app_id}/tools/{tool}", dependencies=[Depends(auth)])
    async def set_tool(app_id: str, tool: str, body: ToolIn) -> dict[str, bool]:
        try:
            ok = manager().set_tool(app_id, tool, body.enabled, body.risk)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if not ok:
            raise HTTPException(404, "no such tool")
        return {"ok": True}

    @app.post("/api/connectors/{app_id}/trust", dependencies=[Depends(auth)])
    async def trust(app_id: str, body: TrustIn) -> dict[str, bool]:
        if not manager().set_trust_hints(app_id, body.trust):
            raise HTTPException(404, "that app is not connected")
        return {"ok": True}

    @app.post("/api/connectors/{app_id}/health", dependencies=[Depends(auth)])
    async def health(app_id: str) -> dict[str, Any]:
        r: dict[str, Any] = await manager().health(app_id)
        return r
