"""The connected-apps fixture for eval tasks with `connector:` (delta.evals.spec).

Starts the fake notes app (delta.evals.fakemcp: Streamable HTTP or SSE with a fake OAuth server, or the stdio
program), connects it through Delta's real AppManager (the full OAuth flow, with a stand-in browser that consents),
registers the app tools, and records what the checks need (approvals asked, connect results). Secrets are sealed
with Windows DPAPI where it works (CI's Windows runners) and with a test sealer elsewhere.

  connector:
    server: http | sse | stdio      default http
    auth: oauth | token | none      default oauth (http only)
    connect: true                   connect before the task (default true)
    autonomy: ask-risky             the task's permission level
    channel: telegram               the request comes from a phone (a PIN is set; locked unless unlocked: true)
    unlocked: false
    inject: true                    read_note results carry a prompt injection
    expire: true | revoke: true     the app ends the access token / revokes the grant before the task
    fail_next: 1                    the next calls fail with HTTP 503
    read: [tool, ...]               the user marked these tools read on the Apps page
    enable: [tool, ...]             the user switched these tools on
    custom: true                    stdio only: added as a custom server (confirmation first, then confirmed)
    node_missing: true              the app needs Node.js and npx is not on the PC
    disconnect_after: true          disconnect after the task, before the checks
"""

from __future__ import annotations

import base64
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from delta.connectors.catalog import AppSpec, AuthSpec, Catalog
from delta.connectors.manager import AppManager
from delta.connectors.manager import Settings as ManagerSettings
from delta.connectors.secrets import SecretBox
from delta.evals.fakemcp import INJECTION, FakeApp, auto_consent

PIN = "4821"
USER = "5"


@dataclass
class ConnectorFixture:
    cfg: dict[str, Any]
    manager: AppManager
    app: FakeApp | None
    tools: Any = None
    guard: Any = None
    channel: str = "api"
    asked: list[Any] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    log: Path | None = None

    def count(self, tool: str) -> int:
        if self.app is not None:
            return self.app.logic.count(tool)
        if self.log is None or not self.log.exists():
            return 0
        return sum(1 for line in self.log.read_text(encoding="utf-8").splitlines() if f'"{tool}"' in line)

    async def close(self) -> None:
        await self.manager.close()
        if self.app is not None:
            self.app.stop()


def _secrets(folder: Path) -> SecretBox:
    from delta.system import vault

    if vault.available():
        return SecretBox(folder / "connectors" / "secrets")  # the real DPAPI path
    return SecretBox(folder / "connectors" / "secrets", seal=lambda t: "eval:" + base64.b64encode(t.encode()).decode(),
                     unseal=lambda s: base64.b64decode(s[5:]).decode())


async def start(cfg: dict[str, Any], data: Path, registry: Any, audit: Any) -> ConnectorFixture:
    from delta.connectors.tools import AppTools

    server = str(cfg.get("server", "http"))
    auth = str(cfg.get("auth", "oauth" if server in ("http", "sse") else "none"))
    settings = ManagerSettings(read_retries=2, backoff_s=0.05, call_timeout_s=float(cfg.get("call_timeout_s", 20)),
                               connect_timeout_s=20, sign_in_timeout_s=30, breaker_failures=4)
    app: FakeApp | None = None
    log: Path | None = None
    if server == "stdio":
        log = data / "stdio-calls.log"
        args = ["-m", "delta.evals.fakemcp", "stdio", "--log", str(log)]
        if cfg.get("inject"):
            args += ["--inject", INJECTION]
        if cfg.get("node_missing"):
            spec = AppSpec(id="fake", name="Fake Notes", status="reference", transport="stdio", runtime="node",
                           package="@delta-eval/fake-notes@1.0.0")
        else:
            spec = AppSpec(id="fake", name="Fake Notes", status="reference", transport="stdio", runtime="command",
                           command=sys.executable, args=args)
    else:
        app = FakeApp(auth=auth, inject=INJECTION if cfg.get("inject") else "", streamable=server != "sse").start()
        spec = AppSpec(id="fake", name="Fake Notes", status="official", url=app.url, sse_url=app.base + "/sse",
                       auth=AuthSpec(type=auth))  # type: ignore[arg-type]
    which = (lambda name: None) if cfg.get("node_missing") else None
    kw: dict[str, Any] = {"which": which} if which else {}
    catalog = Catalog({} if cfg.get("custom") else {"fake": spec})
    manager = AppManager(data, catalog, settings, secrets=_secrets(data), open_browser=auto_consent, audit=audit,
                         **kw)
    fx = ConnectorFixture(cfg, manager, app, log=log)
    if cfg.get("connect", True):
        if cfg.get("custom"):
            custom = {"name": "Fake Notes", "command": " ".join(f'"{a}"' for a in [sys.executable, *spec.args])}
            fx.results.append(await manager.connect("fake", custom=custom))
            fx.results.append(await manager.connect("fake", custom=custom, confirm=True))
        else:
            r = await manager.connect("fake", token=app.static_token if app is not None and auth == "token" else "")
            if r["status"] == "sign_in":
                r = await manager.wait_connect("fake", 30)
            fx.results.append(r)
    for tool in cfg.get("read") or []:
        manager.set_tool("fake", str(tool), risk="read")
    for tool in cfg.get("enable") or []:
        manager.set_tool("fake", str(tool), enabled=True)
    fx.tools = AppTools(manager, registry)
    if app is not None:
        if cfg.get("expire"):
            app.expire_access_tokens()
        if cfg.get("revoke"):
            app.revoke_everything()
        app.fail_next = int(cfg.get("fail_next", 0) or 0)
    if cfg.get("channel"):
        from delta.safety.remote_guard import RemoteGuard

        store: dict[str, str] = {}
        fx.channel = str(cfg["channel"])
        fx.guard = RemoteGuard(data / "guard", SimpleNamespace(record=lambda *a, **k: None), alert=lambda *a: None,
                               get_secret=store.get, set_secret=store.__setitem__)
        fx.guard.set_pin(PIN, by="eval")
        if cfg.get("unlocked"):
            fx.guard.try_pin(USER, PIN)
    return fx


async def finish(fx: ConnectorFixture) -> None:
    if fx.cfg.get("disconnect_after"):
        fx.results.append(await fx.manager.disconnect("fake", by="eval"))
