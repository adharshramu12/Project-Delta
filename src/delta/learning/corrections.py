"""Turns the user's corrections and standing requests into lasting instructions.

Cheap gate first: only messages that *sound* like a standing instruction ("from now on",
"always", "shorter", "in Telugu", ...) cost an extra model call.
"""

from __future__ import annotations

import re

from delta.learning.store import LearningStore
from delta.llm.providers import extract_json
from delta.llm.router import ModelRouter
from delta.llm.schemas import LLMError

CUES = re.compile(
    r"\b(from now on|going forward|next time|in future|always|never|every time|stop (doing|using|asking)|"
    r"don'?t (ever|use|do|ask|reply|answer)|i (prefer|like|want you to|hate|don'?t like)|"
    r"please (be|keep|use|reply|answer)|"
    r"too (long|short|formal|casual|verbose)|shorter|longer|more (detail|brief|concise)|be (brief|concise)|"
    r"in (telugu|hindi|tamil|english|kannada|malayalam|bengali|marathi|urdu|spanish|french|german)|"
    r"call me|my name is)\b",
    re.I,
)

PROMPT = """The user said something to their personal AI assistant. Extract STANDING INSTRUCTIONS:
things they want the assistant to do or avoid in ALL future replies and tasks (style, language,
length, how to address them, defaults like where to save files, tools/apps to prefer or avoid).

Ignore the one-off task itself. If the message only asks for something once, return an empty list.
Write each instruction as a short imperative the assistant can follow, e.g. "Keep replies to one or
two sentences", "Reply in Telugu", "Call the user Adharsh", "Save new files in Documents".

Reply with JSON only: {"instructions": ["..."]}"""


def sounds_like_instruction(text: str) -> bool:
    return bool(CUES.search(text))


async def learn_corrections(router: ModelRouter, store: LearningStore, user_text: str) -> list[str]:
    """Returns the newly stored instructions (empty if none or not worth a model call)."""
    if not sounds_like_instruction(user_text):
        return []
    try:
        resp = await router.complete([{"role": "system", "content": PROMPT},
                                      {"role": "user", "content": user_text[:2000]}],
                                     task="verify", json_mode=True)
        items = extract_json(resp.content).get("instructions", [])
    except (LLMError, ValueError, AttributeError):
        return []
    added = []
    for raw in items[:3]:
        text = str(raw).strip()
        key = re.sub(r"\W+", " ", text.lower()).strip()
        if not 3 < len(text) < 200 or store.find("preference", key):
            continue  # already known, or undone by the user (never re-learn what they removed)
        store.add("preference", key, text)
        added.append(text)
    return added


def standing_instructions(store: LearningStore) -> str:
    """Prompt block of everything the user taught Delta (preferences, avoid list, lessons)."""
    lines = [f"- {i.content}" for i in store.list(status="active", kind="preference")]
    lines += [f"- {i.content}" for i in store.list(status="active", kind="avoid")]
    lines += [f"- Lesson: {i.content}" for i in store.list(status="active", kind="lesson")[:8]]
    return "\n".join(lines)
