"""Tamper-evident audit log: JSON lines where every record carries the SHA-256 of the previous
one. Editing or deleting any past line breaks the chain, which `verify()` detects.

The daemon and the CLI can both write the same file, so each write takes an OS file lock and chains to
the file's real last line, never to a hash cached in this process."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from collections.abc import Iterator
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


def _digest(record: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, default=str).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._lock_path = path.with_name(path.name + ".lock")

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

    def _last_hash(self) -> str:
        """The hash on the file's last line, read from the end so it stays cheap as the log grows."""
        if not self.path.exists():
            return GENESIS
        with self.path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            chunk = 4096
            while True:
                start = max(0, end - chunk)
                f.seek(start)
                lines = [ln for ln in f.read(end - start).splitlines() if ln.strip()]
                if len(lines) > 1 or start == 0:
                    return str(json.loads(lines[-1])["hash"]) if lines else GENESIS
                chunk *= 4

    def record(self, event: str, **data: Any) -> None:
        with self._lock, self._file_lock():
            rec: dict[str, Any] = {"ts": datetime.now(UTC).isoformat(), "event": event, "data": data,
                                   "prev": self._last_hash()}
            rec["hash"] = _digest(rec)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[-n:] if line.strip()]

    def verify(self) -> bool:
        prev = GENESIS
        if not self.path.exists():
            return True
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                claimed = rec.pop("hash")
                if rec["prev"] != prev or _digest(rec) != claimed:
                    return False
                prev = claimed
        return True
