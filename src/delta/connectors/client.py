"""A small Model Context Protocol client: JSON-RPC 2.0 over stdio, Streamable HTTP, and the older HTTP+SSE transport.

Written against the MCP specification (2025-06-18, which also accepts servers answering 2025-03-26 or 2024-11-05)
with httpx, a core dependency, so connected apps work without the optional `mcp` SDK.

Errors come back as McpError with a `kind` the manager acts on:
  auth       401 / 403 insufficient_scope: sign in again (refresh, else "reconnect <app>")
  transient  timeouts, connection errors, 429 and 5xx: a read may be retried
  fallback   the server does not speak Streamable HTTP (404 / 405 on initialize): try HTTP+SSE
  session    the server forgot our session (404 with a session id): start a new one
  protocol   anything else the server got wrong
  closed     the connection is gone
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from delta import __version__
from delta.connectors.catalog import is_loopback
from delta.system.logging import log

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
CONNECT_TIMEOUT_S = 15.0

AuthHeaders = Callable[[], Awaitable[dict[str, str]]]


class McpError(Exception):
    def __init__(self, kind: str, message: str, *, www_authenticate: str = "", status: int = 0,
                 retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.www_authenticate = www_authenticate
        self.status = status
        self.retry_after_s = retry_after_s


async def _no_auth() -> dict[str, str]:
    return {}


def _rpc_error(msg: dict[str, Any]) -> McpError:
    err = msg.get("error") or {}
    return McpError("protocol", f"server error {err.get('code', '')}: {str(err.get('message', ''))[:300]}")


def _retry_after(resp: httpx.Response) -> float | None:
    with contextlib.suppress(TypeError, ValueError):
        return float(resp.headers.get("retry-after", ""))
    return None


def _status_error(resp: httpx.Response, has_session: bool, initializing: bool) -> McpError:
    code = resp.status_code
    www = resp.headers.get("www-authenticate", "")
    if code == 401 or (code == 403 and "insufficient_scope" in www):
        return McpError("auth", f"HTTP {code} unauthorized: the sign-in is missing, expired or was revoked",
                        www_authenticate=www, status=code)
    if code == 404 and has_session and not initializing:
        return McpError("session", "the server ended this session", status=code)
    if code in (404, 405) and initializing:
        return McpError("fallback", f"HTTP {code}: no Streamable HTTP endpoint here", status=code)
    if code == 429 or code >= 500:
        return McpError("transient", f"HTTP {code} from the app's server", status=code,
                        retry_after_s=_retry_after(resp))
    return McpError("protocol", f"HTTP {code} from the app's server", status=code)


async def sse_events(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, str]]:
    """(event, data) pairs from a text/event-stream (multi-line data joined with newlines)."""
    event, data = "message", list[str]()
    async for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "message", list[str]()
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value or "message"
        elif field == "data":
            data.append(value)
    if data:
        yield event, "\n".join(data)


class _Pending:
    """Request ids and the futures waiting for their responses (stdio and SSE: answers arrive on a stream)."""

    def __init__(self) -> None:
        self.ids = itertools.count(1)
        self.waiting: dict[int | str, asyncio.Future[dict[str, Any]]] = {}

    def new(self) -> tuple[int, asyncio.Future[dict[str, Any]]]:
        rid = next(self.ids)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.waiting[rid] = fut
        return rid, fut

    def resolve(self, msg: dict[str, Any]) -> bool:
        fut = self.waiting.pop(msg.get("id"), None)  # type: ignore[arg-type]
        if fut is None or fut.done():
            return False
        fut.set_result(msg)
        return True

    def fail_all(self, error: McpError) -> None:
        for fut in self.waiting.values():
            if not fut.done():
                fut.set_exception(error)
        self.waiting.clear()


def _server_request_reply(msg: dict[str, Any]) -> dict[str, Any]:
    """Our answer to a request the server sends us: ping is answered; Delta offers no other client features."""
    if msg.get("method") == "ping":
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
    return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported by Delta"}}


class Transport:
    name = "transport"

    async def open(self) -> None: ...

    async def request(self, method: str, params: dict[str, Any] | None, timeout_s: float) -> dict[str, Any]:
        raise NotImplementedError

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None: ...

    async def close(self) -> None: ...

    negotiated: str = PROTOCOL_VERSION


# --- stdio ---------------------------------------------------------------------------------------------------

class StdioTransport(Transport):
    """A local server: newline-delimited JSON-RPC on the program's stdin/stdout. stderr goes to a log file."""

    name = "stdio"

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str | None = None,
                 errlog: Path | None = None) -> None:
        self.argv = argv
        self.env = env
        self.cwd = cwd
        self.errlog = errlog
        self.proc: asyncio.subprocess.Process | None = None
        self._pending = _Pending()
        self._reader: asyncio.Task[None] | None = None
        self._err: Any = None
        self._write_lock = asyncio.Lock()

    async def open(self) -> None:
        kw: dict[str, Any] = {}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: no console window pops up
        if self.errlog is not None:
            self.errlog.parent.mkdir(parents=True, exist_ok=True)
            self._err = self.errlog.open("ab")
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=self._err if self._err is not None else asyncio.subprocess.DEVNULL, env=self.env,
                cwd=self.cwd, limit=MAX_MESSAGE_BYTES, **kw)
        except OSError as e:
            await self.close()
            raise McpError("closed", f"could not start {os.path.basename(self.argv[0])}: {e}") from e
        self._reader = asyncio.create_task(self._read(), name="mcp-stdio-reader")

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    async def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue  # a server printing a banner on stdout: not a message
                if not isinstance(msg, dict):
                    continue
                if "method" in msg and "id" in msg:
                    await self._send(_server_request_reply(msg))
                elif "id" in msg:
                    self._pending.resolve(msg)
        except Exception as e:  # a broken pipe or a huge line: every waiting call fails cleanly
            log.info("mcp.stdio_reader_ended", error=str(e))
        finally:
            self._pending.fail_all(McpError("closed", "the app's server program stopped"))

    async def _send(self, msg: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None or self.proc.returncode is not None:
            raise McpError("closed", "the app's server program is not running")
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                self.proc.stdin.write(data)
                await self.proc.stdin.drain()
            except (ConnectionError, RuntimeError) as e:
                raise McpError("closed", f"the app's server program stopped ({type(e).__name__})") from e

    async def request(self, method: str, params: dict[str, Any] | None, timeout_s: float) -> dict[str, Any]:
        rid, fut = self._pending.new()
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params else {})})
        try:
            msg = await asyncio.wait_for(fut, timeout_s)
        except TimeoutError as e:
            self._pending.waiting.pop(rid, None)
            with contextlib.suppress(McpError):
                await self.notify("notifications/cancelled", {"requestId": rid, "reason": "timed out"})
            raise McpError("transient", f"the app did not answer within {timeout_s:g}s (timed out)") from e
        if "error" in msg:
            raise _rpc_error(msg)
        return dict(msg.get("result") or {})

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(BaseException):
                await self._reader
        proc, self.proc = self.proc, None
        if proc is not None and proc.returncode is None:
            await _end_tree(proc)
        if self._err is not None:
            with contextlib.suppress(OSError):
                self._err.close()
            self._err = None


