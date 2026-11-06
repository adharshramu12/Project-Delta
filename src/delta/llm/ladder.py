"""Cheap-first model ladder per agent step: OPT-IN, off by default.

With `router.ladder` empty (the default) nothing here does anything and the router answers every call
from the user's normal route, exactly as before. Delta never adds, reorders or replaces providers on its
own; the ladder only names cheaper models among the user's OWN configured providers.

    router:
      ladder: [ollama]        # cheapest first; your normal route is always the top rung
      escalate_after: 2       # failed steps in a row before moving up a rung
      cheap_steps: [observe]  # which agent steps may start low: observe and/or act

Step types: "observe" is the step right after read-only tool calls that worked (reading a page, a file, a
window: simple, the small model copes); "act" is every other agent step. Planning and verification never
use the ladder (they keep the main model). Once a task has escalated it stays up for the rest of the task.
Which model ran each step is in the run timeline (the step's model) and in the task's usage ("models").
"""

from __future__ import annotations

from typing import Any

from delta.config import RouterConfig
from delta.system.logging import log

# Tools that only look: a step right after these (all of them worked) is an "observe" step.
OBSERVE_TOOLS = {"browser_read", "read_window", "list_windows", "path_info", "read_file", "list_dir", "search_files",
                 "web_search", "fetch_page", "recall", "media_status", "look_at_screen", "list_running_apps"}


class ModelLadder:
    def __init__(self, cfg: RouterConfig | None, providers: dict[str, Any] | None = None) -> None:
        self.cfg = cfg or RouterConfig()
        known = providers or {}
        self.rungs = [n for n in dict.fromkeys(self.cfg.ladder) if n in known]
        missing = [n for n in self.cfg.ladder if n not in known]
        if missing:
            log.warning("ladder.unknown_providers", names=missing,
                        hint="router.ladder names must be providers from llm.providers")
        self._level: dict[str, int] = {}
        self._fails: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.rungs)

    def forget(self, task_id: str) -> None:
        self._level.pop(task_id, None)
        self._fails.pop(task_id, None)

    @staticmethod
    def step_kind(steps: list[Any], since: int) -> str:
        """observe when every tool call since the last model reply only looked, and worked."""
        recent = steps[since:]
        if recent and all(s.tool in OBSERVE_TOOLS and s.ok for s in recent):
            return "observe"
        return "act"

    def pick(self, task_id: str, kind: str) -> str | None:
        """The provider to try first for this step, or None for the normal route."""
        if not self.enabled or kind not in self.cfg.cheap_steps:
            return None
        level = self._level.get(task_id, 0)
        return self.rungs[level] if level < len(self.rungs) else None

    def record(self, task_id: str, ok: bool) -> None:
        """After each step: failures in a row move the task up a rung (and it stays there)."""
        if not self.enabled:
            return
        if ok:
            self._fails[task_id] = 0
            return
        self._fails[task_id] = self._fails.get(task_id, 0) + 1
        if self._fails[task_id] >= self.cfg.escalate_after:
            self._fails[task_id] = 0
            self._level[task_id] = self._level.get(task_id, 0) + 1
            log.info("ladder.escalate", task=task_id, level=self._level[task_id])

    def level(self, task_id: str) -> int:
        return self._level.get(task_id, 0)
