"""Tamper-evident audit log: JSON lines where every record carries the SHA-256 of the previous
one. Editing or deleting any past line breaks the chain, which `verify()` detects.

Keyed (HMAC) chain: a plain hash chain can be recomputed by anyone who can edit the file. When Delta has an
audit key (a random 32-byte key kept in Windows Credential Manager, never in a file), every new record also
carries `mac` = HMAC-SHA256(key, previous mac + this record's hash). Rewriting the log then needs the key as
well. The key's entry also records when signing started, so stripping the `mac` fields to pass the log off as
an old unsigned one is caught too. Records written before the key existed keep their plain chain and still
verify. What this cannot catch: someone who can read the user's Credential Manager (malware running as the
user), or lines cut off the very end of the file (there is nothing after them to break).

The daemon and the CLI can both write the same file, so each write takes an OS file lock and chains to
the file's real last line, never to a hash cached in this process."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS = "0" * 64

if sys.platform == "win32":
    import msvcrt

    def _lock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # retries for ~10 s, then raises

    def _unlock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


KEY_SECRET = "audit_hmac_key"  # in the credential store: {"key": base64, "since": ISO time signing began}


def _digest(record: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, default=str).encode()).hexdigest()


def _mac(key: bytes, prev_mac: str, digest: str) -> str:
    return hmac.new(key, f"{prev_mac}:{digest}".encode(), hashlib.sha256).hexdigest()


class AuditKey:
    """The secret that makes the chain keyed, and when it started signing (ISO time, UTC)."""

    def __init__(self, key: bytes, since: str) -> None:
        self.key, self.since = key, since

    def dump(self) -> str:
        return json.dumps({"key": base64.b64encode(self.key).decode("ascii"), "since": self.since})

    @classmethod
    def parse(cls, raw: str | None) -> AuditKey | None:
        try:
            data = json.loads(raw or "")
            key = base64.b64decode(data["key"])
            return cls(key, str(data["since"])) if len(key) >= 16 else None
        except (ValueError, KeyError, TypeError):
            return None


def load_or_create_key(get: Any = None, put: Any = None, create: bool = True) -> AuditKey | None:
    """The audit key from the credential store, created on first use (create=False: only read it, as
    `delta audit verify` does). None when there is no working store (the log then keeps its plain hash
    chain, as before 0.2.0)."""
    import secrets as _secrets

    if get is None or put is None:
        from delta.system import secrets as store

        get, put = get or store.get_secret, put or store.set_secret
    try:
        found = AuditKey.parse(get(KEY_SECRET))
        if found is not None or not create:
            return found
        new = AuditKey(_secrets.token_bytes(32), datetime.now(UTC).isoformat())
        put(KEY_SECRET, new.dump())
        return AuditKey.parse(get(KEY_SECRET)) or new
    except Exception:  # no keyring backend (Linux dev box), Credential Manager unavailable
        return None


class VerifyReport:
    def __init__(self, ok: bool, records: int, signed: int, line: int = 0, reason: str = "") -> None:
        self.ok, self.records, self.signed, self.line, self.reason = ok, records, signed, line, reason

    def __bool__(self) -> bool:
        return self.ok

    def text(self) -> str:
        if not self.ok:
            return f"AUDIT CHAIN BROKEN at line {self.line}: {self.reason}"
        keyed = f", {self.signed} signed with the audit key" if self.signed else ", none signed with the audit key"
        return f"audit chain intact ({self.records} records{keyed})"


class AuditLog:
    def __init__(self, path: Path, key: AuditKey | None = None,
                 key_loader: Callable[[], AuditKey | None] | None = None) -> None:
        self.path = path
        self._key = key
        self._key_loader = key_loader  # read on first use: keeps the credential store out of start-up
        self._lock = threading.Lock()
        self._lock_path = path.with_name(path.name + ".lock")

    @property
    def key(self) -> AuditKey | None:
        if self._key_loader is not None:
            loader, self._key_loader = self._key_loader, None
            self._key = loader()
        return self._key

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT)
        try:
            _lock(fd)
            try:
                yield
            finally:
                _unlock(fd)
        finally:
            os.close(fd)

    def _last(self) -> dict[str, Any]:
        """The file's last record, read from the end so it stays cheap as the log grows.

        A last line that is not a record (a write cut off by a power cut or a full disk) is skipped: the new record
        chains to the last real one. Nothing is rewritten, so `verify` still reports the broken line."""
        if not self.path.exists():
            return {}
        with self.path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            chunk = 4096
            while True:
                start = max(0, end - chunk)
                f.seek(start)
                lines = [ln for ln in f.read(end - start).splitlines() if ln.strip()]
                whole = lines if start == 0 else lines[1:]  # the first line of a chunk may be cut
                for line in reversed(whole):
                    with contextlib.suppress(ValueError):
                        rec = json.loads(line)
                        if isinstance(rec, dict):
                            return rec
                if start == 0:
                    return {}
                chunk *= 4

    def _ends_cut(self) -> bool:
        """True when the file does not end with a newline (a write was cut off): the next record starts a new line."""
        with contextlib.suppress(OSError), self.path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                return False
            f.seek(-1, os.SEEK_END)
            return f.read(1) != b"\n"
        return False

    def _last_hash(self) -> str:
        return str(self._last().get("hash", GENESIS))

    def record(self, event: str, **data: Any) -> None:
        with self._lock, self._file_lock():
            last = self._last()
            rec: dict[str, Any] = {"ts": datetime.now(UTC).isoformat(), "event": event, "data": data,
                                   "prev": str(last.get("hash", GENESIS))}
            rec["hash"] = _digest(rec)
            key = self.key
            if key is not None:
                rec["mac"] = _mac(key.key, str(last.get("mac", "")), rec["hash"])
            cut = self._ends_cut()
            with self.path.open("a", encoding="utf-8") as f:
                f.write(("\n" if cut else "") + json.dumps(rec, default=str) + "\n")

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        out: list[dict[str, Any]] = []
        for line in lines[-n:]:
            with contextlib.suppress(ValueError):
                rec = json.loads(line) if line.strip() else None
                if isinstance(rec, dict):
                    out.append(rec)
        return out

    def verify(self) -> bool:
        return bool(self.verify_report())

    def verify_report(self) -> VerifyReport:
        """Walk the whole chain. With a key: every record from the key's start time on must carry a valid mac."""
        prev, prev_mac, records, signed = GENESIS, "", 0, 0
        key = self.key
        if not self.path.exists():
            return VerifyReport(True, 0, 0)
        with self.path.open(encoding="utf-8", errors="replace") as f:  # bad bytes: "not a valid record"
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    claimed = rec.pop("hash")
                    mac = rec.pop("mac", None)
                    linked = rec["prev"] == prev
                except (ValueError, KeyError, TypeError, AttributeError):
                    return VerifyReport(False, records, signed, n, "not a valid record")
                if not linked or _digest(rec) != claimed:
                    return VerifyReport(False, records, signed, n, "a record was changed, removed or inserted")
                if key is not None:
                    if mac is not None:
                        if not hmac.compare_digest(str(mac), _mac(key.key, prev_mac, claimed)):
                            return VerifyReport(False, records, signed, n, "signature does not match the audit key")
                        signed += 1
                    elif str(rec.get("ts", "")) >= key.since:
                        return VerifyReport(False, records, signed, n, "signature missing on a record written "
                                            "after signing started")
                prev, prev_mac, records = claimed, str(mac or ""), records + 1
        return VerifyReport(True, records, signed)
