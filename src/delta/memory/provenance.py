"""Where a memory, lesson or recipe came from, and how far Delta may trust it.

Trust levels
  trusted    the user said it themselves (a chat message, the dashboard, `delta memory add`), or it is
             Delta's own fixed text about its own run, in a task that read nothing from outside
  agent      Delta's own short note about a past task (kept as history, never used as an instruction)
  untrusted  it came from outside content: a web page, search results, an email or chat message, a file,
             an attached file or picture, the screen, or a task that had read any of these and the value cannot be
             traced to the user

Untrusted items can be recalled as data (always labelled with where they came from), but can never become a
standing instruction, a lesson or a recipe step: the stores refuse to promote them, in code.

Data flow, not only task taint: almost every task reads something (a page, a window), so "the task was
tainted" alone would stop Delta learning anything. A value from a tainted task stays trusted only when it can
be traced to the user's own request: every content word of a fact appears in what the user typed, or a
recipe argument is a slot filled from the request. Anything else from a tainted task is untrusted.
"""

from __future__ import annotations

import re
from typing import Any

from delta.memory.keywords import keywords

TRUSTED, AGENT, UNTRUSTED = "trusted", "agent", "untrusted"
TRUST_LEVELS = (TRUSTED, AGENT, UNTRUSTED)

# Origins: what kind of source a memory came from.
USER_MESSAGE, TOOL_RESULT, WEB_READ, FILE, MESSAGE, SCREEN, AGENT_NOTE, LEGACY = (
    "user_message", "tool_result", "web_read", "file", "message", "screen", "agent_note", "legacy")
ATTACHMENT = "attachment"  # a file or picture the user sent in the chat (delta.attachments): what it says is not theirs
ORIGINS = (USER_MESSAGE, TOOL_RESULT, WEB_READ, FILE, MESSAGE, SCREEN, AGENT_NOTE, LEGACY, ATTACHMENT)

# The taint source (the first tool that brought outside content into the task) -> the kind of origin.
_TAINT_ORIGIN = {
    "browser_read": WEB_READ, "fetch_page": WEB_READ, "web_search": WEB_READ, "helper_read_page": WEB_READ,
    "parallel_research": WEB_READ, "web_answer": WEB_READ, "read_file": FILE, "read_messages": MESSAGE,
    "read_window": SCREEN,
    "look_at_screen": SCREEN, "look_with_marks": SCREEN, "read_screen_text": SCREEN,
    "attachment": ATTACHMENT, "read_attachment": ATTACHMENT, "look_at_attachment": ATTACHMENT,
    "sign_in": WEB_READ, "inspect_portal": WEB_READ, "watch_items": WEB_READ, "check_watcher": WEB_READ,
    "browser_wait": WEB_READ, "browser_tabs": WEB_READ,
}

ORIGIN_WORDS = {
    USER_MESSAGE: "you told me", TOOL_RESULT: "a tool result", WEB_READ: "a web page", FILE: "a file",
    MESSAGE: "a message", SCREEN: "the screen", AGENT_NOTE: "Delta's note on a task", LEGACY: "older memory",
    ATTACHMENT: "a file you attached",
}


def origin_of_taint(source: str | None) -> str:
    if not source:
        return USER_MESSAGE
    if source.startswith("helper:"):
        return WEB_READ
    return _TAINT_ORIGIN.get(source, TOOL_RESULT)


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w{3,}", text.lower()))


# Words that say nothing on their own: they may appear anywhere without making a value suspicious.
_PLAIN = frozenset({"user", "users", "https", "http", "www", "com", "org", "net", "html", "htm", "php", "aspx",
                    "index", "search", "query", "results", "prefers", "prefer", "likes", "like", "uses", "use",
                    "wants", "want", "called", "named"})


def _said(word: str, said: set[str]) -> bool:
    return any(w.startswith(word[:5]) or word.startswith(w[:5]) for w in said)


def foreign_words(value: str, request: str) -> list[str]:
    """Content words of `value` the user did not type."""
    said = _words(request)
    return [w for w in keywords(value, limit=40) if w not in _PLAIN and not _said(w, said)]


def traced_to_user(value: str, request: str, outside: str | None = None) -> bool:
    """Can `value` be traced to the user rather than to outside content? With `outside` (the text the task read
    from outside sources): no word of the value that the user did not type appears in that text. Without it:
    every content word of the value is in what the user typed."""
    extra = foreign_words(value, request)
    if not extra:
        return True
    if outside is None:
        return False
    seen = _words(outside)
    return not any(_said(w, seen) for w in extra)


def fact_trust(fact: str, request: str, taint_source: str | None, outside: str | None = None) -> tuple[str, str]:
    """(trust, origin) of a fact saved during a task. Untainted task: the user's own words. Tainted task:
    trusted only when the fact can be traced to the request, else untrusted with the taint's origin."""
    if not taint_source:
        return TRUSTED, USER_MESSAGE
    if traced_to_user(fact, request, outside):
        return TRUSTED, USER_MESSAGE
    return UNTRUSTED, origin_of_taint(taint_source)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


_SLOT = re.compile(r"\{[a-z][a-z0-9_]{0,30}\}")


def recipe_trust(steps: list[dict[str, Any]], request: str, taint_source: str | None,
                 outside: str | None = None) -> tuple[str, str]:
    """(trust, reason) of recipe steps learned in a task. When the task read outside content, every literal
    argument (slots removed) must be traceable to the user (see traced_to_user); a value that came from a page
    or a file makes the recipe untrusted. Numbers alone (a ref, a count) carry no instruction."""
    if not taint_source:
        return TRUSTED, ""
    for step in steps:
        for text in _strings(step.get("args", {})):
            literal = _SLOT.sub(" ", text)
            if not traced_to_user(literal, request, outside):
                return UNTRUSTED, (f"step {step.get('tool')} uses '{literal.strip()[:60]}', which is not in your "
                                   f"request and the task read outside content ({taint_source})")
    return TRUSTED, ""
