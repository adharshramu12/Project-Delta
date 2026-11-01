"""MCP bridge add-on: connects Delta to any Model Context Protocol server, so one config entry
gives Delta tools for GitHub, Notion, Google Drive, databases, other agents, etc.

Configure in delta.yaml:

    addons:
      enabled: [mcp]
      config:
        mcp:
          servers:
            filesystem:
              command: npx
              args: ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users/me/Delta"]
            github:
              url: https://example.com/mcp
              token_secret: github_mcp_token     # `delta secrets set github_mcp_token`
              risk: write                        # default risk for this server's tools
              tools: { search_repositories: read }  # per-tool overrides

Safety: MCP tools go through Delta's policy like any other tool. Risk comes from (1) per-tool
override, (2) the server's own annotations (readOnlyHint / destructiveHint), (3) server default,
(4) WRITE — so unknown tools ask before running.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

from pydantic import BaseModel, ConfigDict

from delta.addons.api import AddonContext, Risk, Tool, ToolResult
from delta.system.logging import log
from delta.system.secrets import get_secret

CALL_TIMEOUT_S = 120
NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


class FreeArgs(BaseModel):
    """MCP servers publish their own JSON schema; we pass arguments through untouched."""

    model_config = ConfigDict(extra="allow")


def tool_name(server: str, tool: str) -> str:
    return NAME_RE.sub("_", f"{server}__{tool}")[:64]


def pick_risk(tool: Any, server_cfg: dict[str, Any]) -> Risk:
    override = (server_cfg.get("tools") or {}).get(tool.name)
    if override:
        return Risk(override)
    ann = getattr(tool, "annotations", None)
    if ann is not None:
        if getattr(ann, "destructive_hint", None):
            return Risk.DESTRUCTIVE
        if getattr(ann, "read_only_hint", None):
            return Risk.READ
    if server_cfg.get("risk"):
        return Risk(server_cfg["risk"])
    return Risk.WRITE


class McpTool(Tool[FreeArgs]):
    Args = FreeArgs
    name = ""          # set per instance
    description = ""
    risk = Risk.WRITE

    def __init__(self, server: str, session: Any, tool: Any, risk: Risk) -> None:
        self.server = server
        self.session = session
        self.remote_name = tool.name
        self.name = tool_name(server, tool.name)  # type: ignore[misc]
        self.description = f"[{server} via MCP] {tool.description or tool.name}"[:1000]  # type: ignore[misc]
        self.risk = risk  # type: ignore[misc]
        self.schema = tool.input_schema or {"type": "object", "properties": {}}

    def spec(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                  "parameters": self.schema}}

    def summary(self, args: FreeArgs) -> str:
        return f"{self.server}: {self.remote_name}({args.model_dump_json()[:200]})"

    async def run(self, args: FreeArgs) -> ToolResult:
        try:
            res = await self.session.call_tool(self.remote_name, args.model_dump(),
                                               read_timeout_seconds=CALL_TIMEOUT_S)
        except Exception as e:
            return ToolResult(False, f"MCP call failed: {type(e).__name__}: {e}")
        parts = []
        for c in getattr(res, "content", None) or []:
            text = getattr(c, "text", None)
            parts.append(text if text is not None else f"[{getattr(c, 'type', 'content')}]")
        structured = getattr(res, "structured_content", None)
        ok = not getattr(res, "is_error", False)
        return ToolResult(ok, "\n".join(parts) or str(structured or "(no output)"),
                          {"server": self.server, "tool": self.remote_name,
                           **({"structured": structured} if structured else {})})


class McpBridge:
    name = "mcp"
    version = "1.0.0"
    description = "Connect Model Context Protocol servers as Delta tools"

    async def setup(self, ctx: AddonContext) -> None:
        from contextlib import AsyncExitStack

        self.stack = AsyncExitStack()
        ctx.on_shutdown(self.stack.aclose)
        errlog = (ctx.data_dir / "servers.log").open("a", encoding="utf-8")
        self.stack.callback(errlog.close)
        servers: dict[str, dict[str, Any]] = ctx.config.get("servers") or {}
        for server, cfg in servers.items():
            try:
                session = await self._connect(server, cfg, errlog)
                listed = await session.list_tools()
            except Exception as e:  # one bad server must not block the others
                log.error("mcp.connect_failed", server=server, error=str(e))
                continue
            for t in listed.tools:
                ctx.register_tool(McpTool(server, session, t, pick_risk(t, cfg)))
            log.info("mcp.connected", server=server, tools=len(listed.tools))

    async def _connect(self, server: str, cfg: dict[str, Any], errlog: Any) -> Any:
        from mcp import ClientSession

        if "url" in cfg:
            from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

            headers = {}
            if cfg.get("token_secret"):
                token = get_secret(cfg["token_secret"])
                if not token:
                    raise RuntimeError(f"secret {cfg['token_secret']} not set")
                headers["Authorization"] = f"Bearer {token}"
            # the SDK's own client: MCP v2 uses httpx2, not httpx
            client = await self.stack.enter_async_context(create_mcp_http_client(headers=headers))
            streams = await self.stack.enter_async_context(streamable_http_client(cfg["url"], http_client=client))
        elif "command" in cfg:
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(command=cfg["command"], args=list(cfg.get("args", [])),
                                           env=cfg.get("env"), cwd=cfg.get("cwd"))
            streams = await self.stack.enter_async_context(stdio_client(params, errlog=errlog))
        else:
            raise ValueError(f"server {server}: needs 'command' (local) or 'url' (remote)")
        read, write = streams[0], streams[1]
        session = await self.stack.enter_async_context(ClientSession(read, write))
        with contextlib.suppress(AttributeError):
            await session.initialize()
        return session


addon = McpBridge()
