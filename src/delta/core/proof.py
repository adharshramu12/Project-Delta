"""Proof of work: every task keeps its screenshots, step log and checkpoint results on this PC.

A run is "verified" only when a fresh screenshot of the app or page it worked in shows each
checkpoint met, and the user can open that proof later (Delta's data folder, runs/<task id>).
Kept locally in Delta's protected data folder; only the newest runs are kept.
"""

from __future__ import annotations

import base64
import json
import shutil
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

KEEP_RUNS = 30


class ProofRecorder:
    def __init__(self, root: Path, keep: int = KEEP_RUNS) -> None:
        self.root = root
        self.keep = keep
        self.dir: Path | None = None
        self._n = 0

    def start(self, task_id: str, request: str) -> None:
        self.dir = self.root / task_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._n = 0
        (self.dir / "request.txt").write_text(request, encoding="utf-8")
        self._prune()

    def save_image(self, b64_jpeg: str, label: str) -> str | None:
        """Store a screenshot of the current run; returns its file name (None outside a run)."""
        if self.dir is None:
            return None
        self._n += 1
        safe = "".join(ch if ch.isalnum() else "-" for ch in label.lower())[:40].strip("-") or "screen"
        name = f"{self._n:02d}-{safe}.jpg"
        (self.dir / name).write_bytes(base64.b64decode(b64_jpeg))
        return name

    def checkpoint(self, steps: list[Any]) -> None:
        """Progress so far (after every step), so an interrupted run can be shown and resumed."""
        if self.dir is None:
            return
        rows = [{"tool": s.tool, "ok": s.ok, "output": s.output[:300]} for s in steps]
        tmp = self.dir / "progress.tmp"
        tmp.write_text(json.dumps(rows, default=str), encoding="utf-8")
        tmp.replace(self.dir / "progress.json")  # atomic: a crash never leaves half a file

    def unfinished(self, max_age_s: float = 86400) -> dict[str, Any] | None:
        """The newest run that started but never finished (Delta or the PC stopped mid-task)."""
        if not self.root.is_dir():
            return None
        runs = sorted((p for p in self.root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
        for d in runs[:5]:
            if (d / "run.json").exists() or not (d / "request.txt").exists() or d == self.dir:
                continue
            if time.time() - d.stat().st_mtime > max_age_s:
                return None
            steps = []
            if (d / "progress.json").exists():
                try:
                    steps = json.loads((d / "progress.json").read_text(encoding="utf-8"))
                except ValueError:
                    steps = []
            return {"task_id": d.name, "request": (d / "request.txt").read_text(encoding="utf-8"), "steps": steps}
        return None

    def close_unfinished(self, task_id: str) -> bool:
        """Mark an interrupted run as handled (resumed or dismissed), so it is offered only once."""
        d = self.root / task_id
        if not task_id.isalnum() or not (d / "request.txt").exists() or (d / "run.json").exists():
            return False
        (d / "run.json").write_text(json.dumps({"task_id": task_id, "status": "interrupted"}), encoding="utf-8")
        return True

    def finish(self, result: Any) -> None:
        if self.dir is None:
            return
        data = {"task_id": result.task_id, "status": result.status, "answer": result.answer,
                "attempts": result.attempts, "provider": result.provider, "finished": time.time(),
                "verification": asdict(result.verification) if result.verification else None,
                "steps": [asdict(s) for s in result.steps]}
        (self.dir / "run.json").write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
        self.dir = None

    def files(self, task_id: str) -> list[str]:
        d = self.root / task_id
        return sorted(p.name for p in d.glob("*.jpg")) if d.is_dir() else []

    def _prune(self) -> None:
        runs = sorted((p for p in self.root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
        for old in runs[:-self.keep]:
            shutil.rmtree(old, ignore_errors=True)
