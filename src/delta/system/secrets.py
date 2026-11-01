"""Secret storage: Windows Credential Manager via `keyring`, with DELTA_<NAME> env override.

Keys never touch config files or logs.
"""

from __future__ import annotations

import contextlib
import os

SERVICE = "delta-agent"

KNOWN_SECRETS = {
    "groq_api_key": "Groq API key",
    "nvidia_api_key": "NVIDIA NIM API key",
    "tavily_api_key": "Tavily search API key",
    "serpapi_api_key": "SerpAPI key",
    "telegram_bot_token": "Telegram bot token (BotFather)",
    "slack_bot_token": "Slack bot token (xoxb-...)",
    "slack_app_token": "Slack app-level token for Socket Mode (xapp-...)",
    "vapi_api_key": "Vapi API key",
}


def get_secret(name: str) -> str | None:
    env = os.environ.get(f"DELTA_{name.upper()}")
    if env:
        return env
    import keyring  # ~0.2 s to import, so only when a secret is actually needed
    from keyring.errors import KeyringError

    try:
        return keyring.get_password(SERVICE, name)
    except KeyringError:
        return None


def set_secret(name: str, value: str) -> None:
    import keyring

    keyring.set_password(SERVICE, name, value)


def delete_secret(name: str) -> None:
    import keyring
    from keyring.errors import KeyringError

    with contextlib.suppress(KeyringError):
        keyring.delete_password(SERVICE, name)


def mask(value: str | None) -> str:
    if not value:
        return "(not set)"
    return value[:4] + "…" + value[-2:] if len(value) > 8 else "****"
