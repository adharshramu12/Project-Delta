"""Connected apps: connect, disconnect, status, and every call to an app's server.

Nothing connects at start-up: the tool list from the last connection is kept (connections.json), so the agent
knows the user's apps at once and a server is only started or reached the first time a task uses it.

Reliability, per app:
  - a time limit on every call (connectors.call_timeout_s);
  - retries only for READ tools, and only on transient failures (time-outs, connection drops, 429 and 5xx, as the
    self-heal taxonomy classifies them), with a short back-off;
  - a session the server forgot is re-opened and the call sent once more (the server never saw it);
  - a circuit breaker: after a few failures in a row the app is left alone for a while instead of being hammered;
  - an expired sign-in is refreshed once; if that fails the user is told plainly to reconnect the app.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from delta.connectors.base import McpError, OAuthError, TokenSet
from delta.connectors.catalog import AppSpec, Catalog, RuntimeMissingError, command_for, custom_spec, is_loopback
from delta.connectors.secrets import SecretBox, SecretsUnavailableError
from delta.connectors.store import Connection, ConnectionStore, clean_tools
from delta.llm.router import CircuitBreaker
from delta.system.logging import log
from delta.tools.base import Risk

if TYPE_CHECKING:  # httpx and the transports load on the first connection, not at start-up
    import httpx

    from delta.connectors.client import McpClient
    from delta.connectors.oauth import OAuthFlow

TRANSIENT = {"timeout_s", "navigation_error", "rate_limit", "app_crashed", "app_not_responding"}


@dataclass
class Settings:
    """connectors: in delta.yaml (delta.config.ConnectorsConfig has the same fields)."""
    call_timeout_s: float = 60.0
    connect_timeout_s: float = 30.0
    read_retries: int = 2
    breaker_failures: int = 3
    breaker_cooldown_s: float = 60.0
    sign_in_timeout_s: float = 300.0
    backoff_s: float = 0.5


@dataclass
class CallOutcome:
    ok: bool
    text: str
    evidence: dict[str, Any] = field(default_factory=dict)


class AppAuthError(RuntimeError):
    pass


def reconnect_message(spec: AppSpec) -> str:
    return (f"Your {spec.name} sign-in has expired or was revoked. Reconnect {spec.name}: in the dashboard open Apps "
            f"and press Connect, or run `delta apps connect {spec.id}` on the PC.")


def _result_text(res: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    parts: list[str] = []
    for c in res.get("content") or []:
        if not isinstance(c, dict):
            continue
        kind = c.get("type")
        if kind == "text":
            parts.append(str(c.get("text", "")))
        elif kind == "resource":
            r = c.get("resource") or {}
            parts.append(str(r.get("text") or f"[resource {r.get('uri', '')}]"))
        elif kind == "resource_link":
            parts.append(f"[link {c.get('name', '')}: {c.get('uri', '')}]")
        else:
            parts.append(f"[{kind or 'content'}]")
    structured = res.get("structuredContent")
    text = "\n".join(parts)
    if not text and structured is not None:
        text = json.dumps(structured, ensure_ascii=False)[:20000]
    return text or "(no output)", ({"structured": structured} if structured is not None else {})


class AppManager:
    def __init__(self, data_dir: Path, catalog: Catalog, settings: Settings | None = None, *,
                 secrets: SecretBox | None = None, which: Callable[[str], str | None] = shutil.which,
                 open_browser: Callable[[str], Any] | None = None, audit: Any = None,
                 http: Callable[[str], httpx.AsyncClient] | None = None,
                 env: Callable[[dict[str, str]], dict[str, str]] | None = None) -> None:
        self.folder = data_dir / "connectors"
        self.catalog = catalog
        self.cfg = settings or Settings()
        self.store = ConnectionStore(self.folder)
        self.secrets = secrets or SecretBox(self.folder / "secrets")
        self.which = which
        self.open_browser = open_browser or (lambda url: webbrowser.open(url))
        self.audit = audit
        self._http_factory = http or _http_client
        self._env = env
        self.sessions: dict[str, McpClient] = {}
        self.breakers: dict[str, CircuitBreaker] = {}
        self.pending: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self.flows: dict[str, OAuthFlow] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._listeners: list[Callable[[], Any]] = []
        self._closing: set[asyncio.Task[None]] = set()

    # --- bookkeeping -------------------------------------------------------------------------------------------
    def on_change(self, fn: Callable[[], Any]) -> None:
        """Called when the set of connected apps or their tools changed (the agent's tools are rebuilt)."""
        self._listeners.append(fn)

    def _changed(self) -> None:
        for fn in self._listeners:
            try:
                fn()
            except Exception:
                log.exception("connectors.listener_failed")

    def refresh_if_changed(self) -> bool:
        """Another process (`delta apps connect`) changed connections.json: load it and rebuild the tools."""
        if not self.store.changed_on_disk():
            return False
        self.store.reload()
        for app_id in [a for a in self.sessions if self.store.get(a) is None]:  # disconnected elsewhere
            client = self.sessions.pop(app_id)
            if _loop_running():
                task = asyncio.get_running_loop().create_task(client.close())
                self._closing.add(task)
                task.add_done_callback(self._closing.discard)
        self._changed()
        return True

    def _record(self, event: str, **fields: Any) -> None:
        if self.audit is not None:
            self.audit.record(event, **fields)

    def _lock(self, app_id: str) -> asyncio.Lock:
        return self._locks.setdefault(app_id, asyncio.Lock())

    def breaker(self, app_id: str) -> CircuitBreaker:
        if app_id not in self.breakers:
            self.breakers[app_id] = CircuitBreaker(threshold=self.cfg.breaker_failures,
                                                   cooldown_s=self.cfg.breaker_cooldown_s)
        return self.breakers[app_id]

    def spec(self, app_id: str) -> AppSpec | None:
        conn = self.store.get(app_id)
        if conn is not None and conn.custom:
            with contextlib.suppress(Exception):
                return AppSpec.model_validate(conn.custom)
        return self.catalog.get(app_id)

    def connected(self) -> list[tuple[AppSpec, Connection]]:
        out = []
        for conn in self.store.items.values():
            if (spec := self.spec(conn.id)) is not None:
                out.append((spec, conn))
        return out

    # --- status ------------------------------------------------------------------------------------------------
    def status(self) -> list[dict[str, Any]]:
        """Every catalog app plus custom ones, with connection state (no secrets)."""
        rows = []
        seen = set()
        for spec in [*self.catalog.list(), *[s for s, c in self.connected() if c.custom]]:
            if spec.id in seen:
                continue
            seen.add(spec.id)
            conn = self.store.get(spec.id)
            b = self.breakers.get(spec.id)
            state = "not connected"
            if spec.id in self.pending:
                state = "signing in"
            elif conn is not None:
                state = "needs reconnect" if conn.needs_reconnect else (
                    "paused" if b is not None and not b.available() else "connected")
            rows.append({**spec.summary(), "state": state, "connected": conn is not None,
                         "tools": len(conn.tools) if conn else 0, "last_error": conn.last_error if conn else "",
                         "last_ok": conn.last_ok if conn else 0, "connected_at": conn.connected_at if conn else 0,
                         "custom": bool(conn and conn.custom), "live": spec.id in self.sessions,
                         "trust_read_only_hints": bool(conn and conn.trust_read_only_hints)})
        return rows

    def find_app(self, name: str) -> AppSpec | None:
        """'notion', 'Notion', 'google calendar' -> the catalog or custom app."""
        key = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
        if spec := self.spec(key):
            return spec
        for spec in [*self.catalog.list(), *[s for s, _ in self.connected()]]:
            if re.sub(r"[^a-z0-9]+", "_", spec.name.lower()).strip("_") == key:
                return spec
        return None

    # --- connecting --------------------------------------------------------------------------------------------
    def describe_custom(self, spec: AppSpec, inputs: dict[str, str] | None = None) -> dict[str, Any]:
        """Exactly what a custom server will run or reach, for the confirmation."""
        if spec.transport == "stdio":
            try:
                argv = command_for(spec, inputs, self.which)
            except RuntimeMissingError as e:
                return {"ok": False, "message": str(e)}
            return {"ok": True, "kind": "program", "command": argv,
                    "text": "Delta will run this program on your PC, with your personal environment variables "
                            "removed:\n  " + " ".join(argv)}
        return {"ok": True, "kind": "url", "url": spec.url,
                "text": f"Delta will connect to {spec.url} and send it what your tasks ask that app to do."}

    async def connect(self, app_id: str, *, token: str = "", client_id: str = "", client_secret: str = "",
                      inputs: dict[str, str] | None = None, secret_values: dict[str, str] | None = None,
                      custom: dict[str, Any] | None = None, confirm: bool = False, by: str = "",
                      open_browser: bool = True) -> dict[str, Any]:
        """Start connecting an app. Returns {"status": connected | sign_in | confirm | error, "message", ...}.
        sign_in: an OAuth sign-in is waiting in the browser (`url`); it finishes in the background."""
        spec: AppSpec | None
        if custom is not None:
            try:
                spec = custom_spec(app_id, str(custom.get("name") or app_id), url=str(custom.get("url") or ""),
                                   command=str(custom.get("command") or ""), auth=str(custom.get("auth") or "none"),
                                   client_id_needed=bool(custom.get("client_id_needed")))
            except ValueError as e:
                return {"status": "error", "message": str(e)}
            if self.catalog.get(app_id) is not None:
                return {"status": "error", "message": f"'{app_id}' is a catalog app; pick another name"}
        else:
            spec = self.spec(app_id)
        if spec is None:
            return {"status": "error", "message": f"There is no app called '{app_id}'. See `delta apps list`."}
        if spec.status in ("custom", "community") and not confirm:
            desc = self.describe_custom(spec, inputs)
            if not desc.get("ok"):
                return {"status": "error", "message": desc["message"]}
            warn = "" if spec.status == "custom" else (
                "This is a community server, not published by the app's vendor. ")
            return {"status": "confirm", "message": warn + desc["text"], **desc}
        if app_id in self.pending and not self.pending[app_id].done():
            return {"status": "sign_in", "message": f"A sign-in to {spec.name} is already open in your browser.",
                    "url": getattr(self.flows.get(app_id), "url", "")}
        if spec.auth.type in ("token", "oauth") and not self.secrets.available():
            return {"status": "error", "message": "Delta could not encrypt sign-ins with Windows DPAPI on this PC, so "
                                                  "it will not store an app's token. Repair Delta (run the installer "
                                                  "again or `delta update`)."}
        self._record("app.connect_started", app=app_id, by=by, custom=spec.status == "custom")
        try:
            if spec.transport == "stdio":
                for s in spec.secrets:
                    if not (secret_values or {}).get(s.env):
                        return {"status": "error", "message": f"{spec.name} needs {s.label or s.env}."}
                command_for(spec, inputs, self.which)  # a missing npx / uvx is reported before anything else
                if spec.secrets:
                    self.secrets.save(app_id, {"type": "env", "env": {s.env: (secret_values or {})[s.env]
                                                                       for s in spec.secrets}})
                return await self._finish_connect(spec, inputs or {}, by)
            if spec.auth.type == "token":
                if not token.strip():
                    return {"status": "error", "message": f"{spec.name} needs {spec.auth.label or 'a token'}."}
                self.secrets.save(app_id, {"type": "token", "token": token.strip()})
                return await self._finish_connect(spec, inputs or {}, by)
            if spec.auth.type == "none":
                return await self._finish_connect(spec, inputs or {}, by)
            if spec.auth.client == "user" and not client_id.strip():
                return {"status": "error", "needs": "client_id",
                        "message": f"{spec.name} needs the client id of an OAuth app you create. {spec.auth.label}"}
            return await self._start_oauth(spec, client_id, client_secret, by, open_browser)
        except RuntimeMissingError as e:
            return {"status": "error", "message": str(e), "missing_runtime": True}
        except (SecretsUnavailableError, OAuthError, ValueError) as e:
            if spec.auth.type == "token":
                self.secrets.delete(app_id)
            return {"status": "error", "message": str(e)}
        except McpError as e:
            if spec.auth.type == "token":
                self.secrets.delete(app_id)
            return {"status": "error", "message": self._friendly(spec, e)}

    async def _start_oauth(self, spec: AppSpec, client_id: str, client_secret: str, by: str,
                           open_browser: bool) -> dict[str, Any]:
        from delta.connectors.oauth import OAuthFlow

        http = self._http_factory(spec.url)
        flow = OAuthFlow(spec.url, spec.auth, spec.name, http, client_id, client_secret)
        try:
            url = await flow.start()
        except BaseException:
            await http.aclose()
            raise
        flow.url = url  # type: ignore[attr-defined]
        self.flows[spec.id] = flow

        async def finish() -> dict[str, Any]:
            try:
                tokens = await flow.finish(self.cfg.sign_in_timeout_s)
                self.secrets.save(spec.id, {"type": "oauth", **tokens.to_dict()})
                result = await self._finish_connect(spec, {}, by)
            except (OAuthError, SecretsUnavailableError) as e:
                self._record("app.connect_failed", app=spec.id, why=str(e)[:200])
                result = {"status": "error", "message": f"Connecting {spec.name} did not finish: {e}"}
            except McpError as e:
                self.secrets.delete(spec.id)
                result = {"status": "error", "message": self._friendly(spec, e)}
            finally:
                await http.aclose()
                self.flows.pop(spec.id, None)
                self.pending.pop(spec.id, None)
            return result

        self.pending[spec.id] = asyncio.create_task(finish(), name=f"connect-{spec.id}")
        if open_browser:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.open_browser, url)
        return {"status": "sign_in", "url": url,
                "message": f"Sign in to {spec.name} in the browser window on this PC to finish connecting."}

    async def wait_connect(self, app_id: str, timeout_s: float | None = None) -> dict[str, Any]:
        task = self.pending.get(app_id)
        if task is None:
            conn = self.store.get(app_id)
            return {"status": "connected" if conn else "error",
                    "message": "connected" if conn else "no sign-in is in progress"}
        return await asyncio.wait_for(asyncio.shield(task), timeout_s or self.cfg.sign_in_timeout_s + 10)

    async def _finish_connect(self, spec: AppSpec, inputs: dict[str, str], by: str) -> dict[str, Any]:
        """Open a session, read the tool list, save the connection."""
        old = self.store.get(spec.id)
        conn = Connection(spec.id, inputs=dict(inputs), connected_at=time.time(),
                          choices=dict(old.choices) if old else {},
                          trust_read_only_hints=bool(old and old.trust_read_only_hints),
                          custom=spec.model_dump() if spec.status == "custom" else None)
        self.store.items[spec.id] = conn  # so the session can find its inputs
        try:
            await self._drop(spec.id)
            client = await self._open(spec, conn)
            tools, conn.truncated = clean_tools(await client.list_tools(self.cfg.connect_timeout_s))
        except BaseException:
            if old is not None:
                self.store.items[spec.id] = old
            else:
                self.store.items.pop(spec.id, None)
            await self._drop(spec.id)
            raise
        conn.tools = tools
        conn.last_ok = time.time()
        self.store.put(conn)
        self.breaker(spec.id).record_success()
        self._record("app.connected", app=spec.id, by=by, tools=len(tools), transport=spec.transport)
        self._changed()
        return {"status": "connected", "tools": len(tools),
                "message": f"{spec.name} is connected ({len(tools)} tools). Ask me to do something in it."}

    async def disconnect(self, app_id: str, by: str = "") -> dict[str, Any]:
        spec = self.spec(app_id)
        name = spec.name if spec else app_id
        if (task := self.pending.pop(app_id, None)) is not None:
            task.cancel()
        if (flow := self.flows.pop(app_id, None)) is not None:
            await flow.cancel()
        await self._drop(app_id)
        secret = self.secrets.load(app_id)
        revoked = False
        if secret.get("type") == "oauth":
            from delta.connectors.oauth import revoke

            async with self._http_factory(str(secret.get("token_endpoint") or "")) as http:
                with contextlib.suppress(Exception):
                    revoked = await revoke(TokenSet.from_dict(secret), http)
        deleted = self.secrets.delete(app_id)
        found = self.store.remove(app_id)
        self.breakers.pop(app_id, None)
        self._record("app.disconnected", app=app_id, by=by, revoked=revoked, secrets_deleted=deleted)
        self._changed()
        if not found and not deleted:
            return {"status": "error", "message": f"{name} was not connected."}
        tail = " Its sign-in was revoked at the app and deleted from this PC." if revoked else (
            " Its saved sign-in was deleted from this PC." if deleted else "")
        return {"status": "disconnected", "revoked": revoked, "message": f"{name} is disconnected.{tail}"}

    def set_tool(self, app_id: str, tool: str, enabled: bool | None = None, risk: str | None = None) -> bool:
        conn = self.store.get(app_id)
        if conn is None or not any(t["name"] == tool for t in conn.tools):
            return False
        choice = conn.choices.setdefault(tool, {})
        if enabled is not None:
            choice["enabled"] = bool(enabled)
        if risk is not None:
            if risk not in ("", "read", "write", "external", "destructive"):
                raise ValueError("risk must be read, write, external or destructive")
            if risk:
                choice["risk"] = risk
            else:
                choice.pop("risk", None)
        self.store.save()
        self._record("app.tool_choice", app=app_id, tool=tool, enabled=enabled, risk=risk)
        self._changed()
        return True

    def set_trust_hints(self, app_id: str, trust: bool) -> bool:
        conn = self.store.get(app_id)
        if conn is None:
            return False
        conn.trust_read_only_hints = bool(trust)
        self.store.save()
        self._record("app.trust_read_only_hints", app=app_id, trust=bool(trust))
        self._changed()
        return True

    # --- sessions ----------------------------------------------------------------------------------------------
    async def _auth_headers(self, spec: AppSpec) -> dict[str, str]:
        secret = self.secrets.load(spec.id)
        kind = secret.get("type")
        if kind == "token":
            return {"Authorization": f"{spec.auth.scheme} {secret.get('token', '')}".strip()}
        if kind == "oauth":
            tokens = TokenSet.from_dict(secret)
            if tokens.expired():
                tokens = await self._refresh(spec, tokens)
            return {"Authorization": f"Bearer {tokens.access_token}"}
        if spec.auth.type in ("oauth", "token"):
            raise AppAuthError(reconnect_message(spec))
        return {}

    async def _refresh(self, spec: AppSpec, tokens: TokenSet) -> TokenSet:
        from delta.connectors.oauth import refresh

        async with self._http_factory(tokens.token_endpoint) as http:
            try:
                fresh = await refresh(tokens, http)
            except OAuthError as e:
                self._mark_reconnect(spec.id, str(e))
                raise AppAuthError(reconnect_message(spec)) from e
        self.secrets.save(spec.id, {"type": "oauth", **fresh.to_dict()})
        self._record("app.token_refreshed", app=spec.id)
        return fresh

    def _mark_reconnect(self, app_id: str, why: str) -> None:
        if (conn := self.store.get(app_id)) is not None:
            conn.needs_reconnect, conn.last_error = True, why[:300]
            self.store.save()

    def _child_env(self, spec: AppSpec) -> dict[str, str]:
        from delta.system import childenv

        declared = self.secrets.load(spec.id).get("env") or {}
        extra = {k: str(v) for k, v in declared.items() if k in {s.env for s in spec.secrets}}
        if self._env is not None:
            return self._env(extra)
        return childenv.scrubbed_env(extra)

    async def _open(self, spec: AppSpec, conn: Connection) -> McpClient:
        from delta.connectors.client import McpClient, StdioTransport, open_remote

        timeout_s = self.cfg.connect_timeout_s
        if spec.transport == "stdio":
            argv = command_for(spec, conn.inputs, self.which)
            errlog = self.folder / "logs" / f"{spec.id}.log"
            client = McpClient(StdioTransport(argv, self._child_env(spec), errlog=errlog))
            try:
                await client.initialize(timeout_s)
            except BaseException:
                await client.close()
                raise
        else:
            async def headers() -> dict[str, str]:
                return await self._auth_headers(spec)

            try:
                client = await open_remote(spec.url, spec.sse_url, headers, timeout_s)
            except McpError as e:
                secret = self.secrets.load(spec.id)
                if e.kind != "auth" or secret.get("type") != "oauth":
                    raise
                await self._refresh(spec, TokenSet.from_dict(secret))  # an access token the server ended early
                client = await open_remote(spec.url, spec.sse_url, headers, timeout_s)
        self.sessions[spec.id] = client
        return client

    async def _session(self, spec: AppSpec) -> McpClient:
        async with self._lock(spec.id):
            if (client := self.sessions.get(spec.id)) is not None:
                return client
            conn = self.store.get(spec.id)
            if conn is None:
                raise AppAuthError(f"{spec.name} is not connected. Connect it in the dashboard (Apps) first.")
            return await self._open(spec, conn)

    async def _drop(self, app_id: str) -> None:
        if (client := self.sessions.pop(app_id, None)) is not None:
            with contextlib.suppress(Exception):
                await client.close()

    async def close(self) -> None:
        for task in list(self.pending.values()):
            task.cancel()
        for flow in list(self.flows.values()):
            with contextlib.suppress(Exception):
                await flow.cancel()
        for app_id in list(self.sessions):
            await self._drop(app_id)

    async def health(self, app_id: str) -> dict[str, Any]:
        spec = self.spec(app_id)
        if spec is None or self.store.get(app_id) is None:
            return {"ok": False, "message": "not connected"}
        try:
            client = await self._session(spec)
            await client.ping(min(10.0, self.cfg.call_timeout_s))
        except (McpError, AppAuthError, RuntimeMissingError, OAuthError) as e:
            msg = str(e) if isinstance(e, AppAuthError | RuntimeMissingError) else self._friendly(spec, e)
            await self._drop(app_id)
            return {"ok": False, "message": msg}
        self.store.touch_ok(app_id)
        self.store.save()
        return {"ok": True, "message": f"{spec.name} answered."}

    # --- calls -------------------------------------------------------------------------------------------------
    def _friendly(self, spec: AppSpec, e: Exception) -> str:
        if isinstance(e, McpError) and e.kind == "auth":
            return reconnect_message(spec)
        if isinstance(e, McpError) and e.kind in ("transient", "closed"):
            return f"{spec.name} is not answering right now ({e}). Try again in a minute."
        return f"{spec.name}: {e}"

    async def call(self, app_id: str, tool: str, arguments: dict[str, Any], risk: Risk) -> CallOutcome:
        from delta.selfheal.taxonomy import classify

        spec = self.spec(app_id)
        if spec is None or self.store.get(app_id) is None:
            return CallOutcome(False, f"{app_id} is not connected. Connect it in the dashboard (Apps) first.")
        breaker = self.breaker(app_id)
        if not breaker.available():
            return CallOutcome(False, f"{spec.name} failed several times in a row, so I am leaving it alone for a "
                                      "minute. Try again shortly.", {"app": app_id, "breaker": "open"})
        attempts = 1 + (max(0, self.cfg.read_retries) if risk == Risk.READ else 0)
        started = time.monotonic()
        tried_auth = tried_session = False
        n = 0
        last = ""
        while True:
            n += 1
            try:
                client = await self._session(spec)
                res = await asyncio.wait_for(client.call_tool(tool, arguments, self.cfg.call_timeout_s),
                                             self.cfg.call_timeout_s + 5)
            except TimeoutError:
                err: McpError | None = McpError("transient", f"the app did not answer within "
                                                             f"{self.cfg.call_timeout_s:g}s (timed out)")
            except McpError as e:
                err = e
            except AppAuthError as e:
                self._audit_call(app_id, tool, False, started, n, "auth")
                return CallOutcome(False, str(e), {"app": app_id, "tool": tool, "reconnect": True})
            except RuntimeMissingError as e:
                self._audit_call(app_id, tool, False, started, n, "runtime")
                return CallOutcome(False, str(e), {"app": app_id, "tool": tool})
            except OAuthError as e:
                self._audit_call(app_id, tool, False, started, n, "auth")
                return CallOutcome(False, reconnect_message(spec) + f" ({e})", {"app": app_id, "reconnect": True})
            else:
                err = None
            if err is None:
                text, extra = _result_text(res)
                ok = not res.get("isError")
                breaker.record_success()
                self.store.touch_ok(app_id)
                self._audit_call(app_id, tool, ok, started, n, "" if ok else "tool_error")
                return CallOutcome(ok, text, {"app": app_id, "tool": tool, "attempts": n, **extra})
            last = str(err)
            if err.kind == "auth" and not tried_auth:
                tried_auth = True
                await self._drop(app_id)
                secret = self.secrets.load(app_id)
                if secret.get("type") == "oauth":
                    try:
                        await self._refresh(spec, TokenSet.from_dict(secret))
                        continue
                    except AppAuthError:
                        pass
                self._mark_reconnect(app_id, last)
                self._audit_call(app_id, tool, False, started, n, "auth")
                return CallOutcome(False, reconnect_message(spec), {"app": app_id, "tool": tool, "reconnect": True})
            if err.kind == "auth":
                self._mark_reconnect(app_id, last)
                self._audit_call(app_id, tool, False, started, n, "auth")
                return CallOutcome(False, reconnect_message(spec), {"app": app_id, "tool": tool, "reconnect": True})
            if err.kind == "session" and not tried_session:
                tried_session = True  # the server never handled it: safe to send again, even a write
                await self._drop(app_id)
                continue
            await self._drop(app_id)
            breaker.record_failure()
            kind = str(classify(tool, last))
            if n < attempts and (kind in TRANSIENT or err.kind in ("transient", "closed")):
                delay = min(10.0, err.retry_after_s or self.cfg.backoff_s * (3 ** (n - 1)))
                await asyncio.sleep(delay)
                if breaker.available():
                    continue
            self._audit_call(app_id, tool, False, started, n, err.kind)
            if conn := self.store.get(app_id):
                conn.last_error = last[:300]
            return CallOutcome(False, self._friendly(spec, err), {"app": app_id, "tool": tool, "attempts": n,
                                                                  "error_kind": err.kind})

    def _audit_call(self, app_id: str, tool: str, ok: bool, started: float, attempts: int, error: str) -> None:
        self._record("app.call", app=app_id, tool=tool, ok=ok, ms=int((time.monotonic() - started) * 1000),
                     attempts=attempts, **({"error": error} if error else {}))


def _http_client(url: str) -> httpx.AsyncClient:
    import httpx

    return httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=not is_loopback(url))


def _loop_running() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True

