"""A slide deck as data: the outline the model writes (Markdown) -> Deck, with the limits that keep slides readable.

The outline format is small on purpose, so one model call writes a whole deck:

    # Deck title
    One line under the title (optional)

    ## Slide title
    - a short point
      - a detail under it
    | Plan | Price |        a table, for comparisons and numbers
    | Free | 0     |
    > What to say on this slide (speaker notes)

Limits are applied here, in code, not left to the model: a slide with too many points or table rows continues on
the next slide ("Title (2)"), a deck never has more than MAX_SLIDES slides, and nothing is ever dropped silently
(`Deck.notes` says what was cut).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_SLIDES = 40
MAX_POINTS = 6  # per slide; more continue on the next slide
MAX_TABLE_ROWS = 7  # body rows per slide (the header row is repeated on each continuation)
MAX_TABLE_COLS = 6
MAX_LEVEL = 2

_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_BULLET = re.compile(r"^(\s*)(?:[-*+•]|\d{1,2}[.)])\s+(.*\S)\s*$")
_NOTES = re.compile(r"^\s*(?:>\s?|(?:speaker\s+)?notes?\s*:\s*)(.*)$", re.I)


@dataclass
class Slide:
    title: str
    points: list[tuple[int, str]] = field(default_factory=list)  # (level 0..MAX_LEVEL, text)
    table: list[list[str]] = field(default_factory=list)  # first row is the header
    notes: str = ""


@dataclass
class Deck:
    title: str = ""
    subtitle: str = ""
    slides: list[Slide] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # what the limits changed, for the user

    def words(self) -> int:
        text = " ".join([self.title, self.subtitle] + [s.title + " " + " ".join(p for _, p in s.points)
                                                       for s in self.slides])
        return len(text.split())


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _plain(text: str) -> str:
    """Heading text without Markdown marks (bold is kept only inside points, where the renderer reads it)."""
    return re.sub(r"[*_`#]+", "", text).strip()


def parse_outline(text: str, title: str = "") -> Deck:
    deck = Deck(title=title.strip())
    cur: Slide | None = None
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if not line.strip() or re.fullmatch(r"\s*(?:---+|```\w*)\s*", line):
            continue
        if m := re.match(r"^#\s+(.*)$", line):
            if cur is None and not deck.slides:
                deck.title = deck.title or _plain(m.group(1))
                continue
            line = "## " + m.group(1)  # a second '# ' is a slide, not a new deck
        if m := re.match(r"^#{2,6}\s+(.*)$", line):
            cur = Slide(_plain(m.group(1)))
            deck.slides.append(cur)
            continue
        if cur is None:  # text before the first slide: the line under the title
            if not deck.subtitle:
                deck.subtitle = _plain(line)
            continue
        if line.lstrip().startswith("|"):
            if not _TABLE_SEP.match(line):
                cur.table.append(_cells(line))
            continue
        if (m := _NOTES.match(line)) and (line.lstrip().startswith(">") or not _BULLET.match(line)):
            cur.notes = (cur.notes + " " + m.group(1).strip()).strip()
            continue
        if m := _BULLET.match(line):
            level = min(MAX_LEVEL, len(m.group(1).replace("\t", "  ")) // 2)
            cur.points.append((level, m.group(2)))
        else:
            cur.points.append((0, line.strip()))
    if not deck.slides and deck.subtitle:  # plain text with no slide headings: one slide, nothing lost
        deck.slides.append(Slide(deck.title or "Overview", [(0, deck.subtitle)]))
        deck.subtitle = ""
    deck.title = deck.title or (deck.slides[0].title if deck.slides else "Presentation")
    return fit(deck)


def fit(deck: Deck) -> Deck:
    """Apply the limits: long slides continue on the next one, wide tables lose their extra columns (noted)."""
    out: list[Slide] = []
    for s in deck.slides:
        if s.table:
            width = max(len(r) for r in s.table)
            if width > MAX_TABLE_COLS:
                deck.notes.append(f"'{s.title}': the table had {width} columns; only the first {MAX_TABLE_COLS} fit.")
            cols = min(width, MAX_TABLE_COLS)
            s.table = [(r + [""] * cols)[:cols] for r in s.table]
        point_parts = [s.points[i:i + MAX_POINTS] for i in range(0, len(s.points), MAX_POINTS)] or [[]]
        head, body = (s.table[0], s.table[1:]) if s.table else ([], [])
        table_parts = [body[i:i + MAX_TABLE_ROWS] for i in range(0, len(body), MAX_TABLE_ROWS)] or [[]]
        # points first, then the table on its own slide(s) when there are both: a table needs the room
        pieces: list[tuple[list[tuple[int, str]], list[list[str]]]] = []
        if s.table and s.points:
            pieces = [(p, []) for p in point_parts] + [([], [head, *t]) for t in table_parts]
        elif s.table:
            pieces = [([], [head, *t]) for t in table_parts]
        else:
            pieces = [(p, []) for p in point_parts]
        for n, (points, table) in enumerate(pieces, 1):
            out.append(Slide(s.title if n == 1 else f"{s.title} ({n})", points, table, s.notes if n == 1 else ""))
    if len(out) > MAX_SLIDES:
        deck.notes.append(f"The deck had {len(out)} slides; only the first {MAX_SLIDES} were kept.")
        out = out[:MAX_SLIDES]
    deck.slides = out
    return deck
