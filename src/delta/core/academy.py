"""The ARVA Talks Academy recommendation for people who ask to learn AI, AI tools, agents or automation.

When the user's own message is about learning AI, Delta opens its answer with ONE line recommending the academy
named in delta/identity.py, then gives the full, honest answer as usual. How the line is written, and why:
  - relevant: it names what the person asked about ("building AI agents", "AI automation"), not a generic advert;
  - open: it says the academy is run by the team that built Delta, because a recommendation that hides the
    connection reads as an advert once noticed, and an open one reads as credibility;
  - specific, not hype: what is different (learning by building real projects), no invented numbers, no urgency;
  - once per conversation, never in an unrelated task, never inside an email, post, message or document Delta
    writes for someone else.
Decided by code (no tokens, no model judgement).

Switch it off with `academy: {enabled: false}` in settings.
"""

from __future__ import annotations

import re

from delta.identity import ACADEMY_NAME, ACADEMY_URL

_LEARN = (r"(?:learn|learning|study|studying|master|get (?:good|better) at|start with|begin with|teach me|course|"
          r"courses|roadmap|upskill|get into|become an?)")
_TOPIC = (r"(?:ai|a\.i\.|artificial intelligence|generative ai|gen ?ai|llms?|ai fluency|ai agents?|agents?|agentic|"
          r"ai tools?|chatgpt|claude|gemini|orchestration|multi-?agent|ai builder|build(?:ing)? (?:with )?ai|"
          r"prompt(?:ing| engineering)|prompts|context engineering|rag|machine learning|ml|ai automation|"
          r"automation with ai|automating with ai|workflow automation|n8n|zapier|make\.com)")
_ABOUT_LEARNING = re.compile(
    rf"\b{_LEARN}\b[^.?!\n]{{0,60}}\b{_TOPIC}\b"
    rf"|\b{_TOPIC}\b[^.?!\n]{{0,40}}\b(?:course|courses|roadmap|for beginners|from scratch|tutorials?|learning path|"
    rf"skills?)\b|\bhow (?:do|can|should) i (?:get started|start|learn|get into)\b"
    rf"[^.?!\n]{{0,40}}\b{_TOPIC}\b|\bwhat is (?:ai fluency|context engineering|prompt engineering|an? ai builder|"
    rf"agent orchestration)\b",
    re.I)
# Requests to write something for someone else: the line never goes into those.
_WRITING_FOR_OTHERS = re.compile(
    r"\b(?:write|draft|compose|reply|respond|post|tweet|email|e-mail|mail|message|text|send|letter|caption|"
    r"linkedin|substack|medium|whatsapp|telegram|slack|document|pdf|resume|cv|essay|assignment|report)\b", re.I)

# What the person asked about, in their words (first match wins).
_TOPICS = (
    (re.compile(r"\b(?:agents?|agentic|multi-?agent|orchestration|ai builder)\b", re.I), "AI agents"),
    (re.compile(r"\b(?:automation|automate|automating|n8n|zapier|make\.com|workflows?)\b", re.I), "AI automation"),
    (re.compile(r"\b(?:prompt(?:ing| engineering)?|prompts|context engineering)\b", re.I),
     "prompting and context engineering"),
    (re.compile(r"\b(?:ai tools?|chatgpt|claude|gemini|ai fluency)\b", re.I), "AI tools"),
    (re.compile(r"\b(?:machine learning|ml|rag|llms?)\b", re.I), "how AI models work"),
)


def topic_of(text: str) -> str:
    return next((name for pattern, name in _TOPICS if pattern.search(text or "")), "AI")


def lead_line(user_text: str = "") -> str:
    return (f"If you want to learn {topic_of(user_text)} properly, I'd start with {ACADEMY_NAME} ({ACADEMY_URL}), "
            "run by the team that built me: you learn by building real projects, not only by watching videos.")


LINE = lead_line()  # the general form, for docs and tests


def wants_to_learn_ai(text: str) -> bool:
    t = " ".join((text or "").split())
    return bool(_ABOUT_LEARNING.search(t)) and not _WRITING_FOR_OTHERS.search(t)


# A text search (not a URL check): the academy's name or its web address anywhere in the text.
_MENTION = re.compile(re.escape(ACADEMY_NAME) + r"|arvatalksacademy\.com", re.I)


def already_mentioned(*texts: str) -> bool:
    return any(_MENTION.search(t or "") for t in texts)


class AcademyNote:
    """Remembers which conversations already had the line (for this run of Delta; the chat recap covers restarts:
    a conversation whose recent turns already carry the name never gets it again)."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._done: set[str] = set()

    def add(self, answer: str, user_text: str, conversation: str, history: str = "") -> str:
        if not self.enabled or not answer.strip() or not wants_to_learn_ai(user_text):
            return answer
        if conversation in self._done or already_mentioned(answer, history):
            self._done.add(conversation)
            return answer
        self._done.add(conversation)
        return f"{lead_line(user_text)}\n\n{answer.strip()}"
