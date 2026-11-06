"""`delta apps list|connect|disconnect|status|tools|add`: connected apps from a terminal on the PC.

Works whether or not Delta is running: it uses the same files in the data folder (connections.json and the
DPAPI-sealed sign-ins), and the running service picks the change up by itself the next time it needs the apps.
Tokens and keys are typed without being shown and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
from collections.abc import Callable
from typing import Any

from delta.config import Settings


def add_parser(sub: Any) -> None:
    p = sub.add_parser("apps", help="connected apps (Notion, GitHub, Slack, Gmail...): list, connect, disconnect, "
                                    "status, tools, add")
    p.add_argument("action", choices=["list", "connect", "disconnect", "status", "tools", "add"])
    p.add_argument("app", nargs="?", help="the app id or name, e.g. notion")
    p.add_argument("--url", default="", help="add: a remote MCP server address (https://...)")
    p.add_argument("--command", default="", help="add: a local MCP server command line")
    p.add_argument("--name", default="", help="add: a display name")
    p.add_argument("--oauth", action="store_true", help="add: the server signs in with OAuth")
    p.add_argument("--client-id", default="", help="connect: your OAuth app's client id (Google, Slack, Asana)")
    p.add_argument("--input", action="append", default=[], metavar="KEY=VALUE",
                   help="connect: a setting the app needs, e.g. folder=C:/Users/me/Notes")
    p.add_argument("--enable", default="", metavar="TOOL", help="tools: switch a tool on")
    p.add_argument("--disable", default="", metavar="TOOL", help="tools: switch a tool off")
    p.add_argument("-y", "--yes", action="store_true", help="add: do not ask to confirm")


def run_cli(settings: Settings, args: argparse.Namespace, ask: Callable[[str], str] = input,
            secret: Callable[[str], str] = getpass.getpass, out: Callable[[str], None] = print,
            manager: Any = None) -> int:
    from delta.connectors.setup import build_manager

    m = manager or build_manager(settings)

    async def go() -> int:
        try:
            return await _run(m, args, ask, secret, out)
        finally:
            await m.close()

    return asyncio.run(go())


def _inputs(raw: list[str]) -> dict[str, str]:
    out = {}
    for item in raw:
        key, sep, value = item.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


async def _run(m: Any, args: argparse.Namespace, ask: Callable[[str], str], secret: Callable[[str], str],
               out: Callable[[str], None]) -> int:
    from delta.connectors.tools import resolve_risk, tool_enabled

    if args.action == "list":
        for row in m.status():
            tag = {"official": "", "official-preview": " (preview)", "reference": " (reference)",
                   "community": " (COMMUNITY)", "custom": " (custom)"}.get(row["status"], "")
            out(f"{row['id']:<16} {row['name']}{tag}: {row['state']}"
                + (f", {row['tools']} tools" if row["connected"] else ""))
        out("\nConnect one: delta apps connect <id>")
        return 0
    if not args.app:
        out(f"Say which app: delta apps {args.action} <id>")
        return 2
    if args.action == "add":
        if not (args.url or args.command):
            out("Give the server: delta apps add <id> --url https://... or --command \"npx -y package@1.2.3\"")
            return 2
        custom = {"name": args.name or args.app, "url": args.url, "command": args.command,
                  "auth": "oauth" if args.oauth else "none"}
        first = await m.connect(args.app, custom=custom, inputs=_inputs(args.input), by="cli", open_browser=False)
        if first["status"] != "confirm":
            out(("✓ " if first["status"] == "connected" else "✗ ") + first["message"])
            return 0 if first["status"] == "connected" else 1
        out(first["message"])
        if not args.yes and ask("Add and start it? [y/N] ").strip().lower() not in ("y", "yes"):
            out("Not added.")
            return 1
        return await _connect(m, args.app, out, custom=custom, inputs=_inputs(args.input), confirm=True)
    spec = m.find_app(args.app)
    if spec is None:
        out(f"✗ There is no app called '{args.app}'. See `delta apps list`.")
        return 1
    if args.action == "disconnect":
        r = await m.disconnect(spec.id, by="cli")
        out(("✓ " if r["status"] == "disconnected" else "✗ ") + r["message"])
        return 0 if r["status"] == "disconnected" else 1
    if args.action == "status":
        conn = m.store.get(spec.id)
        if conn is None:
            out(f"{spec.name} is not connected.")
            return 1
        health = await m.health(spec.id)
        out(f"{spec.name}: {'answering' if health['ok'] else 'NOT answering'}. {health['message']}")
        return 0 if health["ok"] else 1
    if args.action == "tools":
        conn = m.store.get(spec.id)
        if conn is None:
            out(f"{spec.name} is not connected.")
            return 1
        for name, on in ((args.enable, True), (args.disable, False)):
            if name:
                if not m.set_tool(spec.id, name, enabled=on):
                    out(f"✗ {spec.name} has no tool called {name}")
                    return 1
                out(f"✓ {name} is now {'on' if on else 'off'}")
        conn = m.store.get(spec.id)
        for t in conn.tools:
            risk = resolve_risk(spec, conn, t)
            state = "on " if tool_enabled(conn, t, risk) else "off"
            out(f"  [{state}] {t['name']:<36} {risk:<11} {t['description'][:70]}")
        return 0
    # connect
    token = client_id = client_secret = ""
    secrets: dict[str, str] = {}
    inputs = _inputs(args.input)
    for item in spec.inputs:
        if item.name not in inputs:
            inputs[item.name] = ask(f"{item.label or item.name}: ").strip()
    for s in spec.secrets:
        secrets[s.env] = secret(f"{s.label or s.env} (hidden): ").strip()
    if spec.auth.type == "token":
        if spec.auth.help:
            out(f"Create one here: {spec.auth.help}")
        token = secret(f"{spec.auth.label or 'Token'} (hidden): ").strip()
    if spec.auth.type == "oauth" and spec.auth.client == "user":
        if spec.auth.label:
            out(spec.auth.label)
        port = spec.auth.redirect_port or 8765
        out(f"Redirect URL to add to your OAuth app: http://127.0.0.1:{port}/callback")
        client_id = args.client_id or ask("Client id: ").strip()
        if spec.auth.client_secret:
            client_secret = secret("Client secret (hidden): ").strip()
    if spec.status == "community":
        out("This is a community server, not published by the app's vendor.")
    return await _connect(m, spec.id, out, token=token, client_id=client_id, client_secret=client_secret,
                          inputs=inputs, secret_values=secrets)


async def _connect(m: Any, app_id: str, out: Callable[[str], None], **kw: Any) -> int:
    r = await m.connect(app_id, by="cli", **kw)
    if r["status"] == "sign_in":
        out(r["message"])
        out(f"If no browser window opened, open this address on this PC:\n{r['url']}")
        r = await m.wait_connect(app_id)
    out(("✓ " if r["status"] == "connected" else "✗ ") + r["message"])
    return 0 if r["status"] == "connected" else 1
