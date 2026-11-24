"""The project list, each project's chat, and the backups behind Undo. All of it lives in Delta's data folder
(studio/), never in the user's project, except the project memory file the user can read (.delta/PROJECT.md).

Undo: before the agent first changes a file in a turn, the file's current content (or the fact that it did not
exist) is saved under studio/<project>/turns/<turn>/. Undo puts every file of the latest turn back as it was.
Only the newest MAX_TURNS turns are kept.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MAX_TURNS = 30          # turns that can still be undone, per project
MAX_HISTORY = 200       # chat lines kept per project


@dataclass
class Project:
    id: str
    name: str
    path: str
    created: float
    opened: float
    last_run: float = 0.0

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Turn:
    id: str
    request: str
    started: float
    changed: list[str] = field(default_factory=list)   # project-relative paths, in first-change order
    undone: bool = False


class StudioStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._file = root / "projects.json"

    # ---- projects
    def _load(self) -> dict[str, Project]:
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out: dict[str, Project] = {}
        for item in raw if isinstance(raw, list) else []:
            try:
                p = Project(**{k: item[k] for k in ("id", "name", "path", "created", "opened")},
                            last_run=float(item.get("last_run", 0)))
            except (KeyError, TypeError, ValueError):
                continue
            out[p.id] = p
        return out

    def _save(self, projects: dict[str, Project]) -> None:
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps([p.public() for p in projects.values()], indent=1), encoding="utf-8")
        tmp.replace(self._file)

    def projects(self) -> list[Project]:
        with self._lock:
            return sorted(self._load().values(), key=lambda p: -max(p.opened, p.last_run))

    def get(self, pid: str) -> Project | None:
        with self._lock:
            return self._load().get(pid)

    def add(self, name: str, path: Path) -> Project:
        """A project for this folder; the same folder again returns the existing project."""
        with self._lock:
            projects = self._load()
            same = next((p for p in projects.values() if Path(p.path) == path), None)
            now = time.time()
            if same:
                same.opened = now
                self._save(projects)
                return same
            p = Project(uuid.uuid4().hex[:12], (name.strip() or path.name)[:80], str(path), now, now)
            projects[p.id] = p
            self._save(projects)
            return p

    def touch(self, pid: str, *, ran: bool = False) -> None:
        with self._lock:
            projects = self._load()
            if (p := projects.get(pid)) is None:
                return
            if ran:
                p.last_run = time.time()
            else:
                p.opened = time.time()
            self._save(projects)

    def forget(self, pid: str) -> bool:
        """Remove the project from the list and delete Delta's own records of it. The project's files stay."""
        with self._lock:
            projects = self._load()
            if projects.pop(pid, None) is None:
                return False
            self._save(projects)
        shutil.rmtree(self._dir(pid), ignore_errors=True)
        return True

    def _dir(self, pid: str) -> Path:
        if not pid.isalnum():
            raise ValueError("bad project id")
        return self.root / pid

    # ---- chat
    def history(self, pid: str, limit: int = MAX_HISTORY) -> list[dict[str, Any]]:
        path = self._dir(pid) / "chat.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def say(self, pid: str, role: str, text: str, **extra: Any) -> None:
        folder = self._dir(pid)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "chat.jsonl"
        with self._lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"role": role, "text": text, "at": time.time(), **extra}) + "\n")
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return
            if len(lines) > MAX_HISTORY * 2:
                path.write_text("\n".join(lines[-MAX_HISTORY:]) + "\n", encoding="utf-8")

    def clear_history(self, pid: str) -> None:
        (self._dir(pid) / "chat.jsonl").unlink(missing_ok=True)

    # ---- turns and undo
    def _turns_dir(self, pid: str) -> Path:
        return self._dir(pid) / "turns"

    def new_turn(self, pid: str, request: str) -> Turn:
        turn = Turn(time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6], request[:300], time.time())
        folder = self._turns_dir(pid) / turn.id
        (folder / "files").mkdir(parents=True, exist_ok=True)
        self._write_turn(pid, turn, {})
        self._prune(pid)
        return turn

    def _manifest(self, pid: str, turn_id: str) -> tuple[Turn, dict[str, bool]]:
        data = json.loads((self._turns_dir(pid) / turn_id / "turn.json").read_text(encoding="utf-8"))
        turn = Turn(data["id"], data["request"], data["started"], list(data["changed"]), bool(data["undone"]))
        return turn, {k: bool(v) for k, v in data.get("existed", {}).items()}

    def _write_turn(self, pid: str, turn: Turn, existed: dict[str, bool]) -> None:
        path = self._turns_dir(pid) / turn.id / "turn.json"
        path.write_text(json.dumps({**asdict(turn), "existed": existed}), encoding="utf-8")

    def back_up(self, pid: str, turn: Turn, rel: str, path: Path) -> None:
        """Keep the file as it was before this turn first changes it. Later changes in the same turn keep the
        first copy, so Undo always returns to the state before the turn."""
        with self._lock:
            current, existed = self._manifest(pid, turn.id)
            if rel in existed:
                return
            existed[rel] = path.is_file()
            if existed[rel]:
                copy = self._turns_dir(pid) / turn.id / "files" / f"{len(existed)}.bak"
                shutil.copy2(path, copy)
                existed[rel] = True
            current.changed.append(rel)
            turn.changed = current.changed
            self._write_turn(pid, current, existed)

    def turns(self, pid: str) -> list[Turn]:
        folder = self._turns_dir(pid)
        out = []
        for d in sorted(folder.iterdir() if folder.is_dir() else [], reverse=True):
            try:
                out.append(self._manifest(pid, d.name)[0])
            except (OSError, ValueError, KeyError):
                continue
        return out

    def original(self, pid: str, turn_id: str, rel: str) -> bytes | None:
        """The file's content before the turn (None: it did not exist)."""
        _turn, existed = self._manifest(pid, turn_id)
        if not existed.get(rel):
            return None
        index = list(existed).index(rel) + 1
        return (self._turns_dir(pid) / turn_id / "files" / f"{index}.bak").read_bytes()

    def undo(self, pid: str, root: Path, check: Any) -> tuple[Turn | None, list[str]]:
        """Put back every file of the newest turn that is not undone yet. Returns (turn, files restored)."""
        turn = next((t for t in self.turns(pid) if not t.undone and t.changed), None)
        if turn is None:
            return None, []
        _, existed = self._manifest(pid, turn.id)
        restored = []
        for index, (rel, was) in enumerate(existed.items(), 1):
            path, _ = check(root, rel)  # the same jail as the agent: a backup can only go back inside the project
            if was:
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self._turns_dir(pid) / turn.id / "files" / f"{index}.bak", path)
            else:
                path.unlink(missing_ok=True)
            restored.append(rel)
        turn.undone = True
        self._write_turn(pid, turn, existed)
        return turn, restored

    def _prune(self, pid: str) -> None:
        folder = self._turns_dir(pid)
        old = sorted(folder.iterdir())[:-MAX_TURNS] if folder.is_dir() else []
        for d in old:
            shutil.rmtree(d, ignore_errors=True)
