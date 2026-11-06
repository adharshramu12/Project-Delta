"""Posts for LinkedIn, Medium and Substack that read like a person wrote them.

Flow: prompt with the rules and the platform layout -> code checks -> safe mechanical fixes -> if
something real is still wrong (length, layout, AI words with no plain swap, flat rhythm), one
regeneration with the problems listed -> fix again -> report whatever remains.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from delta.addons.writer.rules import (
    AI_WORDS,
    HASHTAG,
    WRITING_RULES,
    Issue,
    check_text,
    fix_mechanical,
    rhythm_issue,
    word_count,
)


@dataclass(frozen=True)
class Platform:
    name: str
    label: str
    markdown: bool
    min_words: int
    max_words: int
    max_hashtags: int
    compose_url: str
    layout: str


PLATFORMS: dict[str, Platform] = {
    "linkedin": Platform(
        "linkedin", "LinkedIn", markdown=False, min_words=120, max_words=250, max_hashtags=3,
        compose_url="https://www.linkedin.com/feed/?shareActive=true",
        layout=("Plain text only: no markdown, no asterisks, no headings, no links in square brackets. "
                "Line 1 is a hook of at most 15 words that makes a person stop scrolling, then a blank line. "
                "Then short paragraphs of one to three sentences with blank lines between them. "
                "End with one honest question or a clear takeaway. At most 3 hashtags, all on the last line, "
                "and only if they help.")),
    "medium": Platform(
        "medium", "Medium", markdown=True, min_words=600, max_words=1200, max_hashtags=0,
        compose_url="https://medium.com/new-story",
        layout=("Markdown. Line 1: '# ' and a specific title (not clickbait). Line 2: blank. Line 3: a one-line "
                "subtitle in plain text that says what the reader gets. Then an opening of two or three short "
                "paragraphs, then 3 to 5 sections, each starting with '## ' and a plain heading. Use a list only "
                "where items really are a list. Close with a short ending section. No hashtags.")),
    "substack": Platform(
        "substack", "Substack", markdown=True, min_words=600, max_words=1200, max_hashtags=0,
        compose_url="https://substack.com/publish/post",
        layout=("Markdown, like a personal newsletter. Line 1: '# ' and a title. Line 2: blank. Line 3: a one-line "
                "subtitle in plain text. Open by talking straight to the reader. Then 3 to 5 sections, each "
                "starting with '## '. Close with a short sign-off line in the writer's voice. No hashtags.")),
}
SUBSTACK_HOST = re.compile(r"^[a-z0-9-]+\.substack\.com$")


def platform(name: str) -> Platform:
    key = name.strip().lower().replace(" ", "")
    if key not in PLATFORMS:
        raise ValueError(f"unknown platform '{name}'. Use one of: {', '.join(PLATFORMS)}")
    return PLATFORMS[key]


def check_post(text: str, plat: Platform, *, min_words: int | None = None, max_words: int | None = None,
               hashtags_allowed: int | None = None) -> list[Issue]:
    """Every rule a finished post must meet. Empty list = good to go."""
    issues = check_text(text)
    lo, hi = min_words or plat.min_words, max_words or plat.max_words
    n = word_count(text)
    if n < lo * 0.85:
        issues.append(Issue("length", f"too short: {n} words, aim for {lo} to {hi}"))
    elif n > hi * 1.15:
        issues.append(Issue("length", f"too long: {n} words, aim for {lo} to {hi}"))
    tags = HASHTAG.findall(text)
    allowed = plat.max_hashtags if hashtags_allowed is None else hashtags_allowed
    if len(tags) > allowed:
        issues.append(Issue("hashtags", f"{len(tags)} hashtags, at most {allowed} allowed", True))
    if r := rhythm_issue(text):
        issues.append(r)
    paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    longest = max((len(p.split()) for p in paras if not p.lstrip().startswith(("-", "*", "1."))), default=0)
    limit = 70 if plat.name == "linkedin" else 120
    if longest > limit:
        issues.append(Issue("paragraphs", f"a paragraph runs {longest} words; keep paragraphs short"))
    if plat.markdown:
        lines = text.strip().splitlines()
        if not lines or not lines[0].startswith("# "):
            issues.append(Issue("layout", "missing a '# ' title on the first line"))
        body = [ln for ln in lines[1:] if ln.strip()]
        if not body or body[0].startswith("#"):
            issues.append(Issue("layout", "missing a plain subtitle line under the title"))
        if sum(1 for ln in lines if ln.startswith("## ")) < 2:
            issues.append(Issue("layout", "needs at least two '## ' sections"))
    else:
        if re.search(r"(\*\*|__|^#{1,6}[ \t]|\[[^\]]+\]\([^)]+\)|`)", text, re.M):
            issues.append(Issue("markdown", "LinkedIn takes plain text: remove markdown", True))
        first = text.strip().splitlines()[0] if text.strip() else ""
        if len(first.split()) > 22:
            issues.append(Issue("hook", "the first line should be a short hook (under about 15 words)"))
        if len(paras) < 3:
            issues.append(Issue("paragraphs", "use short paragraphs separated by blank lines"))
    return issues


def tidy(text: str, plat: Platform, hashtags_allowed: int | None = None) -> str:
    t = text.strip().strip("`").strip()
    t = re.sub(r"^(?:here(?:'s| is) (?:your|the) (?:post|draft)[^\n]*\n+)", "", t, flags=re.I)
    allowed = plat.max_hashtags if hashtags_allowed is None else hashtags_allowed
    return fix_mechanical(t, markdown=plat.markdown, hashtags=allowed)


def post_prompt(plat: Platform, topic: str, notes: str, voice: str, min_words: int, max_words: int,
                hashtags_allowed: int, audience: str) -> str:
    tag_rule = (f"Hashtags: at most {hashtags_allowed}, last line only." if hashtags_allowed
                else "Hashtags: none.")
    parts = [
        f"Write a {plat.label} post for the user, in the user's own voice (first person, as them).",
        WRITING_RULES.format(ai_words=", ".join(AI_WORDS[:45])),
        f"{plat.label} layout: {plat.layout}",
        f"Length: {min_words} to {max_words} words. {tag_rule}",
        f"Topic: {topic.strip()}",
    ]
    if audience:
        parts.append(f"Audience: {audience.strip()}")
    if notes.strip():
        parts.append("Notes and facts to use (do not invent facts beyond these and common knowledge):\n"
                     + notes.strip()[:6000])
    if voice.strip():
        parts.append("How the user writes (copy their rhythm, word choice and tone, not their topics):\n"
                     + voice.strip()[:4000])
    parts.append("Reply with the post text only. No preface, no explanation, no quotes around it.")
    return "\n\n".join(parts)


def load_voice(path: Path | None, inline: str = "") -> str:
    """The user's past posts, used as a style sample. The file is optional and only ever read here."""
    text = inline.strip()
    if path and path.is_file() and path.stat().st_size < 400_000:
        text = (text + "\n\n" + path.read_text(encoding="utf-8", errors="replace")).strip()
    return text[-4000:]  # the newest posts are usually at the end


