"""What is new since the last look: items compared by their identity (the recipe's key fields, else the whole item).

    new       an identity that was not there last time
    changed   same identity, different content (only when the recipe names key fields)
    removed   gone since last time (counted, not announced: portals often drop old items)

Pure functions, no I/O, so the rules are easy to test.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from delta.watchers.recipe import MAX_FIELD, MAX_FIELDS, MAX_ITEMS


def normalize(item: dict[str, Any]) -> dict[str, str]:
    """Bounded, plain-text fields: at most MAX_FIELDS fields of MAX_FIELD characters, whitespace collapsed."""
    out: dict[str, str] = {}
    for k, v in list(item.items())[:MAX_FIELDS]:
        text = " ".join(str(v if v is not None else "").split())[:MAX_FIELD]
        out[str(k)[:30]] = text
    return out


def _digest(values: list[str]) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def identity(item: dict[str, str], key: list[str]) -> str:
    """The item's identity: its key fields when the recipe names them (and they are filled), else everything."""
    if key and any(item.get(k) for k in key):
        return _digest([item.get(k, "").lower() for k in key])
    return _digest([f"{k}={v.lower()}" for k, v in sorted(item.items())])


def content(item: dict[str, str]) -> str:
    return _digest([f"{k}={v}" for k, v in sorted(item.items())])


def clean_items(raw: list[Any]) -> list[dict[str, str]]:
    items = [normalize(x) for x in raw if isinstance(x, dict)]
    return [x for x in items if any(x.values())][:MAX_ITEMS]


@dataclass
class SectionDiff:
    name: str
    new: list[dict[str, str]] = field(default_factory=list)
    changed: list[dict[str, str]] = field(default_factory=list)
    removed: int = 0
    total: int = 0
    first: bool = False  # no earlier snapshot: everything is the starting point, nothing is "new"

    @property
    def any(self) -> bool:
        return bool(self.new or self.changed)


def diff(name: str, before: list[dict[str, str]] | None, after: list[dict[str, str]], key: list[str]) -> SectionDiff:
    if before is None:
        return SectionDiff(name, total=len(after), first=True)
    old = {identity(x, key): content(x) for x in before}
    seen: set[str] = set()
    out = SectionDiff(name, total=len(after))
    for item in after:
        ident = identity(item, key)
        if ident in seen:
            continue
        seen.add(ident)
        if ident not in old:
            out.new.append(item)
        elif key and old[ident] != content(item):
            out.changed.append(item)
    out.removed = len(set(old) - seen)
    return out


def line(item: dict[str, str], limit: int = 160) -> str:
    """One item as a short line for a notification: its values, in order, joined."""
    text = ", ".join(v for v in item.values() if v)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
