"""How Delta writes chat messages for the user: like a person texting, not like an AI.

The rules go to the model, and `humanize()` enforces the mechanical ones in code afterwards, so a
reply never ships with an em dash, markdown, or "I hope this message finds you well" even when the
model slips.
"""

from __future__ import annotations

import re

MOODS = ("friendly", "positive", "funny", "loving", "supportive", "calm", "firm", "angry", "formal")

STYLE_GUIDE = """Write exactly like the user texts this person. You ARE the user here, not an assistant.
- Copy the user's own style from their earlier messages in the chat: language mix (e.g. Telugu/Hindi
  words in English letters), slang, emoji habits, length, capitals or not, punctuation.
- Short and natural, like a real text. One or two lines usually. No greetings unless the user uses them.
- No AI phrases ("certainly", "I hope this finds you well", "feel free", "delve", "as an AI",
  "let me know if you need anything else"), no formal US/UK business English unless the user writes so.
- No lists, no markdown, no long dashes, no quotes around the message, no sign-off.
- Emojis only if the user uses them with this person, and at most one or two.
- Match the mood asked for ({mood}) and the other person's mood; never be rude unless the user asked
  for firm or angry, and even then no insults or threats."""

AI_PHRASES = re.compile(
    r"\b(?:i hope (?:this|you)[^.!?,]*?(?:finds you well|doing well|well|finds you)[.!?,]?|as an ai[^.!?,]*[.!?,]?|"
    r"(?:please )?(?:feel free to|don't hesitate to)[^.!?]*[.!?]?|"
    r"let me know if (?:you need|there(?:'s| is)) anything[^.!?]*[.!?]?|certainly[,!.]?|absolutely[,!.]?(?= )|"
    r"i(?:'d| would) be happy to[^.!?,]*[.!?,]?|delve into)", re.I)


def humanize(text: str, max_chars: int = 700) -> str:
    """Strip what makes a message read as machine-written, keep everything the user would type."""
    t = text.strip().strip('"').strip("'").strip()
    t = re.sub(r"^(?:reply|message|me|you)\s*:\s*", "", t, flags=re.I)
    t = re.sub(r"\s*[—–]\s*", ", ", t)          # em/en dashes -> a comma, as people type
    t = re.sub(r"(\*\*|__|`|#{1,6}\s)", "", t)              # markdown
    t = re.sub(r"^\s*[-*•]\s+", "", t, flags=re.M)     # bullet points
    t = AI_PHRASES.sub("", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\s+([,.!?])", r"\1", t)
    t = re.sub(r"^[,.\s]+", "", t).strip()
    return t[:max_chars].rstrip()


def mood_of(text: str) -> str:
    """The mood the user asked for, if they named one ('reply lovingly', 'be firm with him')."""
    low = text.lower()
    for word, mood in (("lov", "loving"), ("funn", "funny"), ("joke", "funny"), ("angr", "angry"),
                       ("firm", "firm"), ("strict", "firm"), ("formal", "formal"), ("calm", "calm"),
                       ("support", "supportive"), ("positive", "positive"), ("cheer", "positive")):
        if word in low:
            return mood
    return "friendly"