@dataclass
class PostResult:
    text: str
    platform: str
    words: int
    issues: list[Issue] = field(default_factory=list)
    attempts: int = 1

    @property
    def clean(self) -> bool:
        return not self.issues


Ask = Callable[[str], Awaitable[str]]


async def generate_post(ask: Ask, plat: Platform, topic: str, notes: str = "", voice: str = "",
                        min_words: int | None = None, max_words: int | None = None,
                        hashtags: int | None = None, audience: str = "") -> PostResult:
    """ask(prompt) -> model text. At most two model calls."""
    lo, hi = min_words or plat.min_words, max_words or plat.max_words
    allowed = plat.max_hashtags if hashtags is None else min(hashtags, 3 if plat.name == "linkedin" else hashtags)
    prompt = post_prompt(plat, topic, notes, voice, lo, hi, allowed, audience)
    text = tidy(await ask(prompt), plat, allowed)
    issues = check_post(text, plat, min_words=lo, max_words=hi, hashtags_allowed=allowed)
    attempts = 1
    if issues:
        attempts = 2
        retry = (prompt + "\n\nYour previous draft broke these rules:\n- " + "\n- ".join(map(str, issues))
                 + "\n\nPrevious draft:\n" + text + "\n\nRewrite it so every rule is met. Post text only.")
        second = tidy(await ask(retry), plat, allowed)
        second_issues = check_post(second, plat, min_words=lo, max_words=hi, hashtags_allowed=allowed)
        if len(second_issues) <= len(issues):
            text, issues = second, second_issues
    return PostResult(text, plat.name, word_count(text), issues, attempts)
