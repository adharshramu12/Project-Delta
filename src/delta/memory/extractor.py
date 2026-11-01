"""Pulls durable facts about the user out of a finished conversation (like ChatGPT memory)."""

from __future__ import annotations

from delta.llm.providers import extract_json
from delta.llm.router import ModelRouter
from delta.llm.schemas import LLMError

PROMPT = """From this exchange, extract durable facts about the USER worth remembering for future
tasks: preferences, tools they use, projects, names, recurring habits. Ignore one-off details,
anything secret (passwords, keys, card numbers, ids) and anything about the assistant.
Reply with JSON only: {"facts": ["...", "..."]} (empty list if nothing is worth keeping)."""


async def extract_facts(router: ModelRouter, user_text: str, answer: str) -> list[str]:
    try:
        resp = await router.complete(
            [{"role": "system", "content": PROMPT},
             {"role": "user", "content": f"USER: {user_text}\nASSISTANT: {answer[:2000]}"}],
            task="verify", json_mode=True)
        facts = extract_json(resp.content).get("facts", [])
    except (LLMError, ValueError, AttributeError):
        return []
    return [f.strip() for f in facts if isinstance(f, str) and 3 < len(f.strip()) < 300][:5]
