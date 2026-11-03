"""Clarifying questions with tap answers, like a good assistant asks before a big task.

The agent writes them as plain text (works with every model, even small free ones):

    NEED_USER: A few details so the research is exactly what you need.
    Which areas should it cover?
    - [ ] Market size
    - [ ] Competitors
    How deep?
    - Quick overview
    - Detailed report

A line ending in "?" starts a question; "- [ ] x" choices are tick boxes (pick any), "- x" choices are
pick-one. Channels show them as buttons; the answers go back as one message the agent understands."""

from __future__ import annotations

import re
from typing import Any

MAX_QUESTIONS = 4
MAX_CHOICES = 10
_CHOICE = re.compile(r"\s*(?:[-*•]|\d+[.)])\s+(\[[ xX]?\]\s*)?(.+)")


def parse_questions(text: str) -> tuple[str, list[dict[str, Any]]]:
    """(intro text, [{"q": question, "options": [...], "multi": bool}, ...]). Questions with fewer than two
    choices stay in the text; with no question lines, bullets after the intro form one question."""
    intro: list[str] = []
    questions: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        m = _CHOICE.match(line)
        if m:
            if current is None:
                current = {"q": "", "options": [], "multi": False}
                questions.append(current)
            if len(current["options"]) < MAX_CHOICES:
                current["options"].append(m.group(2).strip().strip("`*")[:120])
                current["multi"] = current["multi"] or bool(m.group(1))
            continue
        stripped = line.strip()
        if stripped.endswith("?") and len(questions) < MAX_QUESTIONS:
            current = {"q": stripped, "options": [], "multi": False}
            questions.append(current)
        elif stripped:
            intro.append(stripped)
            if current is not None and current["options"]:
                current = None  # text after a question's choices ends that question
    kept = [q for q in questions if len(q["options"]) >= 2]
    for q in questions:
        if q not in kept and q["q"]:
            intro.append(q["q"])
    if kept and not kept[0]["q"]:  # bullets straight after the intro: the intro is their question
        kept[0]["q"] = "\n".join(intro).strip()
        intro = []
    return "\n".join(intro).strip(), kept[:MAX_QUESTIONS]


def answers_text(questions: list[dict[str, Any]], answers: dict[int, list[str]]) -> str:
    """The user's picks as one message: what the agent reads when the task continues."""
    lines = []
    for i, q in enumerate(questions):
        picked = answers.get(i) or []
        lines.append(f"{q['q'] or 'Choice'} -> {', '.join(picked) if picked else '(no preference)'}")
    return "My answers:\n" + "\n".join(lines)