async def _end_tree(proc: asyncio.subprocess.Process) -> None:
    """End a local server with everything it started (npx starts node, uvx starts python): its children through
    delta.system.proctree, then the program itself through asyncio, which also collects its exit status."""
    import psutil

    from delta.system.proctree import kill_tree

    with contextlib.suppress(Exception):
        if proc.stdin is not None:
            proc.stdin.close()
    try:
        children = [(c.pid, c.create_time()) for c in psutil.Process(proc.pid).children(recursive=True)]
    except psutil.Error:
        children = []
    for pid, created in children:
        await asyncio.to_thread(kill_tree, pid, created, 2.0)
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), 3)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 3)


# --- Streamable HTTP ---------------------------------------------------------------------------------------------

class HttpTransport(Transport):
    """Streamable HTTP: every message is a POST; the answer is JSON or a short event stream."""

    name = "http"

    def __init__(self, url: str, auth: AuthHeaders = _no_auth, client: httpx.AsyncClient | None = None,
                 extra_headers: dict[str, str] | None = None) -> None:
        self.url = url
        self.auth = auth
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=CONNECT_TIMEOUT_S),
                                                  follow_redirects=False, trust_env=not is_loopback(url))
        self._own_client = client is None
        self.extra = dict(extra_headers or {})
        self.session_id = ""
        self.ids = itertools.count(1)
        self._initializing = False

    async def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **self.extra}
        h.update(await self.auth())
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if not self._initializing:
            h["MCP-Protocol-Version"] = self.negotiated
        return h

    async def _post(self, msg: dict[str, Any], timeout_s: float) -> dict[str, Any] | None:
        want = msg.get("id")
        try:
            async with self.client.stream("POST", self.url, json=msg, headers=await self._headers(),
                                          timeout=httpx.Timeout(timeout_s, connect=CONNECT_TIMEOUT_S)) as resp:
                if resp.status_code >= 400 or resp.status_code in (301, 302, 303, 307, 308):
                    await resp.aread()
                    raise _status_error(resp, bool(self.session_id), self._initializing)
                if sid := resp.headers.get("mcp-session-id"):
                    self.session_id = sid
                if want is None or resp.status_code == 202:
                    return None
                ctype = resp.headers.get("content-type", "").lower()
                if "text/event-stream" in ctype:
                    async for _event, data in sse_events(resp.aiter_lines()):
                        try:
                            item = json.loads(data)
                        except ValueError:
                            continue
                        for one in item if isinstance(item, list) else [item]:
                            if not isinstance(one, dict):
                                continue
                            if "method" in one and "id" in one:
                                await self._post(_server_request_reply(one), 30)
                            elif one.get("id") == want:
                                return one
                    raise McpError("transient", "the app's server closed the stream before answering")
                body = await resp.aread()
                if len(body) > MAX_MESSAGE_BYTES:
                    raise McpError("protocol", "the app's answer is too large")
                try:
                    parsed = json.loads(body)
                except ValueError as e:
                    raise McpError("fallback" if self._initializing else "protocol",
                                   "the app's server did not answer with JSON-RPC") from e
                if isinstance(parsed, list):
                    parsed = next((p for p in parsed if isinstance(p, dict) and p.get("id") == want), {})
                return dict(parsed)
        except httpx.TimeoutException as e:
            raise McpError("transient", f"the app did not answer within {timeout_s:g}s (timed out)") from e
        except httpx.TransportError as e:
            raise McpError("transient", f"could not reach the app's server ({type(e).__name__})") from e

    async def request(self, method: str, params: dict[str, Any] | None, timeout_s: float) -> dict[str, Any]:
        self._initializing = method == "initialize"
        try:
            msg = await self._post({"jsonrpc": "2.0", "id": next(self.ids), "method": method,
                                    **({"params": params} if params else {})}, timeout_s)
        finally:
            self._initializing = False
        if not msg:
            raise McpError("protocol", "the app's server sent no answer")
        if "error" in msg:
            raise _rpc_error(msg)
        return dict(msg.get("result") or {})

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}, 30)

    async def close(self) -> None:
        if self.session_id:
            with contextlib.suppress(Exception):  # tell the server we are done (it may not support it)
                await self.client.delete(self.url, headers=await self._headers(), timeout=5)
            self.session_id = ""
        if self._own_client:
            await self.client.aclose()


