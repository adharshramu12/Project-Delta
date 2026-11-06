"""Remote Guard: what stands between a stolen phone or Telegram account and the user's PC.

Threat: someone gets the owner's Telegram (SIM swap, phishing, an unlocked phone). Delta cannot tell
them apart from the owner, so sensitive remote actions need a second factor only the owner knows:

- A PIN (4-8 digits), set on the PC. Stored only as a salted scrypt hash in Windows Credential
  Manager; never in a file, a log or a chat. A correct PIN unlocks sensitive actions for 15 minutes.
- Secure by default: with no PIN set, sensitive actions are refused (with how to set one).
- Brute force: 5 wrong PINs in a row lock ALL remote control. Only a correct PIN or an unlock on the PC
  resets the count, and the count is saved in the data folder, so restarting Delta does not clear it.
  Only someone at the PC can lift a lock, never the phone. The owner can also lock it from the phone at
  any time (/lock).
- Every unlock, failure, lock and sensitive action is audit-logged.

Enforced centrally, not per channel: `Container.submit` refuses remote tasks while locked,
`ToolExecutor.execute` asks `tool_refusal()` before any tool runs for a remote request, and
`ApprovalBroker.answer()` asks `approval_refusal()` before accepting an approval button from a phone.
A channel is remote unless it is one of LOCAL_CHANNELS, so a new chat channel is covered by default.

State that must survive a restart (locked, why, wrong PINs so far) lives in remote_guard.json in Delta's
data folder, written atomically (a temporary file, flushed, then renamed over the old one)."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

PIN_SECRET = "remote_pin"  # the hash's name in the credential store
UNLOCK_S = 15 * 60
MAX_FAILURES = 5  # wrong PINs in a row (no time window: waiting never resets the count)
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}

# What needs the PIN. Everything else (questions, music, apps opening) stays one message away.
SENSITIVE = {
    "camera": "take a webcam photo", "screen": "take a screenshot", "close_app": "close an app",
    "send_file": "send a file from the PC", "keys": "change an API key", "autopilot": "turn on Autopilot",
    "unlock": "unlock sensitive actions",
    "schedule": "create, change or turn on a scheduled task",
    "browser": "change which browser Delta uses",
    "signin": "open Delta's private browser on the PC for signing in",
}


# Channels that are this PC itself (terminal, dashboard/API). Every other channel (telegram, slack, and any
# channel added later) is remote: fail closed.
LOCAL_CHANNELS = frozenset({"cli", "api", "dashboard"})
# Approver names that are not people on a channel (ApprovalBroker): never remote.
SYSTEM_APPROVERS = frozenset({"timeout", "shutdown"})

# Tools that need the PIN whenever the request came from a remote channel: they show what is on the PC
# (screen, webcam, windows, files, private messages, clipboard) or run arbitrary code that could.
REMOTE_PIN_TOOLS = {
    "look_at_screen": "see the screen", "look_with_marks": "see the screen",
    "read_screen_text": "read the screen", "click_text": "read the screen to find what to click",
    "take_screenshot": "take a screenshot", "read_window": "read a window",
    "camera_photo": "take a webcam photo", "read_file": "read a file on the PC", "send_file": "send a file from the PC",
    "read_messages": "read your messages", "run_shell": "run a command on the PC",
    "run_python": "run code on the PC",
    # a schedule runs later, unattended: making one or turning it back on from a phone needs the PIN
    "schedule_task": "create a scheduled task", "resume_schedule": "turn a scheduled task back on",
    # opens a browser window on the PC for whoever is there to sign in to (delta.tools.browser_switch)
    "open_signin_browser": "open Delta's private browser on the PC",
}
# Read tools that need the PIN remotely only when they look outside the allowed folders.
REMOTE_PIN_OUTSIDE_ALLOWED = {"list_dir": "list a folder", "search_files": "search files",
                              "path_info": "look at a file"}
# Approving these from a phone needs the PIN (denying never does).
PIN_TO_APPROVE_RISKS = {"destructive", "external", "system"}


def is_remote(channel: str | None) -> bool:
    return (channel or "").lower() not in LOCAL_CHANNELS


def needs_remote_pin(tool: str, outside_allowed: bool = False) -> str | None:
    """What a remote request would do with this tool, if that needs the PIN; else None."""
    if tool in REMOTE_PIN_TOOLS:
        return REMOTE_PIN_TOOLS[tool]
    if "clipboard" in tool.lower():
        return "read the clipboard"
    if outside_allowed and tool in REMOTE_PIN_OUTSIDE_ALLOWED:
        return REMOTE_PIN_OUTSIDE_ALLOWED[tool] + " outside your allowed folders"
    return None


def pin_hint(guard: Any, channel: str, then: str = "ask again") -> str:
    if not guard.has_pin:
        return "Set a remote PIN on the PC first (Delta > Settings > Remote security)."
    if channel == "telegram":
        return f"Send /unlock, enter the PIN, then {then}."
    return f"{channel.capitalize()} cannot take the PIN: do this from the PC or from Telegram after /unlock."


def tool_refusal(guard: Any, channel: str, user: str, tool: str, outside_allowed: bool = False,
                 extra: str | None = None) -> str | None:
    """None when a tool call from this request may run; otherwise why not (for the model and the user).
    Called by ToolExecutor for every call, so no tool and no channel can skip it.
    extra: what this particular call would do when the tool itself says it needs the PIN (e.g. set_browser
    saving a setting); it only ever adds a PIN requirement, never removes one."""
    if guard is None or not is_remote(channel):
        return None
    if guard.locked:
        guard.audit.record("guard.blocked", tool=tool, channel=channel, user=user, why="locked")
        return guard.locked_message()
    what = needs_remote_pin(tool, outside_allowed) or extra
    if what is None:
        return None
    if guard.is_unlocked(user):
        guard.audit.record("guard.sensitive", action=tool, channel=channel, user=user)
        return None
    guard.audit.record("guard.blocked", tool=tool, channel=channel, user=user, why="needs_pin")
    return f"To {what} from {channel}, the owner's PIN is needed. {pin_hint(guard, channel)}"


def approver_channel(by: str) -> tuple[str, str]:
    """'telegram:123' -> ('telegram', '123'); 'dashboard' -> ('dashboard', '')."""
    channel, _, user = by.partition(":")
    return channel.lower(), user


def approval_refusal(guard: Any, by: str, approved: bool, tool: str, risk: str) -> tuple[bool, str] | None:
    """None when an approval answer may be accepted as given. Otherwise (deny_instead, message):
    deny_instead=True: remote control is locked, so the answer counts as a "no" (logged);
    deny_instead=False: approving this from a phone needs the PIN first; the request stays open."""
    channel, user = approver_channel(by)
    if guard is None or channel in SYSTEM_APPROVERS or not is_remote(channel):
        return None
    if guard.locked:
        guard.audit.record("guard.approval_blocked", by=by, tool=tool, risk=risk, answered=approved,
                           why="locked")
        return True, guard.locked_message()
    if approved and (risk in PIN_TO_APPROVE_RISKS or needs_remote_pin(tool)) and not guard.is_unlocked(user):
        guard.audit.record("guard.approval_blocked", by=by, tool=tool, risk=risk, answered=approved,
                           why="needs_pin")
        return False, ("Approving this from your phone needs your PIN. "
                       + pin_hint(guard, channel, "tap Approve again"))
    return None


def hash_pin(pin: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, **SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_pin(pin: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(salt_hex), **SCRYPT).hex()
    return hmac.compare_digest(candidate, digest_hex)  # constant time: no timing leaks


def valid_pin(pin: str) -> bool:
    return pin.isdigit() and 4 <= len(pin) <= 8 and len(set(pin)) > 1  # not 0000 / 1111


class RemoteGuard:
    def __init__(self, data_dir: Path, audit: Any, alert: Callable[[str, str], None] | None = None,
                 get_secret: Callable[[str], str | None] | None = None,
                 set_secret: Callable[[str, str], None] | None = None,
                 now: Callable[[], float] = time.monotonic) -> None:
        from delta.system import secrets as store

        self.path = data_dir / "remote_guard.json"
        self.audit = audit
        self.alert = alert or (lambda title, text: None)
        self._get = get_secret or store.get_secret
        self._set = set_secret or store.set_secret
        self.now = now
        self._lock = threading.Lock()
        self._unlocked_until: dict[str, float] = {}
        state = self._load()
        self.locked: bool = bool(state.get("locked"))
        self.locked_reason: str = str(state.get("reason", ""))
        # Wrong PINs since the last correct one (or the last unlock on the PC). Saved, so a restart, a crash
        # or a reboot never hands an attacker a fresh set of guesses. Older files have no count: 0.
        try:
            self.failures: int = max(0, int(state.get("failures", 0)))
        except (TypeError, ValueError):
            self.failures = 0

    # --- persistence ---------------------------------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        try:
            return dict(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        """Atomic: a crash leaves either the old file or the new one, never half of one."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        data = json.dumps({"locked": self.locked, "reason": self.locked_reason, "failures": self.failures})
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()

    # --- PIN (set on the PC only) ----------------------------------------------------------------------
    @property
    def has_pin(self) -> bool:
        return bool(self._get(PIN_SECRET))

    def set_pin(self, pin: str, by: str) -> None:
        if not valid_pin(pin):
            raise ValueError("Use 4 to 8 digits, not all the same.")
        self._set(PIN_SECRET, hash_pin(pin))
        with self._lock:
            self._unlocked_until.clear()  # a new PIN ends every open unlock
        self.audit.record("guard.pin_set", by=by)

    # --- remote side -----------------------------------------------------------------------------------
    def is_unlocked(self, user: str) -> bool:
        return not self.locked and self._unlocked_until.get(user, 0) > self.now()

    def try_pin(self, user: str, pin: str) -> tuple[bool, str]:
        """Check a PIN sent from the phone. Wrong too often: remote control locks until the PC unlocks it."""
        if self.locked:
            return False, self.locked_message()
        stored = self._get(PIN_SECRET)
        if not stored:
            return False, "No PIN is set. On the PC: Delta → Settings → Remote security → Set PIN."
        if check_pin(pin.strip(), stored):
            with self._lock:
                self._unlocked_until[user] = self.now() + UNLOCK_S
                if self.failures:
                    self.failures = 0
                    self._save()
            self.audit.record("guard.unlocked", user=user, minutes=UNLOCK_S // 60)
            return True, f"Unlocked for {UNLOCK_S // 60} minutes."
        with self._lock:
            self.failures += 1
            left = MAX_FAILURES - self.failures
            self._save()  # before answering: a restart right after a wrong PIN must not forget it
        self.audit.record("guard.pin_failed", user=user, attempts_left=max(left, 0))
        if left <= 0:
            self.lock(f"{MAX_FAILURES} wrong PINs from Telegram", by=f"guard:{user}")
            return False, self.locked_message()
        return False, f"Wrong PIN. {left} tries left before remote control locks."

    def lock(self, reason: str, by: str) -> None:
        with self._lock:
            self.locked, self.locked_reason = True, reason
            self._unlocked_until.clear()
            self._save()
        self.audit.record("guard.locked", reason=reason, by=by)
        self.alert("Delta remote control locked", f"{reason}. Unlock it in Delta on this PC.")

    def unlock_from_pc(self, by: str) -> None:
        """Only the PC (dashboard) calls this: a phone can never undo a lock."""
        with self._lock:
            self.locked, self.locked_reason = False, ""
            self.failures = 0
            self._save()
        self.audit.record("guard.lock_lifted", by=by)

    def locked_message(self) -> str:
        return f"🔒 Remote control is locked ({self.locked_reason}). Unlock it in Delta on the PC."

    def status(self) -> dict[str, Any]:
        return {"has_pin": self.has_pin, "locked": self.locked, "reason": self.locked_reason,
                "unlock_minutes": UNLOCK_S // 60, "max_failures": MAX_FAILURES, "failures": self.failures,
                "sensitive": SENSITIVE}
