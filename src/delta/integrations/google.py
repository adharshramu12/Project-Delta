"""Google sign-in (OAuth 2.0 for desktop apps) and the Google Calendar API.

Why: driving Calendar's web page takes ~30 s per change and breaks when Google redesigns it. The
API is exact, takes well under a second, costs no AI tokens, and returns the Meet link itself.

Security model (Google's recommended flow for installed apps, RFC 8252 + RFC 7636):
- Authorization code flow with PKCE (S256) and a one-time loopback redirect on 127.0.0.1.
- `state` is checked against CSRF; the listener accepts exactly one callback, then closes.
- Least privilege: only the `calendar.events` scope.
- The refresh token lives in Windows Credential Manager (delta.system.secrets), never in files or
  logs. The client id/secret identify Delta's Google Cloud app (a desktop client's "secret" is not
  confidential per Google's docs) and are also kept in the credential store.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import secrets as pysecrets
import time
import urllib.parse
import uuid
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx

from delta.system.secrets import delete_secret, get_secret, set_secret

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CALENDAR = "https://www.googleapis.com/calendar/v3"
SCOPES = ("https://www.googleapis.com/auth/calendar.events",)

CLIENT_ID = "google_client_id"
CLIENT_SECRET = "google_client_secret"
REFRESH_TOKEN = "google_refresh_token"
TIMEOUT = httpx.Timeout(15.0)


class GoogleError(RuntimeError):
    """A Google request failed in a way the user or agent should hear about."""


# --- sign-in -------------------------------------------------------------------------------------

def pkce_pair() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge) per RFC 7636."""
    verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def consent_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(SCOPES), "code_challenge": challenge, "code_challenge_method": "S256",
        "state": state, "access_type": "offline", "prompt": "consent"})


@dataclass
class _Callback:
    code: str | None = None
    state: str | None = None
    error: str | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)


def _listen_once(port: int, box: _Callback, loop: asyncio.AbstractEventLoop) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            box.code = next(iter(q.get("code", [])), None)
            box.state = next(iter(q.get("state", [])), None)
            box.error = next(iter(q.get("error", [])), None)
            ok = box.code and not box.error
            body = ("Delta is connected to Google. You can close this tab." if ok else
                    f"Google sign-in did not finish ({box.error or 'no code'}). You can close this tab.")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())
            loop.call_soon_threadsafe(box.done.set)

        def log_message(self, *args: Any) -> None:  # keep codes out of the console
            return

    return HTTPServer(("127.0.0.1", port), Handler)


async def sign_in(timeout_s: float = 180) -> str:
    """Run the browser consent flow and store the refresh token. Returns the signed-in email."""
    client_id, client_secret = get_secret(CLIENT_ID), get_secret(CLIENT_SECRET)
    if not client_id:
        raise GoogleError("Google is not set up yet: add Delta's Google client ID first "
                          "(delta secrets set google_client_id).")
    loop = asyncio.get_running_loop()
    box = _Callback()
    server = _listen_once(0, box, loop)  # port 0: the OS picks a free one
    redirect = f"http://127.0.0.1:{server.server_port}"
    verifier, challenge = pkce_pair()
    state = pysecrets.token_urlsafe(24)
    worker = loop.run_in_executor(None, server.handle_request)  # exactly one request
    try:
        webbrowser.open(consent_url(client_id, redirect, challenge, state))
        await asyncio.wait_for(box.done.wait(), timeout_s)
    except TimeoutError as e:
        raise GoogleError("Google sign-in timed out: nothing came back from the browser.") from e
    finally:
        server.server_close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(worker, 1)
    if box.error or not box.code:
        raise GoogleError(f"Google sign-in was not completed ({box.error or 'no code'}).")
    if not pysecrets.compare_digest(box.state or "", state):
        raise GoogleError("Google sign-in answer did not match this request (state mismatch); try again.")
    data = {"code": box.code, "client_id": client_id, "redirect_uri": redirect, "grant_type": "authorization_code",
            "code_verifier": verifier}
    if client_secret:
        data["client_secret"] = client_secret
    async with httpx.AsyncClient(timeout=TIMEOUT) as http:
        r = await http.post(TOKEN_URL, data=data)
    tokens = _json(r, "exchanging the sign-in code")
    if not tokens.get("refresh_token"):
        raise GoogleError("Google did not return a long-lived token; remove Delta's access in your Google "
                          "account and connect again.")
    await asyncio.to_thread(set_secret, REFRESH_TOKEN, tokens["refresh_token"])
    _cache.set(tokens)
    cal = await GoogleCalendar().calendar_info()
    return str(cal.get("id", ""))


async def sign_out() -> None:
    token = get_secret(REFRESH_TOKEN)
    if token:
        with contextlib.suppress(httpx.HTTPError):
            async with httpx.AsyncClient(timeout=TIMEOUT) as http:
                await http.post(REVOKE_URL, data={"token": token})
    await asyncio.to_thread(delete_secret, REFRESH_TOKEN)
    _cache.clear()


def connected() -> bool:
    return bool(get_secret(CLIENT_ID) and get_secret(REFRESH_TOKEN))


# --- access tokens -------------------------------------------------------------------------------

