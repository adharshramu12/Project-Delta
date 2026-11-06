"""Which apps are connected, their last known tool lists and the user's per-tool choices.

<data dir>/connectors/connections.json. No secrets here (they are in secrets.py, sealed). Written atomically; the
running service notices when `delta apps` (another process) changed it and reloads.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MAX_DESCRIPTION = 1000
MAX_TOOLS = 300  # per app: a server listing thousands of tools is cut (and the Apps page says so)


@dataclass
class Connection:
    id: str
    custom: dict[str, Any] | None = None       # the AppSpec of a server the user added by hand; None = catalog app
    inputs: dict[str, str] = field(default_factory=dict)  # non-secret settings, e.g. the folder for the file server
    tools: list[dict[str, Any]] = field(default_factory=list)  # name, description, input_schema, annotations
    choices: dict[str, dict[str, Any]] = field(default_factory=dict)  # tool -> {"enabled": bool, "risk": str}
    trust_read_only_hints: bool = False         # the user ticked "trust this app's read-only labels"
    connected_at: float = 0.0
    last_ok: float = 0.0
    last_error: str = ""
    needs_reconnect: bool = False
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Connection:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def clean_tools(raw: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    out = []
    for t in raw[:MAX_TOOLS]:
        schema = t.get("inputSchema") or t.get("input_schema") or {"type": "object", "properties": {}}
        out.append({"name": str(t["name"])[:128], "title": str(t.get("title") or "")[:200],
                    "description": str(t.get("description") or "")[:MAX_DESCRIPTION],
                    "input_schema": schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
                    "annotations": t.get("annotations") if isinstance(t.get("annotations"), dict) else {}})
    return out, len(raw) > MAX_TOOLS


class ConnectionStore:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.path = folder / "connections.json"
        self._mtime = 0.0
        self.items: dict[str, Connection] = {}
        self.reload()

    def reload(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._mtime = self.path.stat().st_mtime
        except (OSError, ValueError):
            raw = {}
        apps = raw.get("apps") if isinstance(raw, dict) else None
        self.items = {}
        for app_id, d in (apps or {}).items():
            with contextlib.suppress(TypeError, ValueError):
                self.items[str(app_id)] = Connection.from_dict({**d, "id": str(app_id)})

    def changed_on_disk(self) -> bool:
        try:
            return self.path.stat().st_mtime != self._mtime
        except OSError:
            return bool(self.items) and self._mtime != 0.0

    def save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"connections.{os.getpid()}.tmp")
        data = {"version": 1, "apps": {k: v.to_dict() for k, v in self.items.items()}}
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
        with contextlib.suppress(OSError):
            self._mtime = self.path.stat().st_mtime

    def get(self, app_id: str) -> Connection | None:
        return self.items.get(app_id)

    def put(self, conn: Connection) -> None:
        self.items[conn.id] = conn
        self.save()

    def remove(self, app_id: str) -> bool:
        found = self.items.pop(app_id, None) is not None
        self.save()
        return found

    def touch_ok(self, app_id: str) -> None:
        if (c := self.items.get(app_id)) is not None:
            c.last_ok, c.last_error, c.needs_reconnect = time.time(), "", False
