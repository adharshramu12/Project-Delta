"""Signing in to a remote MCP server: the MCP authorization spec (OAuth 2.1 with PKCE).

1. Discovery: the server's 401 names its protected resource metadata (RFC 9728); that names the authorization
   server, whose metadata (RFC 8414, or OpenID Connect discovery) gives the endpoints. Servers that predate
   protected resource metadata are tried at the well-known addresses on their own origin.
2. The client: dynamic client registration (RFC 7591) when the server offers it, else the client id (and secret)
   the user created at the vendor (Google, Slack, Asana).
3. The browser: an authorization request with PKCE (S256 only), a random `state` and the `resource` indicator
   (RFC 8707), redirected to a one-shot listener on 127.0.0.1 (RFC 8252). The listener accepts exactly one
   callback whose state matches (compared in constant time) and then closes.
4. Tokens: exchanged with the code verifier, refreshed with the refresh token, revoked on disconnect (RFC 7009)
   when the server has a revocation endpoint. Tokens are handed to delta.connectors.secrets, which seals them
   with Windows DPAPI; they are never logged.

Every endpoint must be https (plain http only on 127.0.0.1, for Delta's own test server).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import html
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from delta.connectors.base import OAuthError, TokenSet
from delta.connectors.catalog import AuthSpec, check_url
from delta.system.logging import log

CALLBACK_PATH = "/callback"
DEFAULT_USER_CLIENT_PORT = 8765  # vendors that need an exact redirect URL registered (Google, Slack, Asana)
SIGN_IN_TIMEOUT_S = 300.0
_PARAM = re.compile(r'(\w+)="([^"]*)"')


@dataclass
class AuthServer:
    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str = ""
    revocation_endpoint: str = ""
    scopes_supported: list[str] = field(default_factory=list)
    token_auth_methods: list[str] = field(default_factory=list)
    challenged_scope: str = ""  # the scope the server's 401 asked for, if any


def parse_www_authenticate(header: str) -> dict[str, str]:
    """resource_metadata, scope, error... from a `Bearer k="v", ...` challenge."""
    return {k.lower(): v for k, v in _PARAM.findall(header or "")}


def _origin(url: str) -> tuple[str, str]:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}", p.path.rstrip("/")


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def _get_json(http: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    try:
        check_url(url)
        resp = await http.get(url, headers={"Accept": "application/json", "MCP-Protocol-Version": "2025-06-18"})
    except (httpx.HTTPError, ValueError):
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def discover(server_url: str, www_authenticate: str, http: httpx.AsyncClient) -> AuthServer:
    challenge = parse_www_authenticate(www_authenticate)
    origin, path = _origin(server_url)
    candidates = [challenge["resource_metadata"]] if challenge.get("resource_metadata") else []
    candidates += [f"{origin}/.well-known/oauth-protected-resource{path}",
                   f"{origin}/.well-known/oauth-protected-resource"]
    prm: dict[str, Any] | None = None
    for url in dict.fromkeys(candidates):
        if (prm := await _get_json(http, url)) is not None:
            break
    resource = server_url
    issuer = origin
    scopes: list[str] = []
    if prm is not None:
        servers = [s for s in prm.get("authorization_servers") or [] if isinstance(s, str)]
        if servers:
            issuer = servers[0]
        resource = str(prm.get("resource") or server_url)
        scopes = [str(s) for s in prm.get("scopes_supported") or []]
        # RFC 9728: the metadata must be about this server, not another one
        if _origin(resource)[0] != origin:
            raise OAuthError("the server's sign-in information names a different server")
    check_url(issuer)
    iss_origin, iss_path = _origin(issuer)
    meta_urls = ([f"{iss_origin}/.well-known/oauth-authorization-server{iss_path}",
                  f"{iss_origin}/.well-known/openid-configuration{iss_path}",
                  f"{issuer.rstrip('/')}/.well-known/openid-configuration"] if iss_path else
                 [f"{iss_origin}/.well-known/oauth-authorization-server",
                  f"{iss_origin}/.well-known/openid-configuration"])
    meta: dict[str, Any] | None = None
    for url in meta_urls:
        if (meta := await _get_json(http, url)) is not None:
            break
    if meta is None:
        if prm is not None:
            raise OAuthError("the app's sign-in server publishes no metadata")
        meta = {"authorization_endpoint": f"{origin}/authorize", "token_endpoint": f"{origin}/token",
                "registration_endpoint": f"{origin}/register", "code_challenge_methods_supported": ["S256"]}
    methods = meta.get("code_challenge_methods_supported")
    if not isinstance(methods, list) or "S256" not in methods:
        raise OAuthError("the app's sign-in server does not offer PKCE (S256), which Delta requires")
    auth_server = AuthServer(
        resource=resource, issuer=str(meta.get("issuer") or issuer),
        authorization_endpoint=str(meta.get("authorization_endpoint") or ""),
        token_endpoint=str(meta.get("token_endpoint") or ""),
        registration_endpoint=str(meta.get("registration_endpoint") or ""),
        revocation_endpoint=str(meta.get("revocation_endpoint") or ""),
        scopes_supported=scopes or [str(s) for s in meta.get("scopes_supported") or []],
        token_auth_methods=[str(m) for m in meta.get("token_endpoint_auth_methods_supported") or []],
        challenged_scope=challenge.get("scope", ""))
    for url in (auth_server.authorization_endpoint, auth_server.token_endpoint):
        if not url:
            raise OAuthError("the app's sign-in server metadata is missing an endpoint")
        check_url(url)
    for url in (auth_server.registration_endpoint, auth_server.revocation_endpoint):
        if url:
            check_url(url)
    return auth_server


def pick_auth_method(supported: list[str], has_secret: bool) -> str:
    if not has_secret:
        return "none"
    if not supported or "client_secret_post" in supported:
        return "client_secret_post"
    return "client_secret_basic"


async def register_client(server: AuthServer, redirect_uri: str, http: httpx.AsyncClient) -> tuple[str, str, str]:
    """Dynamic client registration: (client_id, client_secret, auth_method)."""
    methods = server.token_auth_methods
    wanted = "none" if not methods or "none" in methods else (
        "client_secret_post" if "client_secret_post" in methods else "client_secret_basic")
    body = {"client_name": "Delta", "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": wanted, "application_type": "native",
            "client_uri": "https://github.com/adharshramu12/Project-Delta"}
    try:
        resp = await http.post(server.registration_endpoint, json=body, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise OAuthError(f"could not register Delta with the app ({type(e).__name__})") from e
    if resp.status_code not in (200, 201):
        raise OAuthError(f"the app refused to register Delta (HTTP {resp.status_code})")
    data = resp.json()
    client_id = str(data.get("client_id") or "")
    if not client_id:
        raise OAuthError("the app registered Delta but sent no client id")
    secret = str(data.get("client_secret") or "")
    method = str(data.get("token_endpoint_auth_method") or wanted)
    return client_id, secret, "none" if not secret else method


def _client_auth(form: dict[str, str], headers: dict[str, str], client_id: str, secret: str, method: str) -> None:
    form["client_id"] = client_id
    if not secret or method == "none":
        return
    if method == "client_secret_basic":
        raw = f"{client_id}:{secret}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        form.pop("client_id", None)
    else:
        form["client_secret"] = secret


async def _token_request(url: str, form: dict[str, str], headers: dict[str, str],
                         http: httpx.AsyncClient) -> dict[str, Any]:
    try:
        resp = await http.post(url, data=form, headers={"Accept": "application/json", **headers})
    except httpx.HTTPError as e:
        raise OAuthError(f"could not reach the app's sign-in server ({type(e).__name__})") from e
    try:
        data = resp.json()
    except ValueError:
        data = dict(parse_qs(resp.text)) if "=" in resp.text else {}
        data = {k: v[0] if isinstance(v, list) else v for k, v in data.items()}
    if resp.status_code >= 400 or "error" in data or not data.get("access_token"):
        err = str(data.get("error") or f"HTTP {resp.status_code}")
        raise OAuthError(f"the app's sign-in server said: {err}")
    return dict(data)


def _tokens(data: dict[str, Any], base: TokenSet) -> TokenSet:
    expires = 0.0
    with contextlib.suppress(TypeError, ValueError):
        if data.get("expires_in"):
            expires = time.time() + float(data["expires_in"])
    return TokenSet(access_token=str(data["access_token"]), token_type=str(data.get("token_type") or "Bearer"),
                    refresh_token=str(data.get("refresh_token") or base.refresh_token), expires_at=expires,
                    scope=str(data.get("scope") or base.scope), client_id=base.client_id,
                    client_secret=base.client_secret, auth_method=base.auth_method,
                    token_endpoint=base.token_endpoint, revocation_endpoint=base.revocation_endpoint,
                    resource=base.resource, issuer=base.issuer)


async def refresh(tokens: TokenSet, http: httpx.AsyncClient) -> TokenSet:
    if not tokens.refresh_token:
        raise OAuthError("there is no refresh token: sign in again")
    form = {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token}
    if tokens.resource:
        form["resource"] = tokens.resource
    headers: dict[str, str] = {}
    _client_auth(form, headers, tokens.client_id, tokens.client_secret, tokens.auth_method)
    return _tokens(await _token_request(tokens.token_endpoint, form, headers, http), tokens)


async def revoke(tokens: TokenSet, http: httpx.AsyncClient) -> bool:
    """Best effort (RFC 7009): the refresh token first (it ends the grant), then the access token."""
    if not tokens.revocation_endpoint:
        return False
    done = False
    for token, hint in ((tokens.refresh_token, "refresh_token"), (tokens.access_token, "access_token")):
        if not token:
            continue
        form = {"token": token, "token_type_hint": hint}
        headers: dict[str, str] = {}
        _client_auth(form, headers, tokens.client_id, tokens.client_secret, tokens.auth_method)
        with contextlib.suppress(httpx.HTTPError):
            resp = await http.post(tokens.revocation_endpoint, data=form, headers=headers)
            done = done or resp.status_code in (200, 204)
    return done


# --- the loopback redirect ---------------------------------------------------------------------------------------

_PAGE = ("<!doctype html><meta charset=utf-8><title>Delta</title><body style='font:16px system-ui;margin:3em'>"
         "<h2>{title}</h2><p>{text}</p></body>")


class LoopbackReceiver:
    """A one-shot HTTP listener on 127.0.0.1 for the OAuth redirect. Only `GET /callback` with the expected state
    is accepted; it then answers a short page and stops listening."""

    def __init__(self, state: str, port: int = 0, app_name: str = "the app") -> None:
        self.state = state
        self.port = port
        self.app_name = app_name
        self.result: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()
        self.server: asyncio.base_events.Server | None = None

    async def start(self) -> str:
        try:
            self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        except OSError as e:
            raise OAuthError(f"port {self.port} on 127.0.0.1 is busy; close what uses it and try again") from e
        self.port = int(self.server.sockets[0].getsockname()[1])
        return f"http://127.0.0.1:{self.port}{CALLBACK_PATH}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
            while (await asyncio.wait_for(reader.readline(), 10)) not in (b"\r\n", b"\n", b""):
                pass
            method, _, rest = line.partition(" ")
            target = rest.split(" ", 1)[0]
            parts = urlsplit(target)
            query = {k: v[0] for k, v in parse_qs(parts.query).items()}
            if method != "GET" or parts.path != CALLBACK_PATH:
                await self._reply(writer, 404, "Not found", "This address only accepts the sign-in answer.")
                return
            if not hmac.compare_digest(query.get("state", "").encode(), self.state.encode()):
                log.warning("oauth.state_mismatch")  # someone else's request: ignored, the real one may still come
                await self._reply(writer, 400, "Sign-in not accepted", "This answer does not match the sign-in "
                                  "Delta started, so it was ignored.")
                return
            if err := query.get("error"):
                await self._reply(writer, 200, "Sign-in cancelled",
                                  f"{html.escape(self.app_name)} said: {html.escape(err)}. You can close this tab.")
                if not self.result.done():
                    self.result.set_result({"error": err, "error_description": query.get("error_description", "")})
                return
            await self._reply(writer, 200, "Delta is connected",
                              f"Delta is now connected to {html.escape(self.app_name)}. You can close this tab.")
            if not self.result.done():
                self.result.set_result({"code": query.get("code", "")})
        except Exception as e:
            log.info("oauth.callback_error", error=type(e).__name__)
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: int, title: str, text: str) -> None:
        body = _PAGE.format(title=html.escape(title), text=text).encode("utf-8")
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(status, "OK")
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/html; charset=utf-8\r\n"
                     f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n".encode()
                     + body)
        with contextlib.suppress(Exception):
            await writer.drain()

    async def wait(self, timeout_s: float) -> dict[str, str]:
        try:
            return await asyncio.wait_for(asyncio.shield(self.result), timeout_s)
        except TimeoutError as e:
            raise OAuthError("the sign-in was not finished in time; start it again") from e
        finally:
            await self.close()

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()
            self.server = None


class OAuthFlow:
    """One sign-in: start() gives the address to open in a browser, finish() waits for the answer and returns tokens."""

    def __init__(self, server_url: str, auth: AuthSpec, app_name: str, http: httpx.AsyncClient,
                 client_id: str = "", client_secret: str = "") -> None:
        self.server_url = server_url
        self.auth = auth
        self.app_name = app_name
        self.http = http
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.receiver: LoopbackReceiver | None = None
        self.server: AuthServer | None = None
        self.verifier = ""
        self.redirect_uri = ""
        self.auth_method = "none"

    async def _challenge(self) -> str:
        """The 401 an unauthenticated initialize gets: it names the server's protected resource metadata."""
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "Delta", "version": "probe"}}}
        with contextlib.suppress(httpx.HTTPError):
            resp = await self.http.post(self.server_url, json=body,
                                        headers={"Accept": "application/json, text/event-stream"})
            return resp.headers.get("www-authenticate", "")
        return ""

    async def start(self) -> str:
        self.server = await discover(self.server_url, await self._challenge(), self.http)
        state = secrets.token_urlsafe(32)
        user_client = self.auth.client == "user" or bool(self.client_id)
        port = self.auth.redirect_port or (DEFAULT_USER_CLIENT_PORT if user_client else 0)
        self.receiver = LoopbackReceiver(state, port, self.app_name)
        self.redirect_uri = await self.receiver.start()
        try:
            if self.client_id:
                self.auth_method = pick_auth_method(self.server.token_auth_methods, bool(self.client_secret))
            elif self.server.registration_endpoint:
                self.client_id, self.client_secret, self.auth_method = await register_client(
                    self.server, self.redirect_uri, self.http)
            else:
                raise OAuthError(f"{self.app_name} needs an OAuth client id: create an OAuth app at the vendor and "
                                 "connect again with its client id")
        except BaseException:
            await self.receiver.close()
            raise
        self.verifier, challenge = pkce_pair()
        scope = " ".join(self.auth.scopes) or self.server.challenged_scope
        params = {"response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri,
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
                  "resource": self.server.resource, **({"scope": scope} if scope else {}), **self.auth.extra_params}
        sep = "&" if "?" in self.server.authorization_endpoint else "?"
        return self.server.authorization_endpoint + sep + urlencode(params)

    async def finish(self, timeout_s: float = SIGN_IN_TIMEOUT_S) -> TokenSet:
        assert self.receiver is not None and self.server is not None
        answer = await self.receiver.wait(timeout_s)
        if answer.get("error"):
            raise OAuthError(f"the sign-in was not completed ({answer['error']})")
        if not answer.get("code"):
            raise OAuthError("the sign-in answer had no code")
        form = {"grant_type": "authorization_code", "code": answer["code"], "redirect_uri": self.redirect_uri,
                "code_verifier": self.verifier, "resource": self.server.resource}
        headers: dict[str, str] = {}
        _client_auth(form, headers, self.client_id, self.client_secret, self.auth_method)
        base = TokenSet("", client_id=self.client_id, client_secret=self.client_secret, auth_method=self.auth_method,
                        token_endpoint=self.server.token_endpoint,
                        revocation_endpoint=self.server.revocation_endpoint, resource=self.server.resource,
                        issuer=self.server.issuer)
        return _tokens(await _token_request(self.server.token_endpoint, form, headers, self.http), base)

    async def cancel(self) -> None:
        if self.receiver is not None:
            await self.receiver.close()
