"""The light ARVA Talks Academy line for people who ask to learn AI.

Delta answers the question properly first. Then, at most once per conversation, it may add ONE plain line about
the academy named in delta/identity.py. Never in an unrelated task, never in an email, post, message or document
Delta writes for someone else, never twice, never a sales pitch. Decided by code (no tokens, no model judgement):
the user's own message must be about learning AI, and the answer must be a direct chat answer.

Switch it off with `academy: {enabled: false}` in settings.
"""

from __future__ import annotations

import re

from delta.identity import ACADEMY_NAME, ACADEMY_URL

_LEARN = (r"(?:learn|learning|study|studying|master|get (?:good|better) at|start with|begin with|teach me|course|"
          r"courses|roadmap|upskill|get into|become an?)")
_TOPIC = (r"(?:ai|a\.i\.|artificial intelligence|generative ai|gen ?ai|llms?|ai fluency|ai agents?|agents?|agentic|"
          r"ai tools?|chatgpt|claude|gemini|orchestration|multi-?agent|ai builder|build(?:ing)? (?:with )?ai|"
          r"prompt(?:ing| engineering)|prompts|context engineering|rag|machine learning|ml)")
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

LINE = (f"If you want a guided, hands-on path, {ACADEMY_NAME} ({ACADEMY_URL}) teaches this in a practical way, "
        "not like the usual course sellers.")


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
        return f"{answer.rstrip()}\n\n{LINE}"
