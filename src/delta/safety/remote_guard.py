"""Remote Guard: what stands between a stolen phone or Telegram account and the user's PC.

Threat: someone gets the owner's Telegram (SIM swap, phishing, an unlocked phone). Delta cannot tell
them apart from the owner, so sensitive remote actions need a second factor only the owner knows:

- A PIN (4-8 digits), set on the PC. Stored only as a salted scrypt hash in Windows Credential
  Manager; never in a file, a log or a chat. A correct PIN unlocks sensitive actions for 15 minutes.
- Secure by default: with no PIN set, sensitive actions are refused (with how to set one).
- Brute force: 5 wrong PINs lock ALL remote control. Only someone at the PC can unlock it, never the
  phone. The owner can also lock it from the phone at any time (/lock).
- Every unlock, failure, lock and sensitive action is audit-logged.

State that must survive a restart (locked, why) lives in remote_guard.json in Delta's data folder."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

PIN_SECRET = "remote_pin"  # the hash's name in the credential store
UNLOCK_S = 15 * 60
MAX_FAILURES = 5
FAILURE_WINDOW_S = 15 * 60
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}

# What needs the PIN. Everything else (questions, music, apps opening) stays one message away.
SENSITIVE = {
    "camera": "take a webcam photo", "screen": "take a screenshot", "close_app": "close an app",
    "send_file": "send a file from the PC", "keys": "change an API key", "autopilot": "turn on Autopilot",
    "unlock": "unlock sensitive actions",
}


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
        self._failures: list[float] = []
        state = self._load()
        self.locked: bool = bool(state.get("locked"))
        self.locked_reason: str = str(state.get("reason", ""))

    # --- persistence ---------------------------------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        try:
            return dict(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"locked": self.locked, "reason": self.locked_reason}), encoding="utf-8")
        tmp.replace(self.path)

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
                self._failures.clear()
                self._unlocked_until[user] = self.now() + UNLOCK_S
            self.audit.record("guard.unlocked", user=user, minutes=UNLOCK_S // 60)
            return True, f"Unlocked for {UNLOCK_S // 60} minutes."
        with self._lock:
            t = self.now()
            self._failures = [f for f in self._failures if t - f < FAILURE_WINDOW_S] + [t]
            left = MAX_FAILURES - len(self._failures)
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
            self._failures.clear()
            self._save()
        self.audit.record("guard.lock_lifted", by=by)

    def locked_message(self) -> str:
        return f"🔒 Remote control is locked ({self.locked_reason}). Unlock it in Delta on the PC."

    def status(self) -> dict[str, Any]:
        return {"has_pin": self.has_pin, "locked": self.locked, "reason": self.locked_reason,
                "unlock_minutes": UNLOCK_S // 60, "max_failures": MAX_FAILURES, "sensitive": SENSITIVE}
