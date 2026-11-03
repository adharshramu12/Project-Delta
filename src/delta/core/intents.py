"""Instant commands: common requests that need no AI at all to understand.

"pause the music", "next song", "play Kaanunna Kalyanam on YouTube", "start a Google Meet" map
straight to one tool call: 0 tokens and no wait for a planner. The patterns are deliberately strict;
anything ambiguous ("play chess", "schedule a meeting tomorrow with Ravi") goes to the planner as
before, and if the tool cannot prove its result the normal agent takes over.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Intent:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    check: str = ""  # the checkpoint this command must meet


MEDIA_WORDS = r"(?:the\s+)?(?:music|song|video|track|audio|playback|youtube)"
POLITE = r"^(?:please\s+|delta[,\s]+|hey\s+delta[,\s]+)*"
_RULES: list[tuple[re.Pattern[str], Any]] = [
    (re.compile(POLITE + rf"(?:pause|stop)\s+{MEDIA_WORDS}\s*[.!]*$", re.I),
     lambda m: Intent("media_control", {"action": "pause"}, "playback is paused")),
    (re.compile(POLITE + rf"(?:resume|continue|unpause)(?:\s+{MEDIA_WORDS})?\s*[.!]*$", re.I),
     lambda m: Intent("media_control", {"action": "play"}, "playback resumed")),
    (re.compile(POLITE + r"(?:next|skip)(?:\s+(?:song|track|video))?\s*[.!]*$", re.I),
     lambda m: Intent("media_control", {"action": "next"}, "the next track is playing")),
    (re.compile(POLITE + r"(?:previous|prev|last)\s+(?:song|track|video)\s*[.!]*$", re.I),
     lambda m: Intent("media_control", {"action": "previous"}, "the previous track is playing")),
    (re.compile(POLITE + r"(?:what(?:'s| is)\s+playing|what\s+song\s+is\s+this)\s*\??$", re.I),
     lambda m: Intent("media_status", {}, "the playing media is reported")),
    # "play X on youtube" / "play X song" / "youtube X"
    (re.compile(POLITE + r"(?:play|put on)\s+(?P<q>.+?)\s+(?:on|in|from)\s+youtube\s*[.!]*$", re.I),
     lambda m: Intent("play_youtube", {"query": _clean(m["q"])}, f"'{_clean(m['q'])}' is playing on YouTube")),
    (re.compile(POLITE + r"(?:play|put on)\s+(?P<q>.+?\b(?:song|songs|video|music|trailer)\b.*?)\s*[.!]*$", re.I),
     lambda m: Intent("play_youtube", {"query": _clean(m["q"])}, f"'{_clean(m['q'])}' is playing on YouTube")),
    (re.compile(POLITE + r"stop (?:chatting|replying|talking)(?: (?:with|to) (?P<who>.+?))?\s*[.!]*$", re.I),
     lambda m: Intent("chat_for_me", {"stop": True, **({"person": m["who"].strip()} if m["who"] else {})},
                      "Delta stopped chatting")),
    # "start / create / open a (new / instant) google meet (meeting)"
    (re.compile(POLITE + r"(?:start|create|open|make|set\s*up|begin)\s+(?:an?\s+)?(?:new\s+|instant\s+|quick\s+)*"
                r"(?P<p>google\s+meet|gmeet|meet|zoom|teams|microsoft\s+teams)(?:\s+(?:meeting|call|link))?\s*[.!]*$",
                re.I),
     lambda m: Intent("start_meeting", {"platform": _platform(m["p"])}, "a meeting link was created")),
]


def _clean(q: str) -> str:
    return re.sub(r"^(?:the|a|an)\s+", "", q.strip(), flags=re.I).strip(" .!\"'")


def _platform(p: str) -> str:
    p = p.lower()
    return "zoom" if "zoom" in p else "teams" if "teams" in p else "google_meet"


def match_intent(text: str, available: set[str]) -> Intent | None:
    t = " ".join(text.split())
    if len(t) > 160 or "\n" in text:
        return None  # long or multi-part requests need real planning
    for pattern, build in _RULES:
        m = pattern.match(t)
        if m:
            intent = build(m)
            return intent if intent.tool in available else None
    return None


# Greetings and thanks: answered on the spot. Sending "hi" to an AI app costs ~12k tokens of its
# fixed instructions and 10-20 seconds, for a reply anyone can write.
_GREETING = re.compile(POLITE + r"(hi+|hey+|hello+|hii+|yo|namaste|vanakkam|good\s+(?:morning|afternoon|evening))"
                       r"(?:\s+(?:there|delta|buddy|bro|da))?\s*[!.]*\s*$", re.I)
_THANKS = re.compile(POLITE + r"(thanks?|thank\s+you|thx|ty|ok(?:ay)?\s+thanks?|great,?\s+thanks?|super|nice|cool)"
                     r"(?:\s+(?:so\s+much|a\s+lot|delta|buddy|bro|da))?\s*[!.]*\s*$", re.I)


def small_talk(text: str, owner: str = "") -> str | None:
    t = " ".join(text.split())
    name = f", {owner}" if owner else ""
    if _GREETING.match(t):
        return f"Hi{name}! What should I do for you?"
    if _THANKS.match(t):
        return "Anytime! Tell me if you need anything else."
    return None
