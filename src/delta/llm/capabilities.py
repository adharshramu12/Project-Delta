"""What each brain is good at, so the agent can tell the model about itself and size its work.

A frontier model (Claude Code, Codex) can carry a long multi-step task and decide when to look at
the screen; a small local model does better with fewer steps and plainer instructions.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Brain:
    label: str
    tier: str           # "frontier" | "strong" | "small"
    step_factor: float  # multiplies agent.max_steps


BRAINS = {
    "claude_code": Brain("Claude Code (your Claude plan)", "frontier", 2.0),
    "codex": Brain("Codex (your ChatGPT plan)", "frontier", 2.0),
    "groq": Brain("Groq cloud model", "strong", 1.3),
    "nim": Brain("NVIDIA NIM cloud model", "strong", 1.3),
    "nebius": Brain("Nebius Token Factory cloud model", "strong", 1.3),
    "gemini": Brain("Google Gemini cloud model", "strong", 1.3),
    "openrouter": Brain("an OpenRouter cloud model", "strong", 1.0),  # openrouter/free picks a different model
    "ollama": Brain("a small model on this PC (Ollama)", "small", 0.8),
    "lmstudio": Brain("a small model on this PC (LM Studio)", "small", 0.8),
}
UNKNOWN = Brain("an AI model", "strong", 1.0)

STYLE = {
    "frontier": ("You are a frontier model: plan carefully, keep a short numbered checklist in your "
                 "head and tick items off, combine evidence from several observations, and recover "
                 "from surprises (pop-ups, sign-in walls, layout changes) on your own."),
    "strong": "Work step by step; keep each action simple and check its result before the next.",
    "small": ("Keep it simple: one small action at a time, then read the AFTER observation. If "
              "something fails twice, try a different tool or tell the user."),
}


def brain_for(provider: str) -> Brain:
    return BRAINS.get(provider, UNKNOWN)


def abilities_text(provider: str, can_see: bool, steps: int) -> str:
    b = brain_for(provider)
    sight = ("You CAN see the screen: look_at_screen takes a screenshot and describes it (also use it "
             "to find click positions for click_at). Pass window='<title>' to see one app even when "
             "the user's other windows cover it." if can_see else
             "You cannot see images right now: rely on read_window (apps) and browser_read (web pages).")
    return (f"YOUR ABILITIES: you are running on {b.label}. {STYLE[b.tier]}\n{sight}\n"
            f"You have up to {steps} actions for this attempt.\n"
            "EVERY mouse/keyboard/browser action returns an 'AFTER:' section showing what the screen or "
            "page looks like right after it. Read it before the next action: it tells you whether the "
            "click worked, a dialog opened, or an error appeared. Never assume an action worked "
            "without seeing it in AFTER.\n"
            "Every screenshot (yours and the AFTER ones) is saved automatically as proof of this run, "
            "and a final screenshot is checked against the success criteria when you reply DONE:.\n"
            "Mouse work: when a screenshot already shows where things are, chain the steps in ONE "
            "do_actions call (e.g. click a menu, wait 0.3, click the item; or scroll at x,y then click) "
            "instead of one call per click.\n"
            "BE FAST: each extra call costs the user time and tokens. When an AFTER section already "
            "shows the result, do not check it again with another tool (no extra list_windows, "
            "read_window or look_at_screen for the same thing): reply DONE: right away; the automatic "
            "final screenshot check catches mistakes.")
