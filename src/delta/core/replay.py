"""Skill replay: run a proven skill's recorded tool calls without the AI driving each step.

A recipe is the exact tool calls that worked in a verified run, with request-specific values as
{slots}: [{"tool": "open_app", "args": {"name": "notepad"}}, {"tool": "type_text", "args":
{"text": "{text}"}}]. Replaying it costs one small call to fill the slots (and confirm the request
really fits), instead of a planner call plus an agent loop of 10-20 calls.

Recipes are validated in code, never trusted as written by a model: only calls that succeeded in
the run, only declared slots, and nothing aimed at screen coordinates (positions change between
runs, so those skills stay agent-driven).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from delta.core.state import StepRecord
from delta.llm.providers import extract_json
from delta.llm.router import ModelRouter
from delta.llm.schemas import LLMError

# Aimed by pixels or looking at the screen: not repeatable as recorded.
NOT_REPLAYABLE = frozenset({"click_at", "do_actions", "look_at_screen", "screenshot", "scroll", "look_with_marks",
                            "click_mark", "read_screen_text", "click_text", "parallel_research"})
# Reading steps add nothing when the calls are already known.
OBSERVERS = frozenset({"read_window", "list_windows", "browser_read", "path_info", "list_dir", "read_file",
                       "media_status", "recall"})
SLOT = re.compile(r"\{([a-z][a-z0-9_]{0,30})\}")
MAX_STEPS = 12

FIT_PROMPT = """A saved skill may handle this request. Decide if it truly fits, and fill its slots
from the request text only (never invent values; if a slot's value is not in the request, it does
not fit).

SKILL: {name} - {when}
SLOTS: {slots}

Reply with JSON only: {{"fits": true|false, "values": {{"<slot>": "<value>", ...}}}}"""


@dataclass
class Recipe:
    steps: list[dict[str, Any]]
    slots: dict[str, str] = field(default_factory=dict)  # slot -> what it holds, e.g. "song name"

    def to_json(self) -> str:
        return json.dumps({"steps": self.steps, "slots": self.slots})

    @staticmethod
    def from_json(raw: str | None) -> Recipe | None:
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return Recipe(list(data["steps"]), dict(data.get("slots", {})))
        except (ValueError, KeyError, TypeError):
            return None


def validate_recipe(raw: Any, steps: list[StepRecord]) -> Recipe | None:
    """A model-written recipe, checked against what really happened. None if unsafe or unusable."""
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
        return None
    slots = {str(k): str(v) for k, v in (raw.get("slots") or {}).items() if SLOT.fullmatch("{" + str(k) + "}")}
    done = [s for s in steps if s.ok and s.tool not in OBSERVERS]
    if any(s.tool in NOT_REPLAYABLE for s in done):
        return None  # the run depended on screen positions
    ran = [s.tool for s in done]
    out: list[dict[str, Any]] = []
    cursor = 0
    for step in raw["steps"][:MAX_STEPS]:
        if not isinstance(step, dict) or not isinstance(step.get("args", {}), dict):
            return None
        tool = step.get("tool")
        if not isinstance(tool, str):
            return None
        if tool in OBSERVERS:
            continue
        # Same tools, same order as the verified run: a recipe can't add a call that never happened.
        try:
            cursor = ran.index(tool, cursor) + 1
        except ValueError:
            return None
        used = set(SLOT.findall(json.dumps(step.get("args", {}))))
        if not used <= slots.keys():
            return None
        out.append({"tool": tool, "args": step.get("args", {})})
    return Recipe(out, slots) if out else None


def fill(value: Any, values: dict[str, str]) -> Any:
    """Put slot values into a step's arguments (strings, lists and nested dicts)."""
    if isinstance(value, str):
        return SLOT.sub(lambda m: values[m.group(1)], value)
    if isinstance(value, list):
        return [fill(v, values) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, values) for k, v in value.items()}
    return value


async def match_slots(router: ModelRouter, request: str, name: str, when: str,
                      recipe: Recipe) -> dict[str, str] | None:
    """One small call: does the request fit the skill, and what are the slot values?"""
    if not recipe.slots:
        return {}
    prompt = FIT_PROMPT.format(name=name, when=when,
                               slots=", ".join(f"{k} ({v})" for k, v in recipe.slots.items()))
    try:
        resp = await router.complete([{"role": "system", "content": prompt},
                                      {"role": "user", "content": request[:500]}],
                                     task="verify", json_mode=True)
        data = extract_json(resp.content)
    except (LLMError, ValueError):
        return None
    values = data.get("values") if isinstance(data, dict) else None
    if not data.get("fits") or not isinstance(values, dict):
        return None
    clean = {k: str(values.get(k, "")).strip() for k in recipe.slots}
    if any(not v for v in clean.values()):
        return None
    # The value must really come from the request, not be made up.
    low = request.lower()
    if any(not all(w in low for w in re.findall(r"\w{3,}", v.lower())[:4]) for v in clean.values()):
        return None
    return clean
