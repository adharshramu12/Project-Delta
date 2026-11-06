"""Writing rules for posts and documents, enforced in code after the model writes.

The model is told the rules (WRITING_RULES), and then `check_text` / `check_post` look for what still
slipped through, and `fix_mechanical` repairs what can be repaired safely without changing meaning:
long dashes, backslashes, emojis, US spellings, extra hashtags, markdown on LinkedIn, and a few AI
phrases with a plain replacement. What cannot be fixed safely (an AI word with no neutral swap,
flat sentence rhythm, a wall of text) is reported, so the caller can regenerate once and then say
what remains.

Builds on the chat rules in delta.messaging.style (AI_PHRASES), which stay as they are.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

from delta.messaging.style import AI_PHRASES

WRITING_RULES = """Writing rules (these are checked by code afterwards, so follow them exactly):
- Indian English: British spelling as used in India (organise, colour, centre, behaviour, analyse,
  travelling, programme for an event or scheme, program for software). Natural Indian idiom, plain and
  warm. No US or UK corporate templates, no "Dear valued reader", no "I hope this finds you well".
- Never use em dashes, en dashes or runs of hyphens. Use a comma, a full stop, a colon or brackets.
- No backslashes. No emojis. No hashtags unless the platform rules below allow them.
- None of these AI words or phrases: {ai_words}.
- Avoid the pattern "it's not just X, it's Y" and "not only X but also Y" as a crutch.
- Mix short and long sentences. Some sentences can be four words. Some can run to twenty-five.
- Short paragraphs (one to three sentences) with a blank line between them.
- Concrete over abstract: real numbers, names, examples and what actually happened.
- Sound like one person talking to another, not like a brochure. First person is fine."""

# Words and phrases that make text read as machine-written. Matched as whole words, case-insensitive.
AI_WORDS: list[str] = [
    "delve", "delves", "delving", "tapestry", "leverage", "leverages", "leveraging", "leveraged",
    "unlock", "unlocks", "unlocking", "game-changer", "game changer", "game-changing", "in today's fast-paced world",
    "in today's digital age", "in today's world", "in the ever-evolving", "ever-evolving", "ever-changing landscape",
    "landscape of", "navigate the complexities", "navigating the complexities", "realm", "embark", "embarking",
    "testament to", "a testament", "seamless", "seamlessly", "synergy", "synergies", "paradigm shift",
    "cutting-edge", "revolutionize", "revolutionise", "revolutionizing", "revolutionising", "transformative",
    "elevate", "elevates", "elevating", "empower", "empowering", "empowers", "harness", "harnessing",
    "robust", "holistic", "pivotal", "multifaceted", "intricate", "intricacies", "meticulous", "meticulously",
    "underscore", "underscores", "underscoring", "showcase", "showcasing", "foster", "fostering", "bustling",
    "vibrant", "crucial role", "plays a vital role", "plays a crucial role", "at the end of the day",
    "it is important to note", "it's important to note", "it is worth noting", "it's worth noting",
    "in conclusion", "to sum up", "furthermore", "moreover", "additionally", "unleash", "unleashing",
    "supercharge", "next-level", "dive deep", "deep dive", "let's dive in", "let's dive into", "buckle up",
    "stay tuned", "without further ado", "look no further", "rest assured", "a myriad of", "myriad",
    "plethora", "utilize", "utilise", "utilizing", "utilising", "utilized", "utilised", "endeavour", "endeavor",
    "beacon",
    "treasure trove", "journey of", "the world of", "thought leadership", "move the needle", "circle back",
    "certainly", "as an ai", "i hope this finds you well", "feel free to", "don't hesitate to",
]
# Safe swaps: same meaning in plain words. Anything not here is only reported.
AI_SWAPS: dict[str, str] = {
    "leverage": "use", "leverages": "uses", "leveraging": "using", "leveraged": "used",
    "utilize": "use", "utilise": "use", "utilizing": "using", "utilising": "using",
    "utilized": "used", "utilised": "used", "utilizes": "uses", "utilises": "uses",
    "delve into": "look at", "delves into": "looks at", "delving into": "looking at",
    "game-changer": "big shift", "game changer": "big shift", "game-changing": "big",
    "in today's fast-paced world, ": "", "in today's fast-paced world": "today",
    "in today's digital age, ": "", "in today's digital age": "today",
    "seamless": "smooth", "seamlessly": "smoothly", "cutting-edge": "latest", "myriad": "many",
    "a myriad of": "many", "plethora": "lot", "furthermore, ": "", "moreover, ": "", "additionally, ": "Also, ",
    "in conclusion, ": "", "to sum up, ": "", "it is important to note that ": "", "it's important to note that ": "",
    "it is worth noting that ": "", "it's worth noting that ": "", "certainly, ": "", "robust": "solid",
    "pivotal": "key", "showcase": "show", "showcasing": "showing", "empower": "help", "empowering": "helping",
    "empowers": "helps", "elevate": "lift", "elevating": "lifting", "realm": "area", "embark on": "start",
    "embarking on": "starting", "deep dive": "close look", "let's dive in": "here goes",
}
NOT_JUST = re.compile(r"\b(?:it'?s|this is|that'?s|it is|they'?re|we'?re)\s+not\s+(?:just|only|merely)\b[^.!?\n]{1,80}?"
                      r"[,;:]\s*(?:it'?s|it is|but|this is|they'?re|we'?re)\b", re.I)
NOT_ONLY = re.compile(r"\bnot only\b[^.!?\n]{1,80}?\bbut also\b", re.I)

# US -> Indian (British) spelling, safe to change automatically. "program" is left alone: Indian English
# writes "program" for software and "programme" for an event or scheme, which only the writer can tell.
_IZE_STEMS = ["organ", "real", "recogn", "priorit", "optim", "apolog", "summar", "emphas", "critic", "minim",
              "maxim", "custom", "special", "util", "standard", "categor", "memor", "visual", "final", "author",
              "character", "modern", "monet", "personal", "familiar", "mobil", "capital", "general", "symbol",
              "central", "sympath", "revolution", "harmon", "stabil", "normal", "local", "global", "digit",
              "human", "industrial", "commercial", "decentral", "vapor", "synchron"]
_IZE_SUFFIX = {"ize": "ise", "izes": "ises", "ized": "ised", "izing": "ising", "ization": "isation",
               "izations": "isations", "izer": "iser", "izers": "isers"}
_SPELL_BASE: dict[str, str | None] = {
    "color": "colour", "colors": "colours", "colored": "coloured", "colorful": "colourful", "coloring": "colouring",
    "favorite": "favourite", "favorites": "favourites", "behavior": "behaviour", "behaviors": "behaviours",
    "behavioral": "behavioural", "honor": "honour", "honors": "honours", "honored": "honoured",
    "labor": "labour", "neighbor": "neighbour", "neighbors": "neighbours", "neighborhood": "neighbourhood",
    "humor": "humour", "flavor": "flavour", "flavors": "flavours", "rumor": "rumour", "rumors": "rumours",
    "endeavor": "endeavour", "harbor": "harbour", "savior": "saviour", "vigor": "vigour",
    "center": "centre", "centers": "centres", "centered": "centred", "theater": "theatre", "theaters": "theatres",
    "liter": "litre", "liters": "litres", "fiber": "fibre", "fibers": "fibres", "caliber": "calibre",
    "defense": "defence", "offense": "offence", "catalog": "catalogue", "catalogs": "catalogues",
    "analog": "analogue", "traveling": "travelling", "traveled": "travelled", "traveler": "traveller",
    "travelers": "travellers", "canceled": "cancelled", "canceling": "cancelling", "modeling": "modelling",
    "modeled": "modelled", "labeled": "labelled", "labeling": "labelling", "jewelry": "jewellery",
    "gray": "grey", "enroll": "enrol", "enrollment": "enrolment", "fulfill": "fulfil", "fulfillment": "fulfilment",
    "skillful": "skilful", "aging": "ageing", "analyze": "analyse", "analyzed": "analysed", "analyzes": "analyses",
    "analyzing": "analysing", "paralyze": "paralyse", "catalyze": "catalyse", "judgment": "judgement",
    "acknowledgment": "acknowledgement", "mold": "mould", "plow": "plough", "cozy": "cosy", "pajamas": "pyjamas",
    "mustache": "moustache", "tire": None,  # tyre vs tire (be tired): ambiguous, never changed
}
US_TO_IN: dict[str, str] = {k: v for k, v in _SPELL_BASE.items() if v}
for _stem in _IZE_STEMS:
    for _us, _in in _IZE_SUFFIX.items():
        US_TO_IN[_stem + _us] = _stem + _in

DASHES = re.compile(r"[\u2014\u2013\u2012\u2015\u2E3A\u2E3B\uFE58\uFE63\uFF0D]|-{2,}")
# U+1F000 to U+1FAFF already holds the supplemental symbols (1F900-1F9FF) and the flag letters (1F1E6-1F1FF).
EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
                   "\U0000FE0F\U0000200D\U000020E3\U00002190-\U000021FF\U00002300-\U000023FF"
                   "\U00003030\U0000303D\U00003297\U00003299\U000024C2\U000025A0-\U000025FF]")
HASHTAG = re.compile(r"(?<![\w&/])#[A-Za-z][\w]*")
URL = re.compile(r"https?://\S+|www\.\S+")
CODE = re.compile(r"```.*?```|`[^`\n]+`", re.S)
_WORD_LIST = re.compile(r"\b(" + "|".join(sorted((re.escape(w) for w in US_TO_IN), key=len, reverse=True))
                        + r")\b", re.I)


def _ai_pattern(phrases: list[str]) -> re.Pattern[str]:
    def alt(p: str) -> str:
        body = re.escape(p).replace("'", "['\u2019]")
        return body + (r"(?![\w-])" if p[-1].isalnum() else "")

    alts = [alt(p) for p in sorted(phrases, key=len, reverse=True)]  # longest phrase wins
    return re.compile(r"(?<![\w-])(" + "|".join(alts) + r")", re.I)


AI_WORD_RE = _ai_pattern(AI_WORDS)
AI_SWAP_RE = _ai_pattern(list(AI_SWAPS))


@dataclass(frozen=True)
class Issue:
    code: str      # dash | backslash | emoji | ai_word | us_spelling | hashtags | markdown | length | ...
    message: str
    fixable: bool = False   # fix_mechanical repairs it

    def __str__(self) -> str:
        return self.message


def _match_case(src: str, repl: str) -> str:
    if not repl:
        return repl
    if src.isupper() and len(src) > 1:
        return repl.upper()
    if src[:1].isupper():
        return repl[:1].upper() + repl[1:]
    return repl


def _outside_protected(text: str, fn: object) -> str:
    """Apply fn to the parts of text that are not URLs or code."""
    assert callable(fn)
    out, last = [], 0
    spans = sorted([m.span() for m in URL.finditer(text)] + [m.span() for m in CODE.finditer(text)])
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s, e))
    for s, e in merged:
        out.append(fn(text[last:s]))
        out.append(text[s:e])
        last = e
    out.append(fn(text[last:]))
    return "".join(out)


def _plain(text: str) -> str:
    """The text with URLs and code removed, for checks."""
    return CODE.sub(" ", URL.sub(" ", text))


def ai_words_in(text: str) -> list[str]:
    found = [m.group(1).lower() for m in AI_WORD_RE.finditer(_plain(text))]
    found += [m.group(0).strip().lower() for m in AI_PHRASES.finditer(_plain(text)) if m.group(0).strip()]
    if NOT_JUST.search(text):
        found.append("it's not just X, it's Y")
    if NOT_ONLY.search(text):
        found.append("not only X but also Y")
    return list(dict.fromkeys(found))


def us_spellings_in(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(1).lower() for m in _WORD_LIST.finditer(_plain(text))))


def check_text(text: str) -> list[Issue]:
    """Rules for any prose Delta writes for the user (posts and documents)."""
    issues: list[Issue] = []
    plain = _plain(text)
    if DASHES.search(plain):
        issues.append(Issue("dash", "contains an em dash, en dash or a run of hyphens", True))
    if "\\" in plain:
        issues.append(Issue("backslash", "contains a backslash", True))
    if EMOJI.search(text):
        issues.append(Issue("emoji", "contains emojis", True))
    if words := ai_words_in(text):
        fixable = all(AI_SWAP_RE.fullmatch(w) for w in words)
        issues.append(Issue("ai_word", "AI-sounding words: " + ", ".join(words[:8]), fixable))
    if spell := us_spellings_in(text):
        issues.append(Issue("us_spelling", "US spellings: " + ", ".join(spell[:8]), True))
    return issues


def sentences(text: str) -> list[str]:
    body = re.sub(r"^#+\s.*$", "", text, flags=re.M)
    return [s for s in re.split(r"(?<=[.!?])\s+|\n{2,}", body) if len(s.split()) >= 2]


def rhythm_issue(text: str) -> Issue | None:
    """Every sentence the same length reads as machine-made."""
    lengths = [len(s.split()) for s in sentences(text)]
    if len(lengths) < 6:
        return None
    if statistics.pstdev(lengths) < 3.0:
        return Issue("rhythm", "sentences are all about the same length; mix short and long ones")
    return None


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][\w'%.-]*", _strip_markdown(text)))


def _strip_markdown(text: str) -> str:
    t = re.sub(r"^[ \t]{0,3}#{1,6}[ \t]+", "", text, flags=re.M)  # headings, never #hashtags
    t = re.sub(r"(?<!\w)(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1(?!\w)", r"\2", t)  # snake_case stays
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", t)
    t = t.replace("`", "")
    return t


def strip_markdown(text: str) -> str:
    """Markdown -> plain text for LinkedIn (bold markers, headings, links)."""
    t = _strip_markdown(text)
    t = re.sub(r"^\s*[*+]\s+", "- ", t, flags=re.M)  # keep bullets as a simple hyphen people type
    return t


def fix_dashes(text: str) -> str:
    def one(seg: str) -> str:
        seg = re.sub(r"^\s*-{3,}\s*$", "", seg, flags=re.M)                       # markdown rules
        seg = re.sub(r"(\d)\s*[\u2013\u2014\u2012]\s*(\d)", r"\1 to \2", seg)   # 10-12 ranges
        seg = re.sub(r"\s*(?:[\u2014\u2013\u2012\u2015\u2E3A\u2E3B\uFE58\uFE63\uFF0D]|-{2,})\s*", ", ", seg)
        seg = re.sub(r",\s*,", ",", seg)
        seg = re.sub(r"^\s*,\s*", "", seg, flags=re.M)
        seg = re.sub(r",\s*([.!?:;])", r"\1", seg)
        return seg

    return _outside_protected(text, one)


def fix_spelling(text: str) -> str:
    return _outside_protected(text, lambda seg: _WORD_LIST.sub(
        lambda m: _match_case(m.group(1), US_TO_IN[m.group(1).lower()]), seg))


def fix_ai_words(text: str) -> str:
    def swap(m: re.Match[str]) -> str:
        src = m.group(1)
        repl = AI_SWAPS[src.lower().replace("\u2019", "'")]
        if not repl:  # a dropped opener: the next word starts the sentence if the opener did
            return "\x00" if src[:1].isupper() else ""
        return _match_case(src, repl)

    out = _outside_protected(text, lambda seg: AI_SWAP_RE.sub(swap, seg))
    out = re.sub("\x00([a-z])", lambda m: m.group(1).upper(), out)
    return out.replace("\x00", "")


def limit_hashtags(text: str, keep: int) -> str:
    seen = 0

    def one(m: re.Match[str]) -> str:
        nonlocal seen
        seen += 1
        return m.group(0) if seen <= keep else ""

    out = HASHTAG.sub(one, text)
    out = re.sub(r"[ \t]+\n", "\n", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def fix_mechanical(text: str, *, markdown: bool = True, hashtags: int | None = None) -> str:
    """Repair what can be repaired without changing meaning. Idempotent."""
    t = text.replace("\r\n", "\n")
    t = fix_dashes(t)
    t = _outside_protected(t, lambda seg: seg.replace("\\", ""))
    t = EMOJI.sub("", t)
    t = fix_spelling(t)
    t = fix_ai_words(t)
    if not markdown:
        t = strip_markdown(t)
    if hashtags is not None:
        t = limit_hashtags(t, hashtags)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r" +([,.!?;:])", r"\1", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()
