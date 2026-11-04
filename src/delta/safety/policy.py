"""PolicyEngine = hard safety floor ∩ the user's boundaries.

Hard floor (not configurable): SYSTEM-risk actions (security settings, credentials, payments) are
denied, and changes that cannot be undone (a shell deleting files, Shift+Delete) always need a human.

Autonomy (the user's choice):
  ask-everything  ask before anything except reading
  ask-risky       act freely; ask before changing or sending anything
  trusted         also change things without asking when it can be undone: edit, overwrite or
                  delete files in allowed folders (old versions go to the Recycle Bin), keys and
                  ordinary commands. Still asks before sending anything to anyone.
  autopilot       trusted, and also sends messages and emails without asking.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel

from delta.config import Boundaries
from delta.core.playbooks import EMERGENCY
from delta.core.state import CURRENT_REQUEST
from delta.tools.base import Risk, Tool


class Verdict(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


MESSAGING_TOOLS = {"send_message", "chat_for_me", "send_file"}


def _help_request() -> bool:
    req = CURRENT_REQUEST.get()
    return bool(req and EMERGENCY.search(req.text))


# The user's own words saying to reach someone ("message Ravi now", "call Amma", "alert my contacts") are
# the approval: in an emergency a second tap costs time. Vague distress ("I'm not well") still asks first.
CLEAR_SEND = re.compile(r"\b(send|message|msg|text|whats\s?app|alert|call|ring|inform|notify)\b"
                        r"|\btell (?!you\b|me\b)\w+", re.I)


def _clearly_asked() -> bool:
    req = CURRENT_REQUEST.get()
    return bool(req and CLEAR_SEND.search(req.text))


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


# Part of the hard floor: no setting can unlock these (OS files, stored credentials, SSH keys).
ALWAYS_DENIED = ["%WINDIR%", "~/AppData/Roaming/Microsoft/Credentials", "~/AppData/Local/Microsoft/Credentials",
                 "~/AppData/Roaming/Microsoft/Protect", "~/.ssh", "~/.aws", "~/.gnupg"]


class PolicyEngine:
    def __init__(self, boundaries: Boundaries, now: Callable[[], datetime] = datetime.now,
                 protected: list[Path] | None = None, read_only: list[Path] | None = None) -> None:
        """protected: never touched at all (Delta's data dir: access token, audit log, settings).
        read_only: readable, never changed (Delta's own config: it must not rewrite its rules)."""
        self.b = boundaries
        self.now = now
        self.allowed = [norm_path(p) for p in boundaries.allowed_paths]
        self.denied = [norm_path(p) for p in boundaries.denied_paths]
        roots: list[str | Path] = [*ALWAYS_DENIED, *(protected or [])]
        self.protected = [norm_path(p) for p in roots]
        self.read_only = [norm_path(p) for p in read_only or []]

    @staticmethod
    def _arg_text(args: BaseModel) -> str:
        """All argument text, normalised for matching: env vars and ~ expanded, one slash style,
        Windows case rules. Shell commands and MCP arguments name paths inside free text."""
        raw = json.dumps(args.model_dump(), default=str).replace("\\\\", "\\")
        raw = re.sub(r"\$env:(\w+)", lambda m: os.environ.get(m.group(1), m.group(0)), raw, flags=re.I)
        raw = os.path.expandvars(raw)  # %USERPROFILE%, $HOME
        raw = re.sub(r"(?<![\w.])~(?=[\\/])", lambda _: str(Path.home()), raw)
        return os.path.normcase(raw.replace("/", "\\"))

    def _mentions(self, args: BaseModel, roots: list[Path]) -> Path | None:
        """A root folder named anywhere in the arguments (catches `Get-Content <path>` in a shell)."""
        text = self._arg_text(args)
        return next((r for r in roots if len(str(r)) > 3 and str(r) in text), None)

    def evaluate(self, tool: Tool[BaseModel], args: BaseModel) -> Decision:
        risk = tool.risk_for(args)

        def d(v: Verdict, why: str) -> Decision:
            return Decision(v, why, risk)

        if tool.name in self.b.denied_tools:
            return d(Verdict.DENY, f"tool '{tool.name}' is disabled in your boundaries")
        if risk == Risk.SYSTEM:
            return d(Verdict.DENY, "system/security/credential/payment actions are never allowed")
        if getattr(tool, "always_ask", False) and not _clearly_asked():  # e.g. alerting emergency contacts
            return d(Verdict.ASK, "always confirmed with you, whatever the autonomy level")
        if tool.name in MESSAGING_TOOLS and _help_request() and not _clearly_asked():
            # Found in a live test: in autopilot the agent reached the contact through the ordinary
            # send_message tool and skipped the yes. Any message during a help request is confirmed.
            return d(Verdict.ASK, "a message during a help/emergency request is always confirmed with you")

        paths = [norm_path(p) for p in tool.paths(args)]
        hit = next((p for p in paths if _under(p, self.protected)), None) or self._mentions(args, self.protected)
        if hit:
            return d(Verdict.DENY, f"{hit} is protected: Delta never touches it")
        if risk != Risk.READ:
            ro = next((p for p in paths if _under(p, self.read_only)), None) or self._mentions(args, self.read_only)
            if ro:
                return d(Verdict.DENY, f"{ro} is read-only: Delta cannot change its own configuration")
        denied = next((p for p in paths if _under(p, self.denied)), None) or self._mentions(args, self.denied)
        if denied:
            return d(Verdict.DENY, f"{denied} is inside a folder you blocked")

        app = tool.app(args)
        if app and app.lower() in {a.lower() for a in self.b.denied_apps}:
            return d(Verdict.DENY, f"app '{app}' is denied")

        if self.b.denied_domains:
            text = self._arg_text(args)
            for blocked in self.b.denied_domains:
                b = blocked.lower().strip()
                if b and re.search(rf"(^|[^\w-]){re.escape(b)}($|[^\w-])", text):
                    return d(Verdict.DENY, f"website {b} is blocked")
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
        trusted = autonomy in ("trusted", "autopilot")
        in_allowed = bool(paths) and all(_under(p, self.allowed) for p in paths)
        if risk == Risk.ACT:
            if autonomy == "ask-everything":
                return d(Verdict.ASK, "autonomy=ask-everything")
            return d(Verdict.ALLOW, "non-destructive interaction")
        if risk == Risk.WRITE:
            if trusted and (in_allowed or not paths):
                return d(Verdict.ALLOW, f"change allowed without asking ({autonomy})")
            return d(Verdict.ASK, "modifies data" if not paths else "changes files outside your allowed folders")
        if risk == Risk.DESTRUCTIVE:
            if trusted and in_allowed and tool.undoable(args):
                return d(Verdict.ALLOW, f"undoable: the old version goes to the Recycle Bin ({autonomy})")
            return d(Verdict.ASK, "deletes/overwrites data and cannot be undone"
                     if not tool.undoable(args) else "deletes/overwrites data outside your allowed folders")
        # EXTERNAL: reaches other people or services
        if autonomy == "autopilot":
            return d(Verdict.ALLOW, "sends without asking (autopilot)")
        return d(Verdict.ASK, "sends data outside")

    def _quiet_hours(self) -> bool:
        q = self.b.quiet_hours
        if not q.enabled:
            return False
        start, end = time.fromisoformat(q.start), time.fromisoformat(q.end)
        t = self.now().time()
        return start <= t < end if start < end else (t >= start or t < end)
