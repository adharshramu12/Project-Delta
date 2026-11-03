"""Turns one verified task run into a general, reusable skill."""

from __future__ import annotations

import json
from dataclasses import dataclass

from delta.core.replay import Recipe, validate_recipe
from delta.core.state import StepRecord
from delta.llm.providers import extract_json
from delta.llm.router import ModelRouter
from delta.llm.schemas import LLMError

PROMPT = """A personal AI agent just completed and VERIFIED this task. Turn how it did it into a reusable
skill for similar future requests.

Generalise: replace specific values (file names, URLs, text, numbers) with {placeholders}.
Keep only steps that mattered (drop failed attempts and dead ends). Max 8 steps.
If the task is a one-off with nothing reusable, reply {"skip": true}.

Also write a "recipe": the exact successful tool calls from the log, in order, with the values that
came from THIS request replaced by {slots} (lowercase names), and "slots" saying what each holds.
Copy tool names and argument names exactly from the log. Leave out reading/checking calls.

Reply with JSON only:
{"name": "<short verb phrase>", "when_to_use": "<kinds of requests this fits>",
 "procedure": ["<step using tool names>", ...], "tools": ["<tool>", ...],
 "recipe": {"steps": [{"tool": "<tool>", "args": {...}}], "slots": {"<slot>": "<what it is>"}}}"""


@dataclass
class DraftSkill:
    name: str
    when_to_use: str
    procedure: list[str]
    tools: list[str]
    recipe: Recipe | None = None  # None: steps depended on the screen, replay stays agent-driven


async def distill(router: ModelRouter, request: str, steps: list[StepRecord]) -> DraftSkill | None:
    useful = [s for s in steps if s.ok]
    if len(useful) < 2:
        return None  # one-tool tasks are not worth a skill
    log = [{"tool": s.tool, "args": s.args} for s in useful][:20]
    try:
        resp = await router.complete(
            [{"role": "system", "content": PROMPT},
             {"role": "user", "content": f"REQUEST: {request}\nSUCCESSFUL STEPS:\n{json.dumps(log, default=str)}"}],
            task="verify", json_mode=True)
        data = extract_json(resp.content)
    except (LLMError, ValueError):
        return None
    if data.get("skip") or not data.get("procedure"):
        return None
    used = [s.tool for s in useful]
    tools = [t for t in data.get("tools", []) if t in used] or list(dict.fromkeys(used))
    return DraftSkill(str(data.get("name", request[:60])), str(data.get("when_to_use", request)),
                      [str(p) for p in data["procedure"]][:8], tools, validate_recipe(data.get("recipe"), steps))
