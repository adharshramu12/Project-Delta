"""Recipe auto-repair: when one step of a saved recipe fails during replay, repair just that step.

    replayed step fails -> failure type (delta.selfheal.taxonomy)
      repairable (element not found, stale element, timeout, a banner in the way)?
        1. a repaired candidate for this step exists: try its version of the step
        2. otherwise self-heal's playbook (re-read the page and find the element by name, wait and retry once,
           close a harmless banner, step down to another way of doing the same thing)
      the step now works -> the call that worked becomes the repaired step (slot values put back as {slots})
                         -> saved as a recipe CANDIDATE; the skill's working recipe is not touched
                         -> the rest of the recipe continues
The candidate is used for real only after the promotion gate (delta.learning.gate) passes it: N verified runs
or a passing scripted eval. Until then every replay starts from the working recipe.

Safety: every attempt is an ordinary ToolExecutor call (policy, approvals, Remote Guard, ExfilGuard, taint), the
repair never changes what a step does, only how it finds its target, and a repaired step whose values came from
outside content is untrusted (delta.memory.provenance) and never promoted. Steps that change something are
retried only when self-heal's own rules allow it (it found no target and did nothing).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from delta.core.replay import Recipe, fill
from delta.selfheal.taxonomy import Failure

F = Failure
REPAIRABLE = {F.ELEMENT_NOT_FOUND, F.STALE_ELEMENT, F.TIMEOUT, F.POPUP_OR_DIALOG}


def unfill(value: Any, values: dict[str, str]) -> Any:
    """Put {slots} back where this request's slot values appear (longest values first)."""
    if isinstance(value, str):
        out = value
        for slot, v in sorted(values.items(), key=lambda kv: -len(kv[1])):
            if v and len(v) >= 2:
                out = out.replace(v, "{" + slot + "}")
        return out
    if isinstance(value, list):
        return [unfill(v, values) for v in value]
    if isinstance(value, dict):
        return {k: unfill(v, values) for k, v in value.items()}
    return value


def repaired(recipe: Recipe, index: int, tool: str, args: dict[str, Any], values: dict[str, str]) -> Recipe:
    """The recipe with step `index` replaced by the call that worked (slot values turned back into slots)."""
    steps = [dict(s) for s in recipe.steps]
    steps[index] = {"tool": tool, "args": unfill(dict(args), values)}
    return Recipe(steps, dict(recipe.slots))


def describe_change(old: dict[str, Any], tool: str, args: dict[str, Any]) -> str:
    """Plain words for what changed: 'browser_click by ref -> by text', 'click_element -> click_text'."""
    if old.get("tool") != tool:
        return f"{old.get('tool')} -> {tool}"
    before = ", ".join(sorted(k for k in old.get("args", {}) if k not in args)) or "same"
    after = ", ".join(sorted(k for k in args if k not in old.get("args", {}))) or "same"
    if before == after == "same":
        return f"{tool}: same call worked on a second try"
    return f"{tool}: {before} -> {after}"


@dataclass
class StepAttempt:
    tool: str
    args: dict[str, Any]
    ok: bool
    candidate_id: int | None = None  # the existing candidate whose step was used


def candidate_step(candidates: list[Any], index: int, values: dict[str, str]) -> tuple[int, str, dict[str, Any]] | None:
    """(candidate id, tool, filled args) of the newest open candidate that repaired step `index`."""
    for c in candidates:
        if c.step != index or c.status != "candidate":
            continue
        rec = Recipe.from_json(c.recipe)
        if rec is None or index >= len(rec.steps):
            continue
        step = rec.steps[index]
        try:
            return c.id, str(step["tool"]), fill(step.get("args", {}), values)
        except KeyError:
            continue  # a slot this request does not fill
    return None


def same_recipe(a: str | None, b: str | None) -> bool:
    try:
        return json.loads(a or "null") == json.loads(b or "null")
    except ValueError:
        return a == b
