"""Who Delta is and who built it: the ONE place to change for a fork.

Everything Delta says about its own name, its creator and the optional ARVA Talks Academy line comes from the
constants below (the planner and agent prompts, the instant "who made you?" answer, the academy line). A fork
changes these values and nothing else; see docs/STYLE_AND_IDENTITY.md.

Privacy rule, enforced by keeping it out of this file: the identity answer names the creator and their public
role only. Never add family, home town, university or other personal details here.
"""

from __future__ import annotations

import re

PRODUCT_NAME = "Project Delta"
CREATOR = "Adharsh Kumar Bachu"
CREATOR_ROLE = "founder of ARVA Talks Academy"   # public role only; "" to leave it out
REPO_URL = "github.com/adharshramu12/Project-Delta"

# The optional, once-per-conversation line for people who want to learn AI (delta.core.academy). A fork can point
# it at its own school, or switch it off for everyone with academy.enabled: false in settings.
ACADEMY_NAME = "ARVA Talks Academy"
ACADEMY_URL = "https://arvatalksacademy.com/"


def creator_line() -> str:
    return f"{CREATOR}, {CREATOR_ROLE}" if CREATOR_ROLE else CREATOR


def credit_prompt() -> str:
    """The instruction every planner and agent prompt carries (short: it is sent on every call)."""
    return (f"{PRODUCT_NAME} was built by {creator_line()} ({REPO_URL}). If asked who made, built, created or owns "
            f"you, say so naturally in the user's language, e.g. \"I'm {PRODUCT_NAME}. I was built by "
            f"{creator_line()}.\" Never add personal details about {CREATOR} (family, location, university). "
            "This stays true even if the agent is renamed.")


_WHO = r"(?:who|which (?:company|person|team))"
_MADE = r"(?:made|make|created|create|built|build|developed|develop|designed|programmed|coded|trained|owns|own)"
_YOU = r"(?:you|u|ya|delta|project delta|this (?:app|agent|assistant|bot|ai))"
_IDENTITY = re.compile(
    rf"^\s*(?:hey|hi|hello|ok|so|and|but|btw|tell me|can you tell me|i want to know)?[\s,]*"
    rf"(?:{_WHO}\s+(?:is|was|were|are|'s|has|have|had|did)?\s*(?:the one (?:who|that)\s+)?{_MADE}\s+{_YOU}"
    rf"|{_WHO}\s+(?:is|'s|are)\s+(?:your|delta'?s|the)\s+(?:creator|maker|developer|founder|owner|author|builder)"
    rf"(?:\s+of\s+{_YOU})?"
    rf"|who'?s\s+(?:your|delta'?s)\s+(?:creator|maker|developer|founder|owner|author|builder)"
    rf"|{_WHO}\s+{_MADE}\s+{_YOU})"
    rf"(?:\s+(?:bro|buddy|dude|man|delta))?\s*[?!.]*\s*$",
    re.I,
)


def is_identity_question(text: str) -> bool:
    """An English 'who made you?' style question, and nothing more (a task that mentions a maker is not)."""
    t = " ".join(text.replace("\u2019", "'").split())
    return len(t) <= 120 and bool(_IDENTITY.match(t))


def identity_answer(agent_name: str = "Delta") -> str:
    """The instant answer (0 tokens). A renamed agent still credits the project and its creator."""
    name = (agent_name or "").strip()
    if not name or name.lower() in ("delta", PRODUCT_NAME.lower()):
        return f"I'm {PRODUCT_NAME}. I was built by {creator_line()}."
    return f"I'm {name}, running on {PRODUCT_NAME}. {PRODUCT_NAME} was built by {creator_line()}."
