"""Resume or discard an interrupted task, the same rules for every channel (dashboard, CLI, Telegram).

Rules, enforced here and not left to a channel:
- Only the user starts a resume (a button or a command). Delta never resumes on its own.
- From a phone (any remote channel), Remote Guard applies: nothing while remote control is locked, and
  the PIN must have been entered (/unlock) within its window, because a resume makes Delta act again.
- The resumed task never gets a higher permission level than the one it started with (`autonomy_cap`).
- If the last step changed something and was cut off before it finished, the user is asked first
  whether it happened (`confirm_last`); nothing runs until they answer.
- Steps that already changed something are listed to the model as done, and repeating one of them
  exactly needs the user's yes (TaskEngine.repeat_reason, checked in ToolExecutor).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

from delta.core.state import TaskRequest
from delta.engine.states import TaskState
from delta.safety.remote_guard import is_remote

CONFIRM_DONE, CONFIRM_NOT_DONE = "done", "not_done"


@dataclass
class ResumeDecision:
    ok: bool
    message: str
    request: TaskRequest | None = None
    autonomy_cap: str | None = None
    needs_confirmation: bool = False
    options: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "message": self.message, "needs_confirmation": self.needs_confirmation,
                "options": self.options, "task_id": self.request.id if self.request else None}


def _audit(c: Any, event: str, **data: Any) -> None:
    audit = getattr(c, "audit", None)
    if audit is not None:
        audit.record(event, **data)


def _close_old_record(c: Any, task_id: str) -> None:
    """The run folder's own "interrupted" marker (ProofRecorder), so the older prompt is not shown too."""
    proof = getattr(getattr(c, "orchestrator", None), "proof", None)
    if proof is not None:
        with contextlib.suppress(Exception):
            proof.close_unfinished(task_id)


def prepare_resume(c: Any, task_id: str, channel: str, user: str = "owner",
                   confirm_last: str | None = None) -> ResumeDecision:
    """Check everything and, when allowed, move the task back to queued. Returns the request to submit
    (with `autonomy_cap`) or why not / what to ask the user first."""
    engine = getattr(c, "engine", None)
    if engine is None:
        return ResumeDecision(False, "The task engine is switched off, so there is nothing to resume.")
    plan = engine.plan_resume(task_id)
    if plan is None:
        return ResumeDecision(False, "I don't know that task.")
    if plan.task["state"] != TaskState.INTERRUPTED:
        return ResumeDecision(False, f"That task is {plan.task['state'].replace('_', ' ')}, not interrupted, so "
                                     "there is nothing to resume.")
    if is_remote(channel):
        guard = getattr(c, "guard", None)
        if guard is None or guard.locked:
            _audit(c, "guard.blocked", task=task_id, channel=channel, user=user, why="resume while locked")
            return ResumeDecision(False, guard.locked_message() if guard is not None else
                                  "Remote control is not available.")
        if not guard.has_pin:
            return ResumeDecision(False, "To resume a task from your phone, first set a remote PIN on the PC "
                                         "(Delta, Settings, Remote security).")
        if not guard.is_unlocked(user):
            return ResumeDecision(False, "Resuming makes Delta act on your PC again, so it needs your PIN: send "
                                         "/unlock, enter the PIN, then tap Resume again.")
    if plan.unconfirmed is not None and confirm_last not in (CONFIRM_DONE, CONFIRM_NOT_DONE):
        step = plan.unconfirmed
        return ResumeDecision(
            False, f"Before I stopped I had started step {step.step}: {step.tool} {step.args[:160]}. It may or may "
                   "not have happened. Please check, then tell me which is true. I will not repeat it without "
                   "your answer.",
            needs_confirmation=True,
            options=[{"id": CONFIRM_DONE, "label": "It happened, continue after it"},
                     {"id": CONFIRM_NOT_DONE, "label": "It did not happen, it may be done again"}])
    happened = None if plan.unconfirmed is None else confirm_last == CONFIRM_DONE
    engine.start_resume(task_id, plan, happened)
    _close_old_record(c, task_id)
    task = plan.task
    history = ""
    chats = getattr(c, "chats", None)
    if chats is not None and task.get("chat_id"):
        try:
            history = chats.context_for(task["chat_id"])
        except Exception:
            history = ""
    context = plan.context + (f"\n\nEarlier in this chat:\n{history}" if history else "")
    req = TaskRequest(task["text"], channel=channel, user=user, id=task_id, chat_id=task.get("chat_id") or "",
                      history=context)
    _audit(c, "task.resume", task=task_id, channel=channel, user=user, autonomy_cap=task.get("autonomy") or "",
           confirm_last=confirm_last or "")
    return ResumeDecision(True, "Resuming.", req, task.get("autonomy") or None)


def discard(c: Any, task_id: str, channel: str, user: str = "owner") -> tuple[bool, str]:
    engine = getattr(c, "engine", None)
    if engine is None or not engine.discard(task_id, by=f"{channel}:{user}"):
        return False, "That task is not waiting to be resumed."
    _close_old_record(c, task_id)
    _audit(c, "task.discard", task=task_id, channel=channel, user=user)
    return True, "Discarded. Nothing more will run for it."
