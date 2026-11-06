"""Page snapshots for the model: a full listing, or only what changed since the last one.

Pure text and bookkeeping (no browser), shared by the Delta browser tools and the agent loop's
history trimming. After an action on the same page the model already holds a listing of it, so
sending the whole list again costs ~1,200 characters per step for nothing. A diff names only the
elements that appeared, disappeared or changed (with their refs), plus a title or address change.

Refs themselves are not affected: the browser manager still keeps the full current listing, so
every ref the model saw stays valid (or is reported stale) exactly as before. Lines are made by
ElementRef.line(), the same as a full listing, so field values (passwords) never appear.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

# A full listing starts with this line prefix; a diff with DIFF_PREFIX. The agent loop looks for
# them to tell an old, superseded listing from the one the latest diffs build on.
FULL_PREFIX = "Page: "
DIFF_PREFIX = "Page changes: "
FULL_EVERY = 8  # a full listing at least every this many actions, so the model never drifts
DIFF_HELP = ('After an action on the same page you may get "Page changes:" instead of the full listing: '
             'only "+ " new, "~ " changed and "- " gone elements; every other ref from the last listing '
             "still works.")


class _Line(Protocol):
    @property
    def ref(self) -> int: ...

    def line(self) -> str: ...


@dataclass
class SnapshotState:
    """What the model was last shown of the current page: ref -> listing line."""

    url: str = ""
    title: str = ""
    known: dict[int, str] = field(default_factory=dict)
    since_full: int = 0
    has_base: bool = False

    def reset(self) -> None:
        self.url, self.title, self.known, self.since_full, self.has_base = "", "", {}, 0, False


def same_page(a: str, b: str) -> bool:
    """Same document address. A different path or query is a navigation; a #fragment change is not."""
    return a.split("#", 1)[0] == b.split("#", 1)[0]


def diff_text(state: SnapshotState, title: str, url: str, shown: Sequence[_Line], on_page: set[int],
              more: int = 0) -> str:
    """The changes from what the model holds to `shown` (the elements a full listing would show now):
    "+ " new, "~ " changed, "- " gone (the agent's prompt explains the marks once, see DIFF_HELP).

    on_page: every ref still on the page; a known ref that left the top of the listing but is still
    there (scrolled past) is not "removed", since it is still valid."""
    head = f"{DIFF_PREFIX}{title} — {url}"
    notes: list[str] = []
    if title != state.title:
        notes.append(f"title was: {state.title}")
    if url != state.url:
        notes.append(f"address was: {state.url}")
    added = [e.line() for e in shown if e.ref not in state.known]
    changed = [e.line() for e in shown if e.ref in state.known and state.known[e.ref] != e.line()]
    removed = [state.known[r] for r in sorted(state.known) if r not in on_page]
    lines = [head + (f" ({'; '.join(notes)})" if notes else "")]
    lines += [f"+ {x}" for x in added] + [f"~ {x}" for x in changed] + [f"- {x}" for x in removed]
    if len(lines) == 1:
        lines.append("(no element changes; earlier refs still work)")
    if more > 0:
        lines.append(f"…[{more} more elements; browser_read lists them]")
    return "\n".join(lines)


def remember(state: SnapshotState, title: str, url: str, shown: Sequence[_Line], on_page: set[int],
             full: bool) -> None:
    """Record what the model now holds after a full listing or a diff."""
    if full:
        state.known = {e.ref: e.line() for e in shown}
        state.since_full = 0
        state.has_base = True
    else:
        state.known = {r: line for r, line in state.known.items() if r in on_page}
        state.known.update({e.ref: e.line() for e in shown})
        state.since_full += 1
    state.title, state.url = title, url


def choose(state: SnapshotState, title: str, url: str, shown: Sequence[_Line], on_page: set[int], full: str,
           more: int = 0, enabled: bool = True, every: int = FULL_EVERY) -> str:
    """The text to send: the diff when it is safe and smaller, else the full listing. Updates state."""
    use_diff = (enabled and state.has_base and same_page(state.url, url)
                and state.since_full + 1 < max(1, every))
    if use_diff:
        diff = diff_text(state, title, url, shown, on_page, more)
        if len(diff) < len(full):
            remember(state, title, url, shown, on_page, full=False)
            return diff
    remember(state, title, url, shown, on_page, full=True)
    return full


# --- history helpers (used by the agent loop's compact_history) ---------------------------------

def find_listing(content: str) -> tuple[int, int, str] | None:
    """(start, end, kind) of the page listing inside a browser action's result, kind 'full' or
    'diff'. An action result is "<what happened>" then a blank line then the listing, which runs to
    the next blank line (or the end). Only that spot is looked at, so page text that happens to
    start with "Page: " is never taken for a listing."""
    first = content.find("\n\n")
    if first < 0:
        return None
    start = first + 2
    if content.startswith(DIFF_PREFIX, start):
        kind = "diff"
    elif content.startswith(FULL_PREFIX, start):
        kind = "full"
    else:
        return None
    end = content.find("\n\n", start)
    return start, (len(content) if end < 0 else end), kind


def page_line(listing: str) -> str:
    """The header line of a listing ("Page: title — url"), for a short stub."""
    first = listing.split("\n", 1)[0]
    return first.removeprefix(FULL_PREFIX).removeprefix(DIFF_PREFIX)
