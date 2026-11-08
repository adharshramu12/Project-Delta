"""Version numbers as Semantic Versioning 2.0.0 defines them (https://semver.org): 1.2.3, 1.2.3-beta.2, v1.2.3.

Only what Delta needs to decide "is that one newer than mine": parse, then compare by precedence. Build metadata
(+...) is ignored, as the spec says. A text that is not a version gives None, never an exception, so a strange tag
on GitHub can never crash Delta or count as an update."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SEMVER = re.compile(
    r"^[vV]?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    pre: tuple[str, ...] = ()

    @property
    def prerelease(self) -> bool:
        return bool(self.pre)

    def key(self) -> tuple[int, int, int, tuple[tuple[int, int | str], ...]]:
        """Precedence: a release ranks above its own pre-releases; pre-release identifiers compare one by one,
        numbers numerically and below words, and a longer list wins when all before are equal."""
        if not self.pre:
            return (self.major, self.minor, self.patch, ((2, 0),))
        ids: list[tuple[int, int | str]] = [(0, int(p)) if p.isdigit() else (1, p) for p in self.pre]
        return (self.major, self.minor, self.patch, tuple(ids))

    def __lt__(self, other: Version) -> bool:
        return self.key() < other.key()

    def __gt__(self, other: Version) -> bool:
        return self.key() > other.key()

    def __le__(self, other: Version) -> bool:
        return self.key() <= other.key()

    def __ge__(self, other: Version) -> bool:
        return self.key() >= other.key()

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}" + (f"-{'.'.join(self.pre)}" if self.pre else "")


def parse(text: str | None) -> Version | None:
    m = _SEMVER.match((text or "").strip())
    if not m:
        return None
    pre = tuple(m.group(4).split(".")) if m.group(4) else ()
    return Version(int(m.group(1)), int(m.group(2)), int(m.group(3)), pre)


def is_newer(candidate: str | None, current: str | None) -> bool:
    """True only when both are versions and `candidate` ranks above `current`."""
    a, b = parse(candidate), parse(current)
    return a is not None and b is not None and a > b
