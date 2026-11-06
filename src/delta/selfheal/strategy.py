"""Strategy ladder: when one way of doing a UI action keeps failing, step down to the next way.

    page structure (DOM element numbers) -> Windows UI Automation -> screen vision -> ask the user

Data-driven: FAMILIES lists each tool family's rungs in order, TOOL_RUNGS says which rung each tool works
on, and STEP_DOWN says how one action is expressed on a lower rung (a deterministic translation, no AI).
The vision rung is used only when a model that can see the screen is configured; without one it is
skipped. Which rung finally worked is recorded in the task's event log.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DOM, UIA, VISION, ASK = "dom", "uia", "vision", "ask_user"

RUNG_WORDS = {DOM: "the page structure", UIA: "Windows UI Automation", VISION: "the screen picture", ASK: "you"}

FAMILIES: dict[str, tuple[str, ...]] = {
    "web": (DOM, UIA, VISION, ASK),
    "desktop": (UIA, VISION, ASK),
}

# tool -> (family, rung). The "your browser" tools (delta.tools.user_browser) share names with the Delta
# browser's but work through UI Automation: `rung_of` looks at the tool object to tell them apart.
TOOL_RUNGS: dict[str, tuple[str, str]] = {
    "browser_click": ("web", DOM), "browser_fill": ("web", DOM), "browser_press": ("web", DOM),
    "click_element": ("desktop", UIA), "set_field": ("desktop", UIA),
    "click_at": ("desktop", VISION),
}


@dataclass(frozen=True)
class Translated:
    tool: str
    args: dict[str, Any]


def _browser_title(ctx: dict[str, Any]) -> str:
    return str(ctx.get("title") or "")


def _click_to_uia(args: dict[str, Any], ctx: dict[str, Any]) -> Translated | None:
    label = str(ctx.get("label") or args.get("text") or "").strip()
    if not label:
        return None
    out: dict[str, Any] = {"name": label}
    if title := _browser_title(ctx):
        out["window"] = title
    return Translated("click_element", out)


def _fill_to_uia(args: dict[str, Any], ctx: dict[str, Any]) -> Translated | None:
    if args.get("submit"):
        return None  # submitting is a separate decision: never folded into a step-down
    label = str(ctx.get("label") or args.get("field") or "").strip()
    if not label or "value" not in args:
        return None
    out: dict[str, Any] = {"name": label, "text": str(args["value"])}
    if title := _browser_title(ctx):
        out["window"] = title
    return Translated("set_field", out)


# (tool, target rung) -> translation. A missing entry means the rung is reached by a hint to the model
# (vision: look_at_screen then click_at) or by asking the user.
STEP_DOWN: dict[tuple[str, str], Callable[[dict[str, Any], dict[str, Any]], Translated | None]] = {
    ("browser_click", UIA): _click_to_uia,
    ("browser_fill", UIA): _fill_to_uia,
}


def rung_of(name: str, tool: Any = None) -> tuple[str, str] | None:
    found = TOOL_RUNGS.get(name)
    if found is None:
        return None
    module = str(getattr(type(tool), "__module__", "")) if tool is not None else ""
    if found[0] == "web" and module.endswith("user_browser"):
        return "web", UIA
    return found


def next_rung(family: str, rung: str, can_see: bool) -> str | None:
    rungs = FAMILIES.get(family, ())
    if rung not in rungs:
        return None
    for candidate in rungs[rungs.index(rung) + 1:]:
        if candidate == VISION and not can_see:
            continue
        return candidate
    return None


def target_of(name: str, args: dict[str, Any], label: str = "") -> str:
    """What the action is aimed at, for counting failures per target."""
    text = label or str(args.get("text") or args.get("field") or args.get("name") or args.get("key") or "")
    if not text and args.get("ref") is not None:
        text = f"ref {args['ref']}"
    return f"{name}:{text.strip().lower()[:80]}"


def vision_hint(label: str) -> str:
    return (f"Step down to the screen picture: use look_at_screen to find '{label}', then click_at its position. "
            "Re-check with look_at_screen afterwards.")
