"""Per-task token metering: what each model call reported, or an estimate when it reported nothing.

The router adds every call to a plain dict of counters (ModelRouter.meter):
  calls            every successful model call
  input / cached / output   tokens the provider reported
  reported_calls   calls that reported usage
  est_input / est_output    estimated tokens (about 4 characters per token), for API providers
                   that answered without a usage block
  estimated_calls  calls counted by estimate
  unreported_calls CLI providers (Claude Code, Codex, Cline) that reported nothing: shown as
                   "not reported", because Delta cannot see the prompt the CLI really sent
  unreported_est   a rough size of those calls, used only by the optional token budget

summary() turns the counters into what the dashboard and the API show.
"""

from __future__ import annotations

import json
from typing import Any

CHARS_PER_TOKEN = 4
IMAGE_TOKENS = 1000  # a step screenshot at ~1024 px wide


def estimate_tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> int:
    """Rough input size of one request: text characters / 4, plus a flat cost per image."""
    chars, images = 0, 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image_url" or "image_url" in part:
                    images += 1
                else:
                    chars += len(str(part.get("text", "")))
        else:
            chars += len(str(content))
        if m.get("tool_calls"):
            chars += len(json.dumps(m["tool_calls"]))
    if tools:
        chars += len(json.dumps(tools))
    return chars // CHARS_PER_TOKEN + images * IMAGE_TOKENS


def estimate_output(content: str, tool_calls: list[Any]) -> int:
    chars = len(content or "")
    for c in tool_calls or []:
        chars += len(getattr(c, "name", "")) + len(json.dumps(getattr(c, "arguments", {}) or {}, default=str))
    return chars // CHARS_PER_TOKEN


def add(meter: dict[str, int], key: str, value: int) -> None:
    meter[key] = meter.get(key, 0) + int(value)


def used_for_budget(meter: dict[str, int]) -> int:
    """Tokens this task has used, counting estimates (and a rough size for unreported CLI calls)."""
    return (meter.get("input", 0) + meter.get("output", 0) + meter.get("est_input", 0)
            + meter.get("est_output", 0) + meter.get("unreported_est", 0))


def summary(meter: dict[str, int]) -> dict[str, Any]:
    """{"input", "output", "cached", "calls", "source", "label"} for one task.

    source: "none" (no AI call), "reported", "estimate", "mixed" (some reported, some estimated or
    not reported) or "not reported" (only CLI providers that report nothing)."""
    calls = meter.get("calls", 0)
    est = meter.get("estimated_calls", 0)
    unreported = meter.get("unreported_calls", 0)
    reported = max(0, calls - est - unreported)
    inp = meter.get("input", 0) + meter.get("est_input", 0)
    out = meter.get("output", 0) + meter.get("est_output", 0)
    if calls == 0:
        source = "none"
    elif unreported >= calls:
        source = "not reported"
    elif est == 0 and unreported == 0:
        source = "reported"
    elif reported == 0 and unreported == 0:
        source = "estimate"
    else:
        source = "mixed"
    if source == "none":
        label = "no AI calls"
    elif source == "not reported":
        label = "tokens not reported by this AI"
    else:
        label = f"{inp + out:,} tokens"
        if source == "estimate":
            label += " (estimate)"
        elif source == "mixed":
            parts = []
            if est:
                parts.append(f"{est} call(s) estimated")
            if unreported:
                parts.append(f"{unreported} not reported")
            label += f" ({', '.join(parts)})"
    return {"input": inp, "output": out, "cached": meter.get("cached", 0), "calls": calls,
            "source": source, "label": label}
