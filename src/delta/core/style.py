"""One writing style for everything Delta writes for the user, and the 'slop check' that enforces it.

Two parts, both cheap:
- STYLE_BLOCK and WARMTH_BLOCK: a few lines added to the planner and agent prompts (about 140 tokens).
- slop_check(): a deterministic pass over finished text (no model call). It rewrites long dashes, removes or
  swaps AI-slop phrases, fixes US spellings and Americanisms ("gotten"), drops emojis where they do not belong,
  and replaces any claim to be a human or to have real feelings with an honest line.
- polish_outgoing(): for content that leaves the PC under the user's name (an email): slop_check, then ONE cheap
  model rewrite only if something the code cannot fix safely is still there, then slop_check again.

What is never touched: URLs, email addresses, code (fenced or `inline`), file paths, command-line flags and
markdown table rules. Chat replies keep a user's emoji when the user writes with emojis; professional text
(emails, documents, app content) never has them.

Messages Delta types to the user's friends (send_message, chat_for_me) copy the user's own texting style and
keep their own rules (delta.messaging.style); posts and documents keep theirs (delta.addons.writer.rules), which
share the word lists below.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from delta.addons.writer.rules import US_TO_IN

Kind = Literal["chat", "outgoing", "app"]

STYLE_BLOCK = """WRITING STYLE (every reply, email and document; code checks it afterwards):
- Crisp, plain Indian English: short words, short sentences, warm and direct. British/Indian spelling
  (colour, organise, centre); no Americanisms ("gotten") and no US/UK slang.
