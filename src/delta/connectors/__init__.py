"""Connected apps: Notion, GitHub, Slack, Gmail and other apps that publish a Model Context Protocol (MCP) server.

The user connects an app once (dashboard > Apps, `delta apps connect <id>`, or Telegram /connect), then simply
asks Delta to do work in it from the dashboard chat or Telegram.

Pieces:
  catalog.py   the curated, versioned list of servers (config/mcp_catalog.yaml) and custom servers
  client.py    a small MCP client: JSON-RPC over stdio, Streamable HTTP and the older HTTP+SSE transport
  oauth.py     the MCP authorization spec: OAuth 2.1 + PKCE, metadata discovery, dynamic client registration,
               a loopback redirect on 127.0.0.1, refresh and revoke
  secrets.py   tokens and keys, sealed with Windows DPAPI (delta.system.vault); refused when DPAPI is missing
  store.py     which apps are connected, their tool lists and per-tool choices (no secrets)
  manager.py   connect / disconnect / status, sessions, timeouts, retries, circuit breaker
  tools.py     the agent's view: find_app_tool + use_app_tool (lazy loading) and one gated tool per app tool

Nothing here needs the optional `mcp` SDK: the client speaks the protocol itself over httpx (a core
dependency), so connected apps work on a plain install. The `mcp:` add-on (addons.config.mcp.servers in
delta.yaml) keeps working exactly as before, through the SDK.

Safety: every app tool call goes through the same ToolExecutor gate as Delta's own tools (policy, approvals,
Autopilot rules, Remote Guard, ExfilGuard, taint, audit). See SECURITY.md, "Connected apps".
"""
