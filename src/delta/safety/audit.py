"""Tamper-evident audit log: JSON lines where every record carries the SHA-256 of the previous
one. Editing or deleting any past line breaks the chain, which `verify()` detects."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _digest(record: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, default=str).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._prev = self._last_hash()

    def _last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        last = GENESIS
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = json.loads(line)["hash"]
        return last

    def record(self, event: str, **data: Any) -> None:
        with self._lock:
            rec: dict[str, Any] = {"ts": datetime.now(UTC).isoformat(), "event": event, "data": data,
                                   "prev": self._prev}
            rec["hash"] = _digest(rec)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
            self._prev = rec["hash"]

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
