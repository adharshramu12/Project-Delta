"""Pulls durable facts about the user out of a finished conversation (like ChatGPT memory)."""

from __future__ import annotations

import contextlib
import re

from delta.llm.providers import extract_json
from delta.llm.router import ModelRouter
from delta.llm.schemas import LLMError

PROMPT = """Decide if this exchange reveals a LASTING fact about the user that will still be true and
useful next month: a stated preference ("I prefer short answers"), a tool or app they regularly use,
an ongoing project, their name or role, a recurring habit.

Do NOT record:
- the task itself or what the user asked for this time ("user wants to create a file X")
- anything one-off, temporary, guessed or implied
- anything about the assistant, Delta, or its tools/folders
- secrets: passwords, keys, tokens, card or ID numbers

Most exchanges contain nothing worth keeping; then return an empty list.
Reply with JSON only: {"facts": ["<short fact, starting with 'User'>"]}"""

# Phrases that mark a one-off request rather than a lasting fact.
ONE_OFF = re.compile(r"\b(wants to|asked|requested|is asking|would like to|needs to|tried to|just)\b", re.I)
ABOUT_DELTA = re.compile(r"\b(delta|assistant|agent|save folder)\b", re.I)


async def extract_facts(router: ModelRouter, user_text: str, answer: str) -> list[str]:
    try:
        resp = await router.complete(
            [{"role": "system", "content": PROMPT},
             {"role": "user", "content": f"USER: {user_text}\nASSISTANT: {answer[:2000]}"}],
            task="verify", json_mode=True)
        facts = extract_json(resp.content).get("facts", [])
    except (LLMError, ValueError, AttributeError):
        return []
    return [f.strip() for f in facts
            if isinstance(f, str) and 3 < len(f.strip()) < 300
            and not ONE_OFF.search(f) and not ABOUT_DELTA.search(f)
            and not _guessed_from_system(f, user_text)][:3]


def _guessed_from_system(fact: str, user_text: str) -> bool:
    """Models love to 'learn' the user's name from C:\\Users\\<name> paths in tool output.
    Only keep a fact mentioning the Windows account name if the user said it themselves."""
    import getpass

    with contextlib.suppress(Exception):
        account = getpass.getuser().lower()
        if len(account) >= 3 and account in fact.lower() and account not in user_text.lower():
            return True
    return bool(re.search(r"\busername\b", fact, re.I))