# --- HTTP + SSE (the 2024-11-05 transport) -----------------------------------------------------------------------

class SseTransport(Transport):
    """The older transport some servers still offer: one GET event stream for answers, POSTs to an endpoint."""

    name = "sse"

    def __init__(self, url: str, auth: AuthHeaders = _no_auth, client: httpx.AsyncClient | None = None) -> None:
        self.url = url
        self.auth = auth
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=CONNECT_TIMEOUT_S),
                                                  follow_redirects=False, trust_env=not is_loopback(url))
        self._own_client = client is None
        self.endpoint = ""
        self._pending = _Pending()
        self._reader: asyncio.Task[None] | None = None
        self.negotiated = "2024-11-05"

    async def open(self) -> None:
        ready: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._reader = asyncio.create_task(self._read(ready), name="mcp-sse-reader")
        try:
            self.endpoint = await asyncio.wait_for(ready, CONNECT_TIMEOUT_S)
        except TimeoutError as e:
            await self.close()
            raise McpError("transient", "the app's server did not open its event stream") from e

    async def _read(self, ready: asyncio.Future[str]) -> None:
        headers = {"Accept": "text/event-stream", **(await self.auth())}
        try:
            async with self.client.stream("GET", self.url, headers=headers,
                                          timeout=httpx.Timeout(None, connect=CONNECT_TIMEOUT_S)) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise _status_error(resp, False, False)
                async for event, data in sse_events(resp.aiter_lines()):
                    if event == "endpoint" and not ready.done():
                        endpoint = urljoin(self.url, data.strip())
                        if httpx.URL(endpoint).host != httpx.URL(self.url).host:
                            raise McpError("protocol", "the server pointed Delta at another host")
                        ready.set_result(endpoint)
                        continue
                    try:
                        msg = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and "method" in msg and "id" in msg:
                        await self._post(_server_request_reply(msg))
                    elif isinstance(msg, dict) and "id" in msg:
                        self._pending.resolve(msg)
        except McpError as e:
            if not ready.done():
                ready.set_exception(e)
            self._pending.fail_all(e)
            return
        except Exception as e:
            err = McpError("transient", f"the app's event stream broke ({type(e).__name__})")
            if not ready.done():
                ready.set_exception(err)
            self._pending.fail_all(err)
            return
        self._pending.fail_all(McpError("closed", "the app's server closed the event stream"))

    async def _post(self, msg: dict[str, Any]) -> None:
        try:
            resp = await self.client.post(self.endpoint, json=msg,
                                          headers={"Content-Type": "application/json", **(await self.auth())})
        except httpx.TransportError as e:
            raise McpError("transient", f"could not reach the app's server ({type(e).__name__})") from e
        if resp.status_code >= 400:
            raise _status_error(resp, False, False)

    async def request(self, method: str, params: dict[str, Any] | None, timeout_s: float) -> dict[str, Any]:
        rid, fut = self._pending.new()
        await self._post({"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params else {})})
        try:
            msg = await asyncio.wait_for(fut, timeout_s)
        except TimeoutError as e:
            self._pending.waiting.pop(rid, None)
            raise McpError("transient", f"the app did not answer within {timeout_s:g}s (timed out)") from e
        if "error" in msg:
            raise _rpc_error(msg)
        return dict(msg.get("result") or {})

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(BaseException):
                await self._reader
        if self._own_client:
            await self.client.aclose()


# --- the client ------------------------------------------------------------------------------------------------

class McpClient:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self.server_info: dict[str, Any] = {}
        self.instructions = ""

    async def initialize(self, timeout_s: float = 30) -> dict[str, Any]:
        await self.transport.open()
        result = await self.transport.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "Delta", "version": __version__}}, timeout_s)
        version = str(result.get("protocolVersion") or "")
        if version not in SUPPORTED_VERSIONS:
            raise McpError("protocol", f"the app's server speaks MCP {version or '(unknown)'}, which Delta does not")
        self.transport.negotiated = version
        self.server_info = dict(result.get("serverInfo") or {})
        self.instructions = str(result.get("instructions") or "")[:2000]
        await self.transport.notify("notifications/initialized")
        return result

    async def list_tools(self, timeout_s: float = 30) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = None
        for _ in range(20):  # a server that pages forever must not hang Delta
            res = await self.transport.request("tools/list", {"cursor": cursor} if cursor else None, timeout_s)
            tools += [t for t in res.get("tools") or [] if isinstance(t, dict) and t.get("name")]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        return await self.transport.request("tools/call", {"name": name, "arguments": arguments}, timeout_s)

    async def ping(self, timeout_s: float = 10) -> None:
        await self.transport.request("ping", None, timeout_s)

    async def close(self) -> None:
        await self.transport.close()


async def open_remote(url: str, sse_url: str, auth: AuthHeaders, timeout_s: float = 30,
                      client: httpx.AsyncClient | None = None,
                      extra_headers: dict[str, str] | None = None) -> McpClient:
    """Streamable HTTP first; when the server does not speak it, the older HTTP+SSE transport (spec section
    'Backwards Compatibility'), on the server's own SSE address when the catalog knows it."""
    http = McpClient(HttpTransport(url, auth, client, extra_headers))
    try:
        await http.initialize(timeout_s)
        return http
    except McpError as e:
        await http.close()
        if e.kind != "fallback":
            raise
    sse = McpClient(SseTransport(sse_url or url, auth, client))
    try:
        await sse.initialize(timeout_s)
    except BaseException:
        await sse.close()
        raise
    return sse
