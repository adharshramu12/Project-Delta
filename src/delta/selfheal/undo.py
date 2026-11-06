"""Undo for file changes: a shadow copy of every file Delta changes, moves or recycles inside the user's
allowed folders, kept per task in <data folder>/undo/<task id>/, so the change can be put back.

    before a step   UndoStore.before(task_id, step, tool, args)   copy what is about to change
    after the step  UndoStore.after(entry, ok)                     forget the copy if the step failed
    restore         UndoStore.restore(task_id, policy)             newest change first

Rules:
- Only files inside the allowed folders (PolicyEngine.allowed). Anything else is not copied: changes there
  already need the user's yes, and Delta never copies files out of protected or secret places.
- Files over `max_file_mb`, or past the task's `max_task_mb`, are not copied; the entry says so and the run
  timeline shows "not undoable" for that step. Folders are not copied (a moved folder is moved back).
- Restoring never deletes anything for good: a file in the way (a newer version, a file Delta created) is
  moved into the task's undo folder (displaced/), so even an undo can be taken back by hand.
- Restoring checks the current policy: a path that is now outside the allowed folders, denied or protected is
  skipped. Old undo folders are removed after `retention_days`.
- Permanent deletes are not covered and still ask the user as before; delete_path already uses the Recycle Bin.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from delta.config import UndoConfig
from delta.safety.policy import _under, norm_path
from delta.system.logging import log

MANIFEST = "manifest.json"
# tool -> how its arguments name what it changes
FILE_TOOLS = {"write_file", "edit_file", "move_path", "delete_path"}


@dataclass
class UndoEntry:
    id: str
    op: str                  # modified | created | moved | recycled
    path: str                # the file as it was (for moved: the source)
    tool: str
    step: int | None = None
    dest: str = ""           # moved: where it went
    shadow: str = ""         # copy of the old content, relative to the task's undo folder
    dest_shadow: str = ""    # moved over an existing file: copy of that file
    size: int = 0
    skipped: str = ""        # why no copy was kept (too big, task cap): this change cannot be undone here
    ts: float = field(default_factory=time.time)
    restored: bool = False


def _safe_id(task_id: str) -> str:
    return "".join(c for c in task_id if c.isalnum() or c in "-_")[:64] or "task"


class UndoStore:
    def __init__(self, root: Path, cfg: UndoConfig | None = None,
                 allowed: list[Path] | Callable[[], list[Path]] | None = None) -> None:
        """allowed: the allowed folders, or a function returning them (follows boundary changes live)."""
        self.root = root
        self.cfg = cfg or UndoConfig()
        self._allowed = allowed
        self._lock = threading.Lock()

    @property
    def allowed(self) -> list[Path]:
        raw = self._allowed() if callable(self._allowed) else (self._allowed or [])
        return [norm_path(p) for p in raw]

    # --- bookkeeping ----------------------------------------------------------------------------
    def folder(self, task_id: str) -> Path:
        return self.root / _safe_id(task_id)

    def entries(self, task_id: str) -> list[UndoEntry]:
        path = self.folder(task_id) / MANIFEST
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        out = []
        for item in raw if isinstance(raw, list) else []:
            try:
                out.append(UndoEntry(**item))
            except TypeError:
                continue
        return out

    def _save(self, task_id: str, entries: list[UndoEntry]) -> None:
        folder = self.folder(task_id)
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (MANIFEST + ".tmp")
        tmp.write_text(json.dumps([asdict(e) for e in entries], indent=1), encoding="utf-8")
        tmp.replace(folder / MANIFEST)

    def _used(self, task_id: str) -> int:
        return sum(e.size for e in self.entries(task_id) if e.shadow or e.dest_shadow)

    def summary(self, task_id: str) -> dict[str, Any]:
        """For the run timeline: can this task's file changes be undone?"""
        entries = self.entries(task_id)
        live = [e for e in entries if not e.restored]
        undoable = [e for e in live if not e.skipped]
        return {"available": bool(undoable), "files": len(undoable), "restored": bool(entries) and not live,
                "skipped": [{"path": e.path, "why": e.skipped} for e in live if e.skipped],
                "steps": sorted({e.step for e in undoable if e.step is not None})}

    def _inside(self, p: Path) -> bool:
        roots = self.allowed
        return bool(roots) and _under(p, roots)

    # --- before / after a step ------------------------------------------------------------------
    def before(self, task_id: str, step: int | None, tool: str, args: Any) -> list[str]:
        """Copy what this call is about to change. Returns entry ids (to drop if the step fails). Never raises."""
        if not self.cfg.enabled or not task_id or tool not in FILE_TOOLS:
            return []
        try:
            with self._lock:
                return self._before(task_id, step, tool, args)
        except Exception:
            log.exception("undo.before_failed", task=task_id, tool=tool)
            return []

    def _before(self, task_id: str, step: int | None, tool: str, args: Any) -> list[str]:
        get = args.get if isinstance(args, dict) else (lambda k, d=None: getattr(args, k, d))
        entries = self.entries(task_id)
        new: list[UndoEntry] = []
        if tool in ("write_file", "edit_file", "delete_path"):
            p = norm_path(str(get("path", "") or ""))
            if not self._inside(p) or p.is_dir():
                return []
            if not p.exists():
                if tool == "write_file":
                    new.append(UndoEntry(uuid.uuid4().hex[:10], "created", str(p), tool, step))
            else:
                op = "recycled" if tool == "delete_path" else "modified"
                entry = UndoEntry(uuid.uuid4().hex[:10], op, str(p), tool, step)
                self._copy(task_id, p, entry, "shadow", entries)
                new.append(entry)
        elif tool == "move_path":
            src, dst = norm_path(str(get("source", "") or "")), norm_path(str(get("destination", "") or ""))
            if not (self._inside(src) and self._inside(dst)) or not src.exists():
                return []
            entry = UndoEntry(uuid.uuid4().hex[:10], "moved", str(src), tool, step, dest=str(dst))
            if dst.exists() and dst.is_file():
                self._copy(task_id, dst, entry, "dest_shadow", entries)
            new.append(entry)
        if new:
            self._save(task_id, entries + new)
        return [e.id for e in new]

    def _copy(self, task_id: str, p: Path, entry: UndoEntry, attr: str, entries: list[UndoEntry]) -> None:
        size = p.stat().st_size
        limit = self.cfg.max_file_mb * 1024 * 1024
        if limit <= 0 or size > limit:
            entry.skipped = f"bigger than {self.cfg.max_file_mb:g} MB, so no copy was kept"
            log.warning("undo.file_too_big", task=task_id, path=str(p), size=size)
            return
        used = sum(e.size for e in entries if e.shadow or e.dest_shadow)
        if used + size > self.cfg.max_task_mb * 1024 * 1024:
            entry.skipped = f"this task already keeps {self.cfg.max_task_mb:g} MB of undo copies"
            log.warning("undo.task_cap", task=task_id, path=str(p))
            return
        rel = f"files/{entry.id}-{attr}-{p.name}"
        target = self.folder(task_id) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        setattr(entry, attr, rel)
        entry.size += size

    def after(self, task_id: str, ids: list[str], ok: bool) -> None:
        """A step that failed changed nothing we need to undo: drop its entries and copies."""
        if ok or not ids:
            return
        try:
            with self._lock:
                entries = self.entries(task_id)
                keep = []
                for e in entries:
                    if e.id in ids:
                        for rel in (e.shadow, e.dest_shadow):
                            if rel:
                                (self.folder(task_id) / rel).unlink(missing_ok=True)
                    else:
                        keep.append(e)
                self._save(task_id, keep)
        except Exception:
            log.exception("undo.after_failed", task=task_id)

    # --- restore --------------------------------------------------------------------------------
    def restore(self, task_id: str, policy: Any = None) -> dict[str, Any]:
        """Put back every change of the task, newest first. Returns {"ok", "restored", "skipped", "message"}."""
        with self._lock:
            entries = self.entries(task_id)
            live = [e for e in entries if not e.restored]
            if not live:
                return {"ok": False, "restored": [], "skipped": [],
                        "message": "There is nothing to undo for that task." if not entries
                        else "That task's file changes were already undone."}
            restored: list[str] = []
            skipped: list[dict[str, str]] = []
            for e in reversed(live):
                why = self._blocked(e, policy)
                if not why:
                    try:
                        why = self._restore_one(task_id, e)
                    except OSError as err:
                        why = f"{type(err).__name__}: {err}"
                if why:
                    skipped.append({"path": e.path, "why": why})
                else:
                    e.restored = True
                    restored.append(e.path)
            self._save(task_id, entries)
        if restored and not skipped:
            message = f"Undone: put back {len(restored)} file change(s)."
        elif restored:
            message = f"Put back {len(restored)} file change(s); {len(skipped)} could not be undone."
        else:
            message = "Nothing could be undone: " + "; ".join(f"{s['path']}: {s['why']}" for s in skipped[:3])
        return {"ok": bool(restored), "restored": restored, "skipped": skipped, "message": message}

    def _blocked(self, e: UndoEntry, policy: Any) -> str:
        if e.skipped:
            return e.skipped
        paths = [norm_path(e.path)] + ([norm_path(e.dest)] if e.dest else [])
        allowed = getattr(policy, "allowed", None) if policy is not None else None
        roots = [norm_path(p) for p in allowed] if allowed is not None else self.allowed
        for p in paths:
            if not roots or not _under(p, roots):
                return "no longer inside your allowed folders"
            for attr in ("protected", "denied", "read_only"):
                if policy is not None and _under(p, list(getattr(policy, attr, []) or [])):
                    return "that folder is now protected or blocked"
        return ""

    def _displace(self, task_id: str, p: Path) -> None:
        """Move a file out of the way into the undo folder (never a permanent delete)."""
        if not p.exists():
            return
        target = self.folder(task_id) / "displaced" / f"{uuid.uuid4().hex[:8]}-{p.name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(target))

    def _restore_one(self, task_id: str, e: UndoEntry) -> str:
        folder = self.folder(task_id)
        p = Path(e.path)
        if e.op in ("modified", "recycled"):
            shadow = folder / e.shadow
            if not shadow.is_file():
                return "the undo copy is missing"
            self._displace(task_id, p)
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(shadow, p)
            return ""
        if e.op == "created":
            self._displace(task_id, p)
            return ""
        if e.op == "moved":
            dst = Path(e.dest)
            if p.exists():
                return "something new is already at the old place"
            if not dst.exists():
                return "the moved file is no longer where Delta put it"
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(p))
            if e.dest_shadow and (folder / e.dest_shadow).is_file():
                shutil.copy2(folder / e.dest_shadow, dst)
            return ""
        return f"unknown change type {e.op}"

    # --- retention ------------------------------------------------------------------------------
    def prune(self, now: float | None = None) -> int:
        if self.cfg.retention_days <= 0 or not self.root.is_dir():
            return 0
        cutoff = (time.time() if now is None else now) - self.cfg.retention_days * 86400
        removed = 0
        for folder in self.root.iterdir():
            try:
                if folder.is_dir() and (folder / MANIFEST).stat().st_mtime < cutoff:
                    shutil.rmtree(folder, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
        return removed
