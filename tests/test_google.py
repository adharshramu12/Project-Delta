"""Google sign-in and Calendar API, against a fake Google server (no real account touched)."""

import base64
import hashlib
import json
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from delta.integrations import google
from delta.tools import meetings


@pytest.fixture
def secrets(monkeypatch):
    store = {"google_client_id": "123.apps.googleusercontent.com", "google_refresh_token": "refresh-1"}
    monkeypatch.setattr(google, "get_secret", store.get)
    monkeypatch.setattr(google, "set_secret", store.__setitem__)
    monkeypatch.setattr(google, "delete_secret", lambda k: store.pop(k, None))
    google._cache.clear()
    return store


class FakeGoogle:
    """Just enough of oauth2.googleapis.com and the Calendar API."""

    def __init__(self):
        self.events: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.refreshes = 0
        self.revoked = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, method = request.url, request.method
        params = dict(url.params)
        body = json.loads(request.content) if request.content and request.headers.get("content-type", "").startswith(
            "application/json") else parse_qs(request.content.decode())
        self.calls.append((method, url.path, params))
        if url.host == "oauth2.googleapis.com":
            if self.revoked:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.refreshes += 1
            return httpx.Response(200, json={"access_token": f"at-{self.refreshes}", "expires_in": 3600})
        assert request.headers["Authorization"].startswith("Bearer at-")
        path = url.path.removeprefix("/calendar/v3/calendars/primary/events")
        if method == "POST":
            eid = f"e{len(self.events) + 1}"
            self.events[eid] = {**body, "id": eid, "hangoutLink": "https://meet.google.com/abc-defg-hij",
                                "attendees": body.get("attendees", [])}
            return httpx.Response(200, json=self.events[eid])
        if method == "GET" and path == "":
            q = params.get("q", "").lower()
            return httpx.Response(200, json={"items": [e for e in self.events.values()
                                                       if q in e.get("summary", "").lower()]})
        eid = path.strip("/")
        if eid not in self.events:
            return httpx.Response(404, json={"error": {"message": "Not Found"}})
        if method == "GET":
            return httpx.Response(200, json=self.events[eid])
        if method == "PATCH":
            self.events[eid].update(body)
            return httpx.Response(200, json=self.events[eid])
        if method == "DELETE":
            del self.events[eid]
            return httpx.Response(204)
        return httpx.Response(405)


@pytest.fixture
def fake(monkeypatch, secrets):
    server = FakeGoogle()
    transport = httpx.MockTransport(server)
    real = httpx.AsyncClient

    def client(*a, **kw):
        kw["transport"] = transport
        return real(*a, **kw)

    monkeypatch.setattr(google.httpx, "AsyncClient", client)
    monkeypatch.setattr(meetings, "_google_connected", lambda: True)
    monkeypatch.setattr(meetings, "copy_to_clipboard", lambda text: True)
    return server


def test_pkce_and_consent_url_follow_the_standard():
    verifier, challenge = google.pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = parse_qs(urlparse(google.consent_url("cid", "http://127.0.0.1:5555", challenge, "st")).query)
    assert q["code_challenge_method"] == ["S256"] and q["state"] == ["st"] and q["access_type"] == ["offline"]
    assert q["scope"] == ["https://www.googleapis.com/auth/calendar.events"]  # least privilege


async def test_access_token_is_cached_and_refreshed_once(fake):
    cal = google.GoogleCalendar()
    await cal.create("A", datetime(2030, 1, 1, 10), datetime(2030, 1, 1, 10, 30), [])
    await cal.find("A")
    assert fake.refreshes == 1  # one refresh serves both calls


async def test_revoked_access_clears_the_token_and_says_reconnect(fake, secrets):
    fake.revoked = True
    with pytest.raises(google.GoogleError, match="connect Google again"):
        await google.GoogleCalendar().find("x")
    assert "google_refresh_token" not in secrets


async def test_schedule_via_api_creates_meet_and_reads_it_back(fake):
    r = await meetings.ScheduleMeeting().run(meetings.ScheduleArgs(
        title="Sprint sync", start="2030-01-07T17:00", minutes=45, guests=["ravi@x.com"]))
    assert r.ok and r.evidence["verified"] and r.evidence["via"] == "api"
    assert r.evidence["link"] == "https://meet.google.com/abc-defg-hij" and "ravi@x.com" in r.output
    post = next(c for c in fake.calls if c[0] == "POST" and c[1].endswith("/events"))
    assert post[2] == {"conferenceDataVersion": "1", "sendUpdates": "all"}
    assert any(c[0] == "GET" and c[1].endswith("/e1") for c in fake.calls)  # read back as proof


async def test_edit_via_api_moves_keeps_length_and_verifies(fake):
    await google.GoogleCalendar().create("Delta review", datetime(2030, 1, 7, 10), datetime(2030, 1, 7, 10, 30), [])
    r = await meetings.EditMeeting().run(meetings.EditArgs(title="Delta review", new_start="12:00"))
    assert r.ok and r.evidence["verified"], r.output
    e = google.Event.parse(fake.events["e1"])
    assert (e.start, e.end) == (datetime(2030, 1, 7, 12), datetime(2030, 1, 7, 12, 30))


async def test_edit_asks_which_one_when_several_match(fake):
    cal = google.GoogleCalendar()
    for day in (7, 8):
        await cal.create("Standup", datetime(2030, 1, day, 9), datetime(2030, 1, day, 9, 15), [])
    r = await meetings.EditMeeting().run(meetings.EditArgs(title="Standup", new_start="10:00"))
    assert not r.ok and "Several meetings" in r.output and len(r.evidence["matches"]) == 2


async def test_cancel_via_api_confirms_it_is_gone(fake):
    await google.GoogleCalendar().create("Old sync", datetime(2030, 1, 7, 9), datetime(2030, 1, 7, 9, 30), [])
    r = await meetings.EditMeeting().run(meetings.EditArgs(title="Old sync", cancel=True))
    assert r.ok and r.evidence["verified"] and not fake.events


def test_event_parse_reads_meet_from_conference_data():
    e = google.Event.parse({"id": "x", "summary": "T", "start": {"dateTime": "2030-01-07T10:00:00+05:30"},
                            "end": {"dateTime": "2030-01-07T10:30:00+05:30"},
                            "conferenceData": {"entryPoints": [{"entryPointType": "video",
                                                                "uri": "https://meet.google.com/x"}]}})
    assert e.meet_link == "https://meet.google.com/x" and e.title == "T"
