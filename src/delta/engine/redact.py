"""What the event log may keep of a tool call's arguments and results.

Secrets never reach tasks.db: values under secret-sounding keys (password, token, api_key, pin, ...)
and anything that looks like a key or token (sk-..., ghp_..., xoxb-..., a Telegram bot token, a JWT,
a private key block) are replaced with a mask from delta.system.secrets.mask, which shows at most the
first 4 and last 2 characters. Long text is cut to a short head. Names of the user's stored secrets
(delta.system.secrets.KNOWN_SECRETS) count as secret keys too.
"""

from __future__ import annotations

import json
import re
from typing import Any

from delta.system.secrets import KNOWN_SECRETS, mask

SECRET_KEY = re.compile(
    r"(pass(word|wd|phrase)?|pwd|secret|token|api[_-]?key|apikey|authorization|^auth$|cookie|"
    r"credential|private[_-]?key|(^|[_-])(pin|otp|iban|ssn)($|[_-])|cvv|cvc|card[_-]?(number|no))", re.I)
TOKEN_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"\b(sk|rk|pk)-(proj-|ant-|live-|test-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(gsk|ghp|gho|ghu|ghs|ghr|xapp|nvapi|tvly|hf)[_-][A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"),  # Telegram bot token
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{12,}"),
    re.compile(r"(?i)(?<=[?&])(access_token|token|key|api_key|apikey|sig|signature|password)=[^&#\s]+"),
]
REDACTED = "[redacted]"


def _is_secret_key(key: str) -> bool:
    return bool(SECRET_KEY.search(key)) or key.lower() in KNOWN_SECRETS


def redact_text(text: str, limit: int = 0) -> str:
    """Tokens in free text masked; cut to `limit` characters when limit > 0."""
    out = str(text)
    for pat in TOKEN_PATTERNS:
        out = pat.sub(lambda m: mask(m.group(0)), out)
    if limit and len(out) > limit:
        out = out[:limit] + f"...[{len(out) - limit} more chars]"
    return out


def redact_value(value: Any, limit: int = 300, depth: int = 0) -> Any:
    if depth > 6:
        return "[nested]"
    if isinstance(value, dict):
        return {str(k): (REDACTED if _is_secret_key(str(k)) and isinstance(v, str) and v
                         else redact_value(v, limit, depth + 1)) for k, v in list(value.items())[:50]}
    if isinstance(value, list | tuple):
        items = [redact_value(v, limit, depth + 1) for v in list(value)[:50]]
        return items + ([f"...[{len(value) - 50} more]"] if len(value) > 50 else [])
    if isinstance(value, str):
        return redact_text(value, limit)
    if value is None or isinstance(value, bool | int | float):
        return value
    return redact_text(str(value), limit)


def redact_args(args: Any, limit: int = 300, max_chars: int = 2000) -> str:
    """JSON of a tool call's arguments, safe to store: secrets masked, each value and the whole capped."""
    try:
        text = json.dumps(redact_value(args if isinstance(args, dict) else {"value": args}, limit),
                          ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = json.dumps({"value": redact_text(repr(args), limit)})
    if len(text) > max_chars:
        text = json.dumps({"truncated": redact_text(text, max_chars)}, ensure_ascii=False)
    return text