- Never em dashes, en dashes or long dashes: use a comma, full stop, colon or brackets.
- No emojis in professional content; in casual chat only if the user uses them.
- No AI phrases: delve, tapestry, "I hope this email finds you well", "in today's fast-paced world",
  game-changer, leverage, synergy, "Certainly!", "Great question!", "As an AI language model"."""

WARMTH_BLOCK = """WARMTH: notice the user's mood. Stressed or frustrated: acknowledge it in a few words, stay calm
and patient, give the next concrete step. Happy: share the good news briefly. Be kind, never fake: you are an
AI, so never claim real feelings, a body, a life or being human; if asked, say plainly you are an AI that cares
about getting this right for them."""

# --- what the check looks for ----------------------------------------------------------------------------------

# (pattern, replacement). Replacement "" drops the phrase; the tidy-up below fixes the spaces and capitals left.
_PHRASES: list[tuple[str, str]] = [
    (r"i hope (?:this|my) (?:e-?mail|message|note|letter) finds you (?:well|in good health)[.!,]?", ""),
    (r"i hope this finds you well[.!,]?", ""),
    (r"i trust this (?:e-?mail|message) finds you well[.!,]?", ""),
    (r"in today'?s (?:fast-paced|fast paced|digital|ever-changing|modern) (?:world|age|era|landscape),?", ""),
    (r"in the ever-evolving (?:world|landscape) of", "in"),
    (r"as an ai(?: language model)?,", ""),
    (r"(?:great|excellent|good) question[!.]", ""),
    (r"certainly[!]", ""),
    (r"absolutely[!]", ""),
    (r"i(?:'d| would) be (?:happy|glad|delighted) to help(?: you)?(?: with (?:that|this))?[.!]?", ""),
    (r"leverag(?:e|ing) (?:our |the )?synerg(?:y|ies)", "work together"),
    (r"synergies", "joint strengths"),
    (r"delv(?:e|es|ing) (?:deep )?into", "look at"),
    (r"delve", "look"),
    (r"(?:a |the )?rich tapestry of", "a mix of"),
    (r"tapestry", "mix"),
    (r"game[- ]changers", "big shifts"),
    (r"(?:a |the )game[- ]changer", "a big shift"),
    (r"game[- ]changer", "big shift"),
    (r"game[- ]changing", "major"),
    (r"leveraging", "using"),
    (r"leverages", "uses"),
    (r"leveraged", "used"),
    (r"leverage(?= (?:the|our|your|their|this|these|those|it|them|a|an|ai|existing|data)\b)", "use"),
    (r"utili[sz]ing", "using"),
    (r"utili[sz]e", "use"),
    (r"utili[sz]ed", "used"),
    (r"gotten", "got"),
]
_PHRASE_RE = re.compile(r"(?<![\w-])(" + "|".join(f"(?:{p})" for p, _ in _PHRASES) + r")(?![\w])", re.I)
_PHRASE_SUBS = [(re.compile(rf"^(?:{p})$", re.I), r) for p, r in _PHRASES]

# Claims that Delta is a person or has real feelings: replaced with an honest line (rule D).
_HUMAN_CLAIM = re.compile(
    r"[^.!?\n]*\b(?:i(?:'m| am) (?:a |an )?(?:real )?(?:human|person|man|woman|girl|boy)(?: being)?\b"
    r"|i(?:'m| am) not (?:an? )?(?:ai|bot|robot|machine|program)\b"
    r"|i (?:really |truly |genuinely )?(?:have|feel) (?:real |genuine |human )?(?:feelings|emotions)\b"
    r"|i (?:have|had) (?:a body|a family|a childhood)\b"
    r"|as a (?:human|person),)[^.!?\n]*[.!?]?", re.I)
HONEST_LINE = "I'm an AI, so I don't have feelings like a person does, but I do care about getting this right for you."

DASH_CHARS = "\u2014\u2013\u2012\u2015\u2E3A\u2E3B\uFE58\uFE63\uFF0D"
_DASH_RE = re.compile(rf"[{DASH_CHARS}]|(?<![-\w])-{{2,}}(?![-\w])|(?<=\s)-{{2,}}(?=\s)")
# Emoji sequences only. A lone zero-width joiner is part of Indic scripts (Telugu, Hindi, Malayalam) and stays.
_PICTO = "\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF"
EMOJI_RE = re.compile(rf"[{_PICTO}][\uFE0F\U0001F3FB-\U0001F3FF]?(?:\u200D[{_PICTO}][\uFE0F]?)*|\uFE0F|\u20E3")

# Never edited: URLs, emails, code, paths, flags, markdown table rules, file names.
_PROTECT = re.compile(
    r"```.*?```|`[^`\n]+`"
    r"|https?://\S+|www\.\S+"
    r"|[\w.+-]+@[\w-]+\.[\w.-]+"
    r"|[A-Za-z]:\\\S*|(?<!\w)(?:\.{1,2}|~)?[\\/][\w.$%-]+(?:[\\/][\w.$%-]*)+"
    r"|(?<!\w)--?[A-Za-z][\w-]*"
    r"|^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{0,}:?\s*\|?\s*$"
    r"|\b[\w-]+\.(?:txt|md|py|js|ts|json|yaml|yml|csv|xlsx?|docx?|pptx?|pdf|png|jpe?g|html?|css|exe|zip|log)\b",
    re.S | re.M | re.I)
_SPELL_RE = re.compile(r"(?<![-\w./#=])(" + "|".join(sorted((re.escape(w) for w in US_TO_IN), key=len, reverse=True))
                       + r")(?![-\w(=]|:\s*\S)", re.I)


@dataclass
class SlopReport:
    text: str
    fixed: list[str] = field(default_factory=list)      # what the code changed (codes)
    remaining: list[str] = field(default_factory=list)  # what is still there and needs a writer

    @property
    def clean(self) -> bool:
        return not self.remaining


def _outside_protected(text: str, fn: Callable[[str], str]) -> str:
    out, last = [], 0
    for m in _PROTECT.finditer(text):
        out.append(fn(text[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(fn(text[last:]))
    return "".join(out)


def _plain(text: str) -> str:
    return _PROTECT.sub(" ", text)


def _match_case(src: str, repl: str) -> str:
    if not repl:
        return repl
    if src.isupper() and len(src) > 1 and len(repl) > 1:
        return repl.upper()
    if src[:1].isupper():
        return repl[:1].upper() + repl[1:]
    return repl


def _swap_phrase(m: re.Match[str]) -> str:
    src = m.group(1)
    for rx, repl in _PHRASE_SUBS:
        if rx.match(src):
            return _match_case(src, repl) if repl else "\x00" if src[:1].isupper() else ""
    return src


def fix_dashes(text: str) -> str:
    def one(seg: str) -> str:
        seg = re.sub(rf"(\d)\s*[{DASH_CHARS}]\s*(\d)", r"\1 to \2", seg)  # a range with a dash: 10 to 12
        seg = re.sub(rf"\s*(?:[{DASH_CHARS}]|(?<![-\w])-{{2,}}(?![-\w]))\s*", ", ", seg)
        seg = re.sub(r",\s*,", ",", seg)
        seg = re.sub(r"^\s*,\s*", "", seg, flags=re.M)
        return re.sub(r",\s*([.!?:;)])", r"\1", seg)

    return _outside_protected(text, one)


def _tidy(text: str) -> str:
    t = re.sub("\x00\\s*([a-z])", lambda m: m.group(1).upper(), text).replace("\x00", "")
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"[ \t]+([,.!?;:])", r"\1", t)
    t = re.sub(r"^[ \t]*[,.;:][ \t]*", "", t, flags=re.M)
    t = t[:1].upper() + t[1:] if t[:1].islower() and text[:1].isupper() else t
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def user_uses_emoji(user_text: str) -> bool:
    return bool(EMOJI_RE.search(user_text or ""))


def slop_check(text: str, kind: Kind = "chat", user_text: str = "") -> SlopReport:
    """Fix what code can fix safely; report what is left. Idempotent. kind:
    chat      a reply to the user (emojis kept only when the user's own message has them)
    outgoing  an email or document going out under the user's name (no emojis, ever)
    app       text Delta puts into a connected app (dashes and AI phrases only; emojis are the user's call)."""
    if not text or not text.strip():
        return SlopReport(text or "")
    fixed: list[str] = []
    t = text.replace("\r\n", "\n")
    if _DASH_RE.search(_plain(t)):
        t = fix_dashes(t)
        fixed.append("dash")
    if _PHRASE_RE.search(_plain(t)):
        before = t
        t = _outside_protected(t, lambda seg: _PHRASE_RE.sub(_swap_phrase, seg))
        if t != before:
            fixed.append("ai_phrase")
    if kind != "app" and _SPELL_RE.search(_plain(t)):
        t = _outside_protected(t, lambda seg: _SPELL_RE.sub(
            lambda m: _match_case(m.group(1), US_TO_IN[m.group(1).lower()]), seg))
        fixed.append("us_spelling")
    if kind in ("chat", "outgoing") and _HUMAN_CLAIM.search(t):
        t = _HUMAN_CLAIM.sub(HONEST_LINE, t, count=1)
        t = _HUMAN_CLAIM.sub("", t)
        fixed.append("human_claim")
    keep_emoji = kind == "app" or (kind == "chat" and user_uses_emoji(user_text))
    if not keep_emoji and EMOJI_RE.search(_plain(t)):
        t = _outside_protected(t, lambda seg: EMOJI_RE.sub("", seg))
        fixed.append("emoji")
    if fixed:
        t = _tidy(t)
    return SlopReport(t, fixed, remaining_issues(t, kind))


def remaining_issues(text: str, kind: Kind = "outgoing") -> list[str]:
    """What still breaks the style after slop_check (things only a writer can fix)."""
    from delta.addons.writer.rules import AI_WORD_RE

    plain = _plain(text)
    out = []
    if _DASH_RE.search(plain):
        out.append("long dash")
    if kind == "outgoing":
        words = sorted({m.group(1).lower() for m in AI_WORD_RE.finditer(plain)})
        if words:
            out.append("AI-sounding words: " + ", ".join(words[:6]))
    return out


REWRITE_PROMPT = STYLE_BLOCK + """

Rewrite the text below so it follows these rules. Keep the meaning, facts, names, numbers, links and length.
Fix only: {issues}. Reply with the rewritten text only, nothing else."""


async def polish_outgoing(text: str, router: Any = None, *, user_text: str = "") -> SlopReport:
    """Outgoing content (an email): code fixes first; one cheap model rewrite only if something is left that code
    cannot fix safely; then the code check again. Never more than one model call."""
    report = slop_check(text, "outgoing", user_text)
    if report.clean or router is None:
        return report
    try:
        resp = await router.complete(
            [{"role": "system", "content": REWRITE_PROMPT.format(issues="; ".join(report.remaining))},
             {"role": "user", "content": report.text}], task="chat")
        rewritten = (getattr(resp, "content", "") or "").strip()
    except Exception:  # no model: keep the code-fixed text; the person sees it before it goes anywhere
        return report
    if not rewritten or len(rewritten) < len(report.text) * 0.5:
        return report  # a broken rewrite never replaces a good draft
    again = slop_check(rewritten, "outgoing", user_text)
    again.fixed = [*report.fixed, "rewrite", *again.fixed]
    return again
