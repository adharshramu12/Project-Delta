"""The words of a request that say what it is about, for matching skills and memories.

Every word of 3+ letters used to count, so "did you complete the previous talk buddy" matched every
skill and past task through "the", "did" and "you", and each of them rode along in the prompt."""

from __future__ import annotations

import re

STOPWORDS = frozenset("""
the and for are was were you your yours our ours his her hers its their them they this that these those
with from into onto about over under than then there here what when where which who whom whose why how
can could would should will shall may might must did does doing done has have had having been being
not but all any some just also very too more most much many such only own same other again once
please pls now today yes yeah okay hey hello thanks thank buddy bro dude man sir madam friend delta
something anything everything thing things want need make get got let tell say said give take put
use using via per one two
""".split())  # noqa: SIM905 - a word list reads better as text


def keywords(text: str, limit: int = 12) -> list[str]:
    """Distinct content words, in order: filler, pronouns and chat words removed."""
    seen: dict[str, None] = {}
    for w in re.findall(r"\w{3,}", text.lower()):
        if w not in STOPWORDS and not w.isdigit():
            seen.setdefault(w, None)
    return list(seen)[:limit]


def overlap(query_words: list[str], text: str) -> int:
    """How many of the query's content words appear in `text` (prefix match: play/playing)."""
    words = set(re.findall(r"\w{3,}", text.lower()))
    return sum(1 for q in query_words if any(w.startswith(q[:5]) for w in words))
