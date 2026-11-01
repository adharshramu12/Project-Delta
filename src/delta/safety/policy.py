"""PolicyEngine = hard safety floor ∩ the user's boundaries.

Hard floor (not configurable): SYSTEM-risk actions are denied, DESTRUCTIVE and EXTERNAL
actions always need a human. The user's `boundaries.yaml` can only make Delta stricter
than that, or relax ordinary writes inside allowed folders.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel

from delta.config import Boundaries
from delta.tools.base import Risk, Tool


class Verdict(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str
    risk: Risk


def norm_path(p: str | Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(str(p)))
    return Path(os.path.normcase(os.path.abspath(expanded)))


def _under(path: Path, roots: list[Path]) -> bool:
    return any(path == r or path.is_relative_to(r) for r in roots)


def _host(domain_or_url: str) -> str:
    d = domain_or_url if "://" in domain_or_url else f"https://{domain_or_url}"
    return (urlparse(d).hostname or "").lower()


def _domain_match(host: str, patterns: list[str]) -> bool:
    return any(host == p.lower() or host.endswith("." + p.lower()) for p in patterns)


class PolicyEngine:
    def __init__(self, boundaries: Boundaries, now: Callable[[], datetime] = datetime.now) -> None:
        self.b = boundaries
        self.now = now
        self.allowed = [norm_path(p) for p in boundaries.allowed_paths]
        self.denied = [norm_path(p) for p in boundaries.denied_paths]

    def evaluate(self, tool: Tool[BaseModel], args: BaseModel) -> Decision:
        risk = tool.risk_for(args)

        def d(v: Verdict, why: str) -> Decision:
            return Decision(v, why, risk)

        if tool.name in self.b.denied_tools:
            return d(Verdict.DENY, f"tool '{tool.name}' is disabled in your boundaries")
        if risk == Risk.SYSTEM:
            return d(Verdict.DENY, "system/security/credential/payment actions are never allowed")

        paths = [norm_path(p) for p in tool.paths(args)]
        for p in paths:
            if _under(p, self.denied):
                return d(Verdict.DENY, f"{p} is inside a denied folder")

        app = tool.app(args)
        if app and app.lower() in {a.lower() for a in self.b.denied_apps}:
            return d(Verdict.DENY, f"app '{app}' is denied")

        for dom in tool.domains(args):
            host = _host(dom)
            if _domain_match(host, self.b.denied_domains):
                return d(Verdict.DENY, f"domain {host} is denied")
            if self.b.allowed_domains and not _domain_match(host, self.b.allowed_domains):
                return d(Verdict.DENY, f"domain {host} is not in allowed_domains")

        if risk == Risk.READ:
            return d(Verdict.ALLOW, "read-only")
        if self._quiet_hours():
            return d(Verdict.ASK, "quiet hours: autonomous actions are paused")

        autonomy = self.b.autonomy
        if risk == Risk.ACT:
            if autonomy == "ask-everything":
                return d(Verdict.ASK, "autonomy=ask-everything")
            return d(Verdict.ALLOW, "non-destructive interaction")
        if risk == Risk.WRITE:
            if autonomy == "trusted" and paths and all(_under(p, self.allowed) for p in paths):
                return d(Verdict.ALLOW, "write inside allowed folders (trusted)")
            return d(Verdict.ASK, "modifies data")
        # DESTRUCTIVE / EXTERNAL: hard floor, always a human decision
        return d(Verdict.ASK, "deletes/overwrites data" if risk == Risk.DESTRUCTIVE else "sends data outside")

    def _quiet_hours(self) -> bool:
        q = self.b.quiet_hours
        if not q.enabled:
            return False
        start, end = time.fromisoformat(q.start), time.fromisoformat(q.end)
        t = self.now().time()
        return start <= t < end if start < end else (t >= start or t < end)
