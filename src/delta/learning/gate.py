"""The promotion gate: what Delta learns on its own is used only after it has proved itself.

Items it governs (named the way `delta learn` shows them):
  L<id>  a lesson (learning.db, kind "lesson"): a self-heal recovery that worked on a site or in an app
  R<id>  a recipe candidate (skills.db): a repaired version of one step of a saved recipe

Lifecycle
  candidate --(N successful real runs, or a passing scripted eval)--> active (used)
  candidate --(M failures in a row)--> rejected (lesson: retired)
  active    --(M failures in a row)--> retired (a recipe goes back to the version it was repaired from)
  user: `delta learn promote <id>` / `reject <id>` (or the dashboard) decides at any time
N = learning.promote_after_runs (default 3), M = learning.demote_after_failures (default 2).

Untrusted items (traced to outside content, delta.memory.provenance) are never promoted, by the runs, by an
eval or by a person: the stores refuse it in code and the refusal is written to the history.

Every change is written to the item's history (learning.db learn_events, append-only) and, when it happened
during a task, as a "learning" line in that task's run timeline (tasks.db).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from delta.config import LearningConfig
from delta.learning.store import LearningStore, UntrustedPromotionError
from delta.system.logging import log


@dataclass
class GateChange:
    ref: str
    change: str  # counted | promoted | rejected | retired | refused
    summary: str


def parse_ref(ref: str) -> tuple[str, int]:
    """'R3' -> ('R', 3); 'L12' -> ('L', 12). A bare number is a lesson."""
    ref = ref.strip().upper()
    if ref[:1] in ("L", "R") and ref[1:].isdigit():
        return ref[0], int(ref[1:])
    if ref.isdigit():
        return "L", int(ref)
    raise ValueError(f"not a learning id: {ref!r} (use L<number> for lessons, R<number> for recipe candidates)")


class PromotionGate:
    def __init__(self, cfg: LearningConfig, learning: LearningStore | None, skills: Any = None,
                 engine: Any = None) -> None:
        self.cfg = cfg
        self.learning = learning
        self.skills = skills  # SkillStore
        self.engine = engine  # TaskEngine: timeline lines; None = no task log

    # --- history -------------------------------------------------------------------------------
    def _history(self, ref: str, event: str, task_id: str, detail: str) -> None:
        if self.learning is not None:
            try:
                self.learning.log(ref, event, task_id, detail)
            except Exception:
                log.exception("gate.history_failed", ref=ref)
        if task_id and self.engine is not None:
            line = getattr(self.engine, "line", None)
            if callable(line):
                line(task_id, "learning", detail, ok=event not in ("refused", "rejected", "retired"),
                     data={"item": ref, "event": event})

    # --- one item, whatever its kind -----------------------------------------------------------
    def _get(self, ref: str) -> tuple[str, Any]:
        kind, num = parse_ref(ref)
        if kind == "L":
            item = self.learning.get(num) if self.learning is not None else None
        else:
            item = self.skills.get_candidate(num) if self.skills is not None else None
        if item is None:
            raise KeyError(ref)
        return kind, item

    def _set(self, kind: str, item: Any, status: str, by: str, task_id: str) -> None:
        if kind == "L":
            assert self.learning is not None
            self.learning.set_status(item.id, status, by=by, task_id=task_id)
            return
        assert self.skills is not None
        if status == "retired" and item.status == "active":
            self.skills.set_recipe(item.skill_id, item.base_recipe)  # back to the version it was repaired from
        self.skills.set_candidate_status(item.id, status)

    def _count(self, kind: str, item: Any, *, success: bool | None = None, eval_passed: bool = False) -> Any:
        if kind == "L":
            assert self.learning is not None
            return self.learning.count(item.id, success=success, eval_passed=eval_passed)
        assert self.skills is not None
        return self.skills.count_candidate(item.id, success=success, eval_passed=eval_passed)

    @staticmethod
    def _what(kind: str, item: Any) -> str:
        if kind == "L":
            return f"lesson L{item.id}"
        return f"repaired recipe R{item.id} (skill #{item.skill_id}, step {item.step + 1})"

    def _promote(self, kind: str, item: Any, why: str, by: str, task_id: str) -> GateChange:
        ref = f"{kind}{item.id}"
        if item.trust == "untrusted":
            line = f"Did not use {self._what(kind, item)}: it came from outside content, so it can never become a rule."
            self._history(ref, "refused", task_id, line)
            return GateChange(ref, "refused", line)
        self._set(kind, item, "active", by, task_id)
        line = f"Started using {self._what(kind, item)}: {why}."
        self._history(ref, "promoted", task_id, line)
        return GateChange(ref, "promoted", line)

    # --- evidence ------------------------------------------------------------------------------
    def record_run(self, ref: str, ok: bool, task_id: str = "") -> GateChange | None:
        """A real task used this item (or, for a candidate, its repaired step). Counts towards promotion or
        demotion. Never raises."""
        if not self.cfg.enabled:
            return None
        try:
            kind, item = self._get(ref)
        except (KeyError, ValueError):
            return None
        if item.status not in ("candidate", "active"):
            return None
        item = self._count(kind, item, success=ok)
        what = self._what(kind, item)
        if ok:
            self._history(ref, "run_ok", task_id, f"{what} worked in a real run ({item.successes} so far).")
            if item.status == "candidate" and item.successes >= self.cfg.promote_after_runs:
                return self._promote(kind, item, f"it worked in {item.successes} real runs", "gate", task_id)
            return GateChange(ref, "counted", what)
        self._history(ref, "run_failed", task_id, f"{what} failed in a real run ({item.failures} in a row).")
        if item.failures >= self.cfg.demote_after_failures:
            new = "retired" if (item.status == "active" or kind == "L") else "rejected"
            self._set(kind, item, new, "gate", task_id)
            line = f"Stopped using {what}: it failed {item.failures} times in a row."
            self._history(ref, new, task_id, line)
            return GateChange(ref, new, line)
        return GateChange(ref, "counted", what)

    def record_eval(self, ref: str, passed: bool, eval_id: str = "", task_id: str = "") -> GateChange | None:
        """A scripted eval exercised this item. A pass promotes a trusted candidate (learning.promote_on_eval);
        a fail counts as a failed run."""
        if not self.cfg.enabled:
            return None
        try:
            kind, item = self._get(ref)
        except (KeyError, ValueError):
            return None
        if not passed:
            return self.record_run(ref, False, task_id)
        item = self._count(kind, item, eval_passed=True)
        self._history(ref, "eval_passed", task_id, f"{self._what(kind, item)} passed eval {eval_id or '(scripted)'}.")
        if item.status == "candidate" and self.cfg.promote_on_eval:
            return self._promote(kind, item, f"it passed the scripted eval {eval_id}".rstrip(), "eval", task_id)
        return GateChange(ref, "counted", self._what(kind, item))

    # --- the user's own decisions --------------------------------------------------------------
    def promote(self, ref: str, by: str = "user") -> GateChange:
        """The user promotes an item by hand. Refused (UntrustedPromotionError) for untrusted items."""
        kind, item = self._get(ref)
        if item.trust == "untrusted":
            self._history(f"{kind}{item.id}", "refused", "", f"{by} asked to use it; refused: outside content.")
            raise UntrustedPromotionError(f"{ref} came from outside content and can never become a rule")
        return self._promote(kind, item, f"{by} chose to use it", by, "")

    def reject(self, ref: str, by: str = "user") -> GateChange:
        kind, item = self._get(ref)
        new = "undone" if kind == "L" else ("retired" if item.status == "active" else "rejected")
        self._set(kind, item, new, by, "")
        line = f"{by.capitalize()} rejected {self._what(kind, item)}."
        self._history(f"{kind}{item.id}", "rejected", "", line)
        return GateChange(f"{kind}{item.id}", "rejected", line)

    # --- listing -------------------------------------------------------------------------------
    def items(self, status: str | None = None) -> list[dict[str, Any]]:
        """Lessons and recipe candidates, newest first, in one list."""
        out: list[dict[str, Any]] = []
        if self.learning is not None:
            for i in self.learning.list(kind="lesson"):
                if status and i.status != status:
                    continue
                out.append({"ref": f"L{i.id}", "kind": "lesson", "status": i.status, "trust": i.trust,
                            "summary": i.content, "successes": i.successes, "failures": i.failures,
                            "evals_passed": i.evals_passed, "task_id": i.task_id, "updated": i.updated})
        if self.skills is not None:
            for c in self.skills.candidates(status=status):
                skill = self.skills.get(c.skill_id)
                out.append({"ref": f"R{c.id}", "kind": "recipe", "status": c.status, "trust": c.trust,
                            "summary": f"{skill.name if skill else 'skill #' + str(c.skill_id)}: step {c.step + 1} "
                                       f"repaired ({c.change})",
                            "successes": c.successes, "failures": c.failures, "evals_passed": c.evals_passed,
                            "task_id": c.task_id, "updated": c.updated})
        out.sort(key=lambda d: str(d["updated"]), reverse=True)
        return out

    def history(self, ref: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if self.learning is None:
            return []
        return self.learning.history(ref.upper() if ref else None, limit)
