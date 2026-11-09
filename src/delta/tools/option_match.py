"""Choosing a dropdown option by its TEXT, the way a person reads it, never by where it sits in the list.

College portals list years unsorted (2027-2028 before 2008-2009), write the same thing many ways ("2026-27",
"2026 - 2027", "Odd Sem" / "ODD SEMESTER"), pad options with spaces and put a "--Select--" first. What is asked for is
matched, in order of how sure the match is:

1. the same text, ignoring case, spaces and the kind of dash;
2. the same academic year written another way (2026-27 = 2026-2027 = 2026/2027 = AY 2026-27);
3. every word asked for starts a word of the option ("odd sem" -> "Odd Semester"), or every word of the option starts
   a word asked for ("Odd" for "odd sem");
4. the option contains what is asked for.

A step that finds more than one option (and they are not the same text) is ambiguous: nothing is chosen and the
caller says which options there are, rather than taking the first. Placeholders ("--Select--", "Choose ...") never
match a request that is not itself a placeholder.
"""

from __future__ import annotations

import re
import unicodedata

DASHES = re.compile(r"[\u2010-\u2015\u2212\uFE58\uFE63\uFF0D]")
YEAR = re.compile(r"\b((?:19|20)\d\d)\s*[-/]\s*(\d{2}|\d{4})\b")
PLACEHOLDER = re.compile(r"^[-\s.*]*(select|choose|pick|none|--)\b", re.I)


def norm(text: str) -> str:
    """Lower case, one kind of dash, single spaces, no spaces around a dash or slash, no trailing colon."""
    t = unicodedata.normalize("NFKC", text or "")
    t = DASHES.sub("-", t).lower()
    t = re.sub(r"\s+", " ", t).strip().rstrip(":").strip()
    return re.sub(r"\s*([-/])\s*", r"\1", t)


def years(text: str) -> set[str]:
    """Academic years in a text, each as 'YYYY-YYYY' ('2026-27' -> '2026-2027'). A pair that is not one year apart
    (2026-2030) is not an academic year."""
    out = set()
    for a, b in YEAR.findall(norm(text)):
        end = int(a[:2] + b) if len(b) == 2 else int(b)
        if end == int(a) + 1:
            out.add(f"{a}-{end}")
    return out


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", norm(text)) if w]


def _starts(want: list[str], have: list[str]) -> bool:
    return bool(want) and all(any(h.startswith(w) for h in have) for w in want)


def match_option(options: list[str], want: str) -> tuple[int | None, list[int]]:
    """(index of the option to choose, or None; the indexes that matched at the step that decided). The index is only
    a handle on the option whose text matched; the order of the list plays no part in which one it is."""
    target = norm(want)
    if not target:
        return None, []
    placeholder_ok = bool(PLACEHOLDER.match(want.strip()))
    cands = [(i, o) for i, o in enumerate(options) if placeholder_ok or not PLACEHOLDER.match((o or "").strip())]
    want_years = years(want)
    want_words = _words(want)

    def same_year(o: str) -> bool:
        got = years(o)
        if not (want_years and got and want_years == got):
            return False
        rest_w = [w for w in _words(YEAR.sub(" ", norm(want))) if w not in ("ay", "a", "y")]
        rest_o = [w for w in _words(YEAR.sub(" ", norm(o))) if w not in ("ay", "a", "y")]
        return not rest_w or _starts(rest_w, rest_o)

    steps = [
        lambda o: norm(o) == target,
        same_year,
        lambda o: not want_years and (_starts(want_words, _words(o)) or _starts(_words(o), want_words)),
        lambda o: not want_years and target in norm(o),
    ]
    for test in steps:
        hits = [i for i, o in cands if test(o or "")]
        if len({norm(options[i]) for i in hits}) == 1:
            return hits[0], hits
        if hits:
            return None, hits
    return None, []


__all__ = ["match_option", "norm", "years"]
