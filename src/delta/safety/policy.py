"""PolicyEngine = hard safety floor ∩ the user's boundaries.

Hard floor (not configurable): SYSTEM-risk actions (security settings, credentials, payments) are
denied, and changes that cannot be undone (a shell deleting files, Shift+Delete) always need a human.

Autonomy (the user's choice):
  ask-everything  ask before anything except reading
  ask-risky       act freely; ask before changing or sending anything
  trusted         also change things without asking when it can be undone: edit, overwrite or
                  delete files in allowed folders (old versions go to the Recycle Bin), keys and
                  ordinary commands. Still asks before sending anything to anyone.
  autopilot       trusted, and also sends messages and emails without asking. Still asks before:
                  deleting anything that cannot be undone, changing files outside the allowed folders,
                  typing, keys or clicks in a terminal, the Run box or Delta's own window, alerts to
                  emergency contacts the user did not clearly ask for, and, once a task has read outside
                  content (a web page, file, message or the screen), sending, deleting, running code,
                  saving to memory, or opening an address that carries a long encoded value.
                  Unlike the other levels, after such a read it opens plain addresses on new sites and
                  presses Enter / Alt+F4 in ordinary apps without asking (ToolExecutor, ExfilGuard).

Every level: denied tools, apps and sites, secret files, protected and read-only paths stay denied, and
Remote Guard (PIN, lock) applies on top. "Allow for this task" on an approval covers the same tool on the
same kind of target until the task ends, in memory only (ToolExecutor.task_grant_key).
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


# Part of the hard floor: no setting can unlock these, for reading or writing. OS files, Windows
# credential and DPAPI key stores, SSH/cloud/CLI credentials, browser profiles (cookies, saved passwords),
# AI coding CLIs' sign-ins and messenger sessions: a prompt-injected task must never be able to read them.
ALWAYS_DENIED = [
    "%WINDIR%",
    # Windows Credential Manager, DPAPI master keys, certificate private keys, Web Credentials vault
    "~/AppData/Roaming/Microsoft/Credentials", "~/AppData/Local/Microsoft/Credentials",
    "~/AppData/Roaming/Microsoft/Protect", "~/AppData/Roaming/Microsoft/Crypto",
    "~/AppData/Local/Microsoft/Vault", "~/AppData/Roaming/Microsoft/Vault",
    # keys and developer credentials
    "~/.ssh", "~/.aws", "~/.gnupg", "~/.azure", "~/.kube", "~/.docker", "~/.config/gh",
    "~/AppData/Roaming/GitHub CLI", "~/.git-credentials", "~/.netrc", "~/_netrc", "~/.pypirc", "~/.npmrc",
    "~/.config/gcloud", "~/AppData/Roaming/gcloud",
    # AI coding CLIs (sign-in tokens and API keys)
    "~/.codex", "~/.claude", "~/.claude.json", "~/.gemini", "~/.config/claude",
    # browser profiles: cookies, saved passwords, autofill
    "~/AppData/Local/Google/Chrome/User Data", "~/AppData/Local/Microsoft/Edge/User Data",
    "~/AppData/Local/BraveSoftware/Brave-Browser/User Data", "~/AppData/Local/Vivaldi/User Data",
    "~/AppData/Roaming/Opera Software", "~/AppData/Roaming/Mozilla/Firefox/Profiles",
    # messenger sessions
    "~/AppData/Roaming/Telegram Desktop/tdata",
]
# Secret files wherever they are: env files, private keys, password-manager vaults and certificate
# stores, browser cookie and password databases. Matched on the file name of declared paths, and on
# path-like mentions (after a backslash) in argument text such as a shell command. URLs are left out:
# a web page called /cookies is not a cookie store.
_SECRET_NAME = (r"(\.env(\.(?!example\b|sample\b|template\b|dist\b|defaults?\b)[\w-]+)?"
                r"|id_(rsa|dsa|ecdsa|ed25519)"
                r"|[^\\/\s\"']+\.(kdbx|kdb|opvault|pfx|p12|ppk)"
                r"|login data( for account)?|cookies|web data|key[34]\.db|logins\.json|cookies\.sqlite)")
_SECRET_FILE = re.compile(_SECRET_NAME, re.I)
# ...followed by the end of that path: a quote, separator, end of text, or a space then a flag/next path.
_SECRET_MENTION = re.compile(r"\\" + _SECRET_NAME + r"(?=$|\\\"|[\"',;)|>]|\s+[-|>;]|\s+[a-z]:\\|\s+\\)", re.I)
_URL = re.compile(r"[a-z][a-z0-9+.-]*:\\\\[^\s\"']*", re.I)


def denied_file(path: str | Path) -> str | None:
    """The secret file a path points at (by its file name), if any."""
    name = os.path.basename(str(path).replace("\\", "/"))
    return name if _SECRET_FILE.fullmatch(name) else None


def denied_mention(text: str) -> str | None:
    """A secret file named by path in free argument text (normalised by PolicyEngine._arg_text)."""
    m = _SECRET_MENTION.search(_URL.sub(" ", text))
    return m.group(1) if m else None


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
        if getattr(tool, "approve_each", False):  # every email: shown in full and sent only on a human yes
            return d(Verdict.ASK, "every email is shown to you in full and sent only when you approve it")
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
        secret = next((f for p in paths if (f := denied_file(p))), None) or denied_mention(self._arg_text(args))
        if secret:
            return d(Verdict.DENY, f"'{secret}' is a secrets file (keys, passwords or cookies): Delta never touches it")
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
