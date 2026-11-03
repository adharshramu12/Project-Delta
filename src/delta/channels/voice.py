"""Voice notes to text, so a user can just speak to Delta from their phone.

Groq's hosted Whisper (whisper-large-v3-turbo): free tier, about a second per note, and it handles
Telugu, Hindi, English and mixes of them. Uses the Groq key the user already gave Delta."""

from __future__ import annotations

import httpx

from delta.system.secrets import get_secret

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MODEL = "whisper-large-v3-turbo"
TIMEOUT_S = 60
MAX_BYTES = 25_000_000  # Groq's upload limit (about 25 minutes of a Telegram voice note)


class VoiceError(RuntimeError):
    """Shown to the user as is: what went wrong and what to do."""


async def transcribe(audio: bytes, filename: str = "voice.ogg") -> str:
    key = get_secret("groq_api_key")
    if not key:
        raise VoiceError("To understand voice notes I need a free Groq key: on your PC open Delta > Setup > "
                         "AI models and add it (console.groq.com). Typing works meanwhile.")
    if len(audio) > MAX_BYTES:
        raise VoiceError("That voice note is too long for me (over ~25 minutes). Please send a shorter one.")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            r = await client.post(GROQ_URL, headers={"Authorization": f"Bearer {key}"},
                                  data={"model": MODEL, "response_format": "json", "temperature": "0"},
                                  files={"file": (filename, audio, "audio/ogg")})
    except httpx.HTTPError as e:
        raise VoiceError(f"I couldn't reach the speech service ({type(e).__name__}). Try again, or type it.") from e
    if r.status_code == 429:
        raise VoiceError("The free speech service is busy right now. Try again in a minute, or type it.")
    if r.status_code >= 400:
        raise VoiceError(f"The speech service refused the note (HTTP {r.status_code}). Try again, or type it.")
    text = str(r.json().get("text", "")).strip()
    if not text:
        raise VoiceError("I couldn't hear any words in that note. Try again a little closer to the mic.")
    return text
