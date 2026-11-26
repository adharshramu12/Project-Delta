"""Voice mode: one spoken turn to text and language, spoken turns answered short and plain, the dashboard parts."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from delta.api.server import create_app, load_token
from delta.bootstrap import build
from delta.channels import voice
from delta.config import Settings
from delta.core.state import TaskRequest

STATIC = Path(__file__).resolve().parents[1] / "src" / "delta" / "api" / "static"


@pytest.fixture
def client(tmp_path):
    c = build(Settings(data_dir=tmp_path))
    token = load_token(tmp_path)
    with TestClient(create_app(c, token)) as tc:
        tc.headers["Authorization"] = f"Bearer {token}"
        yield tc


def test_turn_to_text_and_language(client, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    async def fake(audio: bytes, filename: str = "", content_type: str = "") -> tuple[str, str]:
        seen.update(size=len(audio), filename=filename, type=content_type)
        return "namaste delta, aaj ka mausam kaisa hai", "hindi"

    monkeypatch.setattr(voice, "transcribe_with_language", fake)
    r = client.post("/api/voice/transcribe", content=b"\x1aE\xdf\xa3" + b"0" * 3000,
                    headers={"Content-Type": "audio/webm;codecs=opus"})
    assert r.json() == {"text": "namaste delta, aaj ka mausam kaisa hai", "language": "hindi"}
    assert seen == {"size": 3004, "filename": "turn.webm", "type": "audio/webm"}


def test_turn_needs_a_token_audio_and_a_sane_size(client, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("not for these")

    monkeypatch.setattr(voice, "transcribe_with_language", boom)
    wrong = {"Content-Type": "audio/webm", "Authorization": "Bearer x"}
    assert client.post("/api/voice/transcribe", content=b"0" * 3000, headers=wrong).status_code == 401
    assert client.post("/api/voice/transcribe", content=b"0" * 3000,
                       headers={"Content-Type": "text/html"}).status_code == 415
    assert client.post("/api/voice/transcribe", content=b"0" * 50,
                       headers={"Content-Type": "audio/webm"}).json() == {"text": "", "language": ""}
    assert client.post("/api/voice/transcribe", content=b"0" * 8_000_001,
                       headers={"Content-Type": "audio/webm"}).status_code == 413


def test_a_speech_problem_comes_back_as_words(client, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_key(*a, **k):
        raise voice.VoiceError("To understand voice notes I need a free Groq key")

    monkeypatch.setattr(voice, "transcribe_with_language", no_key)
    r = client.post("/api/voice/transcribe", content=b"0" * 3000, headers={"Content-Type": "audio/ogg"}).json()
    assert r["text"] == "" and "Groq key" in r["error"]


def test_whisper_language_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice, "get_secret", lambda name: "k")
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = request.content
        return httpx.Response(200, json={"text": " Hello there ", "language": "English"})

    real = httpx.AsyncClient
    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **k: real(transport=httpx.MockTransport(handler)))
    assert asyncio.run(voice.transcribe_with_language(b"x", "t.webm", "audio/webm")) == ("Hello there", "english")
    assert b"verbose_json" in sent["body"]
    assert asyncio.run(voice.transcribe(b"x")) == "Hello there"  # Telegram's voice notes keep working the same


def test_a_spoken_turn_asks_for_a_short_plain_answer() -> None:
    plain = TaskRequest("what's the time", channel="api")
    spoken = TaskRequest("what's the time", channel="api", spoken=True)
    assert "voice mode" not in plain.with_context
    assert "read aloud" in spoken.with_context and "language and script the user spoke" in spoken.with_context
    assert spoken.text == "what's the time"  # the chat shows what was said, not the instruction


def test_dashboard_has_voice_mode_with_barge_in_wake_word_and_tri() -> None:
    js = (STATIC / "voice.js").read_text(encoding="utf-8")
    assert "speechSynthesis.cancel()" in js and "BARGE" in js  # talk over Delta and it stops
    assert "Hey Delta" in js and "WAKE" in js
    assert 'pet("talking")' in js and 'pet("listening")' in js
    assert "echoCancellation: true" in js
    assert "spoken: !!D.voiceTurn" in (STATIC / "chat.js").read_text(encoding="utf-8")
    assert '<script src="/ui/voice.js"></script>' in (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert '.tri[data-mood="talking"]' in css and '.tri[data-mood="listening"]' in css
