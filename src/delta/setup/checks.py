"""Checks used by the first-run setup page: what this PC can run, and whether a key works.

Keys are validated against the real service before they are saved, so a typo is caught at
once instead of surfacing later as a confusing task failure. Keys are never logged or echoed.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import asdict, dataclass
from typing import Any

import psutil

# name -> (what it is, how to validate it). Validation returns (ok, human message).
KEY_INFO = {
    "groq_api_key": "Groq (free, fast cloud models)",
    "nvidia_api_key": "NVIDIA NIM (free cloud models)",
    "nebius_api_key": "Nebius Token Factory (your Nebius credits)",
    "gemini_api_key": "Google Gemini (free cloud key)",
    "openrouter_api_key": "OpenRouter (free cloud key)",
    "tavily_api_key": "Tavily web search",
    "serpapi_api_key": "SerpAPI web search",
    "telegram_bot_token": "Telegram bot",
}


@dataclass
class Hardware:
    ram_gb: float
    ram_free_gb: float
    gpu: str | None
    vram_gb: float | None
    ollama_running: bool
    recommendation: str
    can_run_local: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_hardware(ollama_running: bool) -> Hardware:
    mem = psutil.virtual_memory()
    gpu, vram = None, None
    with contextlib.suppress(Exception):
        import pynvml

        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(h)
        gpu = name.decode() if isinstance(name, bytes) else str(name)
        vram = round(pynvml.nvmlDeviceGetMemoryInfo(h).total / 1e9, 1)
        pynvml.nvmlShutdown()
    # A ~4B model at 4-bit needs ~3.5 GB VRAM (or runs slowly on CPU with 8+ GB RAM).
    can_local = (vram or 0) >= 4 or mem.total / 1e9 >= 16
    if (vram or 0) >= 4:
        rec = (f"Your {gpu} ({vram} GB) can run a small local model privately and for free. "
               "Add a free cloud key too, as a backup for harder tasks.")
    elif mem.total / 1e9 >= 16:
        rec = "No suitable GPU found; local models would run slowly on the CPU. A free cloud key is recommended."
    else:
        rec = "This PC is best served by a free cloud model (Groq). Local models would make it slow."
    return Hardware(round(mem.total / 1e9, 1), round(mem.available / 1e9, 1), gpu, vram, ollama_running, rec,
                    can_local)


TELEGRAM_TOKEN = re.compile(r"[0-9]{3,20}:[A-Za-z0-9_-]{20,100}")


async def validate_key(name: str, value: str) -> tuple[bool, str]:
    """Check a key with a harmless read-only call to its service."""
    import httpx

    value = value.strip()
    if not value:
        return False, "The key is empty."
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            if name == "groq_api_key":
                r = await c.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {value}"})
            elif name == "nvidia_api_key":
                r = await c.get("https://integrate.api.nvidia.com/v1/models",
                                headers={"Authorization": f"Bearer {value}"})
            elif name == "nebius_api_key":
                r = await c.get("https://api.tokenfactory.nebius.com/v1/models",
                                headers={"Authorization": f"Bearer {value}"})
            elif name == "gemini_api_key":
                # Google answers 400 (not 401) for a key it does not know, so the body is read below.
                r = await c.get("https://generativelanguage.googleapis.com/v1beta/openai/models",
                                headers={"Authorization": f"Bearer {value}"})
                if r.status_code == 400 and "api key" in r.text.lower():
                    return False, "The service rejected this key. Check it was copied completely."
            elif name == "openrouter_api_key":
                # /models is public, so it proves nothing about the key; /key answers 401 for a key it does not know.
                r = await c.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {value}"})
            elif name == "telegram_bot_token":
                # The token becomes part of the address: only the exact shape BotFather gives (digits, a colon,
                # letters, digits, - and _) is sent, so nothing typed here can change where the request goes.
                if not TELEGRAM_TOKEN.fullmatch(value):
                    return False, ("That does not look like a bot token. It looks like 123456789:ABC-def_..., "
                                   "copied from BotFather's message.")
                r = await c.get(f"https://api.telegram.org/bot{value}/getMe")
                if r.status_code == 200 and r.json().get("ok"):
                    return True, f"Connected to bot @{r.json()['result'].get('username', '?')}."
            elif name == "tavily_api_key":
                r = await c.post("https://api.tavily.com/search", json={"api_key": value, "query": "test",
                                                                        "max_results": 1})
            elif name == "serpapi_api_key":
                r = await c.get("https://serpapi.com/account.json", params={"api_key": value})
            else:
                return False, "Unknown key."
    except httpx.HTTPError:
        return False, "Could not reach the service. Check your internet connection and try again."
    if r.status_code in (401, 403):
        return False, "The service rejected this key. Check it was copied completely."
    if r.status_code >= 400:
        return False, f"The service answered with an error ({r.status_code}). Try again in a minute."
    return True, "Key works."
