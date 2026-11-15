"""What Delta knows about itself: its version and what the last releases added.

An agent that does not know its own features cannot offer them. Delta's tools are always in the planner's prompt,
but "what can you do", "what is new" and "how do I set up a daily brief" also need the features that are not a
tool (the Schedules page's daily briefs, backup AIs, Telegram results) and what changed lately. This card is built
from CHANGELOG.md's "In short" lines, so every release teaches Delta about itself with no extra step, and it is
added to the planner's prompt only for a request about Delta (delta.core.planner.hints_for): no tokens otherwise.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from delta import __version__
from delta.config import REPO_ROOT

RELEASES = 4        # how many releases back
CARD_CHARS = 1500   # the whole card never grows past this

# A request about Delta itself: what it can do, what changed, how one of its own pages or settings works.
ABOUT = re.compile(
    r"what (?:can|could|do|all can) you\b|what(?:'s| is| are)(?: the)? new\b|your (?:features?|abilit|skills?|"
    r"capabilit|version|updates?|limits?)|new features?|latest (?:update|version|features?)|which version|"
    r"\bchangelog\b|about (?:yourself|delta)|how (?:do|can|to) (?:i|we|you) (?:use|set ?up|add|turn|enable|"
    r"switch|connect|pair|schedule)|can (?:you|delta) (?:do|help|also|even)|(?:are|were) you updated|"
    r"kya kar sakt|what else can", re.I)

WHERE = ("Where things are in the dashboard: Schedules (reminders, repeating tasks, the Daily briefs buttons: "
         "Markets, AI and tech, Gmail summary); Apps (AI models, main AI and backup AIs, connected apps); "
         "Settings > Keys (AI and search keys), Settings > Phone and PIN (pair Telegram; scheduled results "
         "arrive there), Settings > Updates and help (Update now, What's new); "
         "Memory (what Delta remembers); Tasks (history, "
         "resume). Answer questions about Delta from this card and the tool list; if something is not in either, "
         "say you are not sure rather than inventing a feature.")


def _plain(line: str) -> str:
    line = re.sub(r"\s*\(#\d+(?:,\s*#\d+)*\)", "", line)
    return re.sub(r"\*\*(.+?)\*\*", r"\1", line).strip()


def recent_changes(changelog: str, releases: int = RELEASES) -> list[tuple[str, list[str]]]:
    """[(version, its "In short" lines)] for the newest released versions that have such lines."""
    out: list[tuple[str, list[str]]] = []
    for m in re.finditer(r"(?m)^## (\d+\.\d+\.\d+)[^\n]*\n(.*?)(?=^## |\Z)", changelog, flags=re.S):
        head = m.group(2).split("\n### ", 1)[0]
        lines = [_plain(" ".join(b.split())) for b in re.findall(r"(?m)^- (.+(?:\n  .+)*)", head)]
        if lines:
            out.append((m.group(1), lines))
        if len(out) >= releases:
            break
    return out


@lru_cache(maxsize=1)
def about_delta(root: Path = REPO_ROOT) -> str:
    """The card. Without a changelog (an unusual install) it still gives the version and where things are."""
    try:
        changes = recent_changes((root / "CHANGELOG.md").read_text(encoding="utf-8"))
    except OSError:
        changes = []
    parts = [f"ABOUT DELTA (you): version {__version__}."]
    if changes:
        parts.append("What recent versions added:")
        parts += [f"- {version}: " + " ".join(lines) for version, lines in changes]
    card = "\n".join(parts)
    room = CARD_CHARS - len(WHERE) - 1
    if len(card) > room:
        card = card[:room].rsplit(" ", 1)[0] + "…"
    return card + "\n" + WHERE
