"""Secret redaction before anything is stored in memory or learned.

Memory is encrypted at rest (Windows DPAPI), but a secret that never reaches the file cannot leak from
it, cannot be recalled into a prompt and cannot be sent anywhere by mistake. So before a memory, a lesson or
a recipe is stored, every secret-looking value is replaced, not masked: unlike the event log (which keeps
the first and last few characters so a person can tell keys apart), memory keeps nothing of it.

Covered: API keys and tokens (the event log's patterns: sk-, ghp_, xoxb-, Telegram bot tokens, JWTs, private
key blocks, bearer tokens, secrets in URLs), card numbers that pass the Luhn check, and values said to be a
password, PIN, OTP, CVV or similar ("my wifi password is hunter22", "pin: 4321").
"""

from __future__ import annotations

import re

from delta.engine.redact import TOKEN_PATTERNS

REMOVED = "[secret removed]"

_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
# "<secret word> is|was|=|: <value>" ; the value is one token (no spaces), at least 3 characters.
_SAID_SECRET = re.compile(
    r"(?i)\b(pass(?:word|code|phrase)?|pwd|pin|otp|one[- ]time (?:code|password)|cvv|cvc|security code|"
    r"api[ _-]?key|secret(?: key)?|token|private key|recovery code|seed phrase)"
    r"(\s*(?:is|was|=|:|-)\s*|\s+)(['\"]?)([^\s'\"]{3,})\3")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _card(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return REMOVED if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)


def _said(m: re.Match[str]) -> str:
    value = m.group(4)
    if value.lower() in {"is", "was", "the", "a", "an", "my", "your", "saved", "set", "reset", "changed", "manager"}:
        return m.group(0)
    return f"{m.group(1)}{m.group(2)}{REMOVED}"


def redact_secrets(text: str) -> tuple[str, int]:
    """(text with every secret replaced, how many were replaced)."""
    out = str(text or "")
    start = out.count(REMOVED)
    for pat in TOKEN_PATTERNS:
        out = pat.sub(REMOVED, out)
    out = _CARD.sub(_card, out)
    out = _SAID_SECRET.sub(_said, out)
    return out, out.count(REMOVED) - start


def only_secret(text: str) -> bool:
    """Nothing worth keeping is left once the secrets are gone (e.g. the whole fact was a key)."""
    rest = re.sub(r"\W+", " ", text.replace(REMOVED, " ")).strip().split()
    meaningful = [w for w in rest if len(w) > 2 and w.lower() not in {"the", "and", "user", "users", "for", "his",
                                                                      "her", "their", "key", "code", "is", "my"}]
    return REMOVED in text and len(meaningful) < 2