class _TokenCache:
    def __init__(self) -> None:
        self.token: str | None = None
        self.expires = 0.0
        self.lock = asyncio.Lock()

    def set(self, data: dict[str, Any]) -> None:
        self.token = data.get("access_token")
        self.expires = time.monotonic() + float(data.get("expires_in", 3600)) - 60  # refresh a minute early

    def clear(self) -> None:
        self.token, self.expires = None, 0.0


_cache = _TokenCache()


async def access_token(http: httpx.AsyncClient) -> str:
    async with _cache.lock:
        if _cache.token and time.monotonic() < _cache.expires:
            return _cache.token
        refresh, client_id = get_secret(REFRESH_TOKEN), get_secret(CLIENT_ID)
        if not (refresh and client_id):
            raise GoogleError("Google is not connected (Connected apps → Google).")
        data = {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id}
        if secret := get_secret(CLIENT_SECRET):
            data["client_secret"] = secret
        r = await http.post(TOKEN_URL, data=data)
        if r.status_code in (400, 401) and "invalid_grant" in r.text:
            await asyncio.to_thread(delete_secret, REFRESH_TOKEN)
            raise GoogleError("Google access was removed or expired: connect Google again.")
        _cache.set(_json(r, "refreshing Google access"))
        assert _cache.token
        return _cache.token


def _json(r: httpx.Response, doing: str) -> dict[str, Any]:
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400:
        msg = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else data.get("error")
        raise GoogleError(f"Google refused {doing}: HTTP {r.status_code} {msg or r.text[:160]}")
    return data


# --- Calendar ------------------------------------------------------------------------------------

@dataclass
class Event:
    id: str
    title: str
    start: datetime
    end: datetime
    meet_link: str
    guests: list[str]
    html_link: str

    @staticmethod
    def parse(raw: dict[str, Any]) -> Event:
        def when(part: dict[str, Any]) -> datetime:
            value = part.get("dateTime") or part.get("date") or "1970-01-01"
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)

        meet = raw.get("hangoutLink") or next(
            (e.get("uri", "") for e in (raw.get("conferenceData") or {}).get("entryPoints", [])
             if e.get("entryPointType") == "video"), "")
        return Event(raw["id"], raw.get("summary", ""), when(raw.get("start", {})), when(raw.get("end", {})),
                     meet, [a["email"] for a in raw.get("attendees", []) if a.get("email")], raw.get("htmlLink", ""))


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone().isoformat()  # local wall time -> with the PC's UTC offset


class GoogleCalendar:
    """The user's primary calendar. Every call refreshes access automatically."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def _call(self, method: str, path: str, doing: str, **kw: Any) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as http:
            token = await access_token(http)
            r = await http.request(method, CALENDAR + path, headers={"Authorization": f"Bearer {token}"}, **kw)
        if method == "DELETE" and r.status_code in (200, 204):
            return {}
        return _json(r, doing)

    async def calendar_info(self) -> dict[str, Any]:
        return await self._call("GET", "/calendars/primary", "reading your calendar")

    async def find(self, title: str, start: datetime | None = None, days: int = 60) -> list[Event]:
        lo = start.replace(hour=0, minute=0) if start else datetime.now()
        params = {"q": title, "singleEvents": "true", "orderBy": "startTime", "maxResults": "10",
                  "timeMin": _rfc3339(lo), "timeMax": _rfc3339(lo.replace(hour=23, minute=59) if start
                                                             else datetime.fromtimestamp(time.time() + days * 86400))}
        data = await self._call("GET", "/calendars/primary/events", "searching your calendar", params=params)
        want = title.lower()
        return [Event.parse(e) for e in data.get("items", []) if want in e.get("summary", "").lower()]

    async def create(self, title: str, start: datetime, end: datetime, guests: list[str], details: str = "",
                     notify: bool = True) -> Event:
        body = {"summary": title, "description": details, "start": {"dateTime": _rfc3339(start)},
                "end": {"dateTime": _rfc3339(end)}, "attendees": [{"email": g} for g in guests],
                "conferenceData": {"createRequest": {"requestId": uuid.uuid4().hex,
                                                     "conferenceSolutionKey": {"type": "hangoutsMeet"}}}}
        params = {"conferenceDataVersion": "1", "sendUpdates": "all" if notify and guests else "none"}
        return Event.parse(await self._call("POST", "/calendars/primary/events", "creating the event",
                                            params=params, json=body))

    async def update(self, event_id: str, *, title: str | None = None, start: datetime | None = None,
                     end: datetime | None = None, notify: bool = True) -> Event:
        body: dict[str, Any] = {}
        if title:
            body["summary"] = title
        if start:
            body["start"] = {"dateTime": _rfc3339(start)}
        if end:
            body["end"] = {"dateTime": _rfc3339(end)}
        return Event.parse(await self._call("PATCH", f"/calendars/primary/events/{event_id}", "updating the event",
                                            params={"sendUpdates": "all" if notify else "none"}, json=body))

    async def get(self, event_id: str) -> Event:
        return Event.parse(await self._call("GET", f"/calendars/primary/events/{event_id}", "reading the event"))

    async def delete(self, event_id: str, notify: bool = True) -> None:
        await self._call("DELETE", f"/calendars/primary/events/{event_id}", "cancelling the event",
                         params={"sendUpdates": "all" if notify else "none"})
