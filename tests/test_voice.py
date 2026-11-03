"""Voice notes: speech to text through Groq Whisper, then done like a typed request."""

from types import SimpleNamespace

import httpx
import pytest

from delta.channels import voice
from delta.channels.voice import VoiceError, transcribe


def fake_groq(monkeypatch, status=200, body=None, seen=None):
    real = httpx.AsyncClient

    def handler(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body or {"text": " send me the daddy image "})

    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(voice, "get_secret", lambda name: "gsk_test" if name == "groq_api_key" else None)


async def test_a_voice_note_becomes_text(monkeypatch):
    seen = []
    fake_groq(monkeypatch, seen=seen)
    assert await transcribe(b"OggS...") == "send me the daddy image"
    assert seen[0].headers["authorization"] == "Bearer gsk_test" and b"whisper-large-v3-turbo" in seen[0].content


async def test_problems_are_explained_in_plain_words(monkeypatch):
    fake_groq(monkeypatch, status=429)
    with pytest.raises(VoiceError, match="busy"):
        await transcribe(b"x")
    monkeypatch.setattr(voice, "get_secret", lambda name: None)
    with pytest.raises(VoiceError, match="Groq key"):
        await transcribe(b"x")


async def test_telegram_voice_note_is_shown_back_then_done(monkeypatch):
    pytest.importorskip("telegram")
    from delta.channels import telegram as tg_mod

    ch = tg_mod.TelegramChannel(SimpleNamespace(audit=SimpleNamespace(record=lambda *a, **k: None)),
                                "123456:TEST-TOKEN", ["5"])
    replies, ran = [], []

    async def fake_transcribe(audio, filename="voice.ogg"):
        assert audio == b"OggS-audio"
        return "send me the daddy image"

    monkeypatch.setattr(voice, "transcribe", fake_transcribe)

    async def run(update, uid, text):
        ran.append((uid, text))

    ch._run = run

    async def get_file():
        async def download():
            return bytearray(b"OggS-audio")
        return SimpleNamespace(download_as_bytearray=download)

    async def reply_text(text, **kw):
        replies.append(text)

    async def typing(action):
        return None

    msg = SimpleNamespace(voice=SimpleNamespace(get_file=get_file), audio=None, reply_text=reply_text,
                          chat=SimpleNamespace(send_action=typing))
    await ch._on_voice(SimpleNamespace(effective_user=SimpleNamespace(id=5), message=msg), None)
    assert replies == ["\U0001f399 I heard: send me the daddy image"] and ran == [("5", "send me the daddy image")]
