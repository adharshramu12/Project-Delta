"""What the tools of a task really returned, in full, for answer grounding and for provenance.

StepRecord keeps only the first 2000 characters of each result (enough for the model and the log), but a fact
can sit further down a page. The executor adds every call that really ran here (the model's own calls, the
automatic reads after an action, self-heal retries, read-only helpers merged back), with its full output, so
grounding can check a claim against everything the task saw, and provenance can tell which values came from
outside content. Memory only, capped per task, dropped when the task ends (never written to disk).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any

MAX_CHARS_PER_TASK = 400_000
MAX_ENTRY_CHARS = 60_000
MAX_TASKS = 64


@dataclass
class Observation:
    tool: str
    ok: bool
    args: dict[str, Any]
    output: str
    evidence: dict[str, Any]
    outside: bool  # the tool brings in content someone else wrote (UNTRUSTED_SOURCES)
    reads: bool = False  # a read-only tool: its arguments are a question, not something the task did


class ObservationLog:
    def __init__(self) -> None:
        self._by_task: dict[str, list[Observation]] = {}
        self._size: dict[str, int] = {}
        self._lock = threading.Lock()

    def add(self, task_id: str, tool: str, ok: bool, args: dict[str, Any], output: str,
            evidence: dict[str, Any] | None, outside: bool, reads: bool = False) -> None:
        if not task_id:
            return
        text = str(output or "")[:MAX_ENTRY_CHARS]
        with self._lock:
            if task_id not in self._by_task and len(self._by_task) >= MAX_TASKS:
                self._by_task.pop(next(iter(self._by_task)), None)  # a task that never said goodbye
            items = self._by_task.setdefault(task_id, [])
            used = self._size.get(task_id, 0)
            if used + len(text) > MAX_CHARS_PER_TASK:
                text = text[:max(0, MAX_CHARS_PER_TASK - used)]
            items.append(Observation(tool, ok, dict(args or {}), text, dict(evidence or {}), outside, reads))
            self._size[task_id] = used + len(text)

    def items(self, task_id: str) -> list[Observation]:
        with self._lock:
            return list(self._by_task.get(task_id, []))

    def outside_text(self, task_id: str) -> str:
        """Everything the task read from outside sources (pages, files, messages, the screen)."""
        return "\n".join(o.output for o in self.items(task_id) if o.outside and o.ok)

    def corpus(self, task_id: str) -> str:
        """Evidence text: the outputs and evidence of every call that worked, and the arguments of the calls
        that worked and changed something (write_file with path X ran fine: X is something this task did).
        The arguments of read-only calls are questions (a search, a focus word), not evidence, and a check that
        found nothing (path_info: does not exist) proves nothing exists."""
        parts: list[str] = []
        for o in self.items(task_id):
            if not o.ok or o.evidence.get("exists") is False:
                continue
            parts.append(o.output)
            if o.evidence:
                parts.append(json.dumps(o.evidence, default=str, ensure_ascii=False))
            if o.args and not o.reads:
                parts.append(json.dumps(o.args, default=str, ensure_ascii=False))
        return "\n".join(parts)

    def missing_paths(self, task_id: str) -> set[str]:
        """Paths the task's own checks (path_info) last saw as not existing."""
        state: dict[str, bool] = {}
        for o in self.items(task_id):
            path = o.evidence.get("path")
            if o.ok and isinstance(path, str) and isinstance(o.evidence.get("exists"), bool):
                state[path.replace("/", "\\").lower()] = bool(o.evidence["exists"])
        return {p for p, exists in state.items() if not exists}

    def forget(self, task_id: str) -> None:
        with self._lock:
            self._by_task.pop(task_id, None)
            self._size.pop(task_id, None)
