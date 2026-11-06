"""The task state machine: every task Delta works on is in exactly one of these states, and only the
transitions listed in TRANSITIONS are allowed. TaskStore.transition enforces them in code, so a bug
cannot move a finished task back to running or skip the interrupted step after a crash.

    queued -> planning -> acting <-> verifying -> done
                 |           |          |
                 +-----------+----------+--> waiting_user (a question for the user) -> planning | acting
                 |           |          |
                 +-----------+----------+--> failed | cancelled | interrupted (Delta or the PC stopped)

interrupted -> queued (the user pressed Resume) | cancelled (Discard) | failed.
done, failed and cancelled are final.
"""

from __future__ import annotations

from enum import StrEnum


class TaskState(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    ACTING = "acting"
    WAITING_USER = "waiting_user"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


S = TaskState

# Where each state may go next. Anything else is a bug and is refused.
TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    S.QUEUED: frozenset({S.PLANNING, S.ACTING, S.CANCELLED, S.FAILED, S.INTERRUPTED, S.DONE, S.WAITING_USER}),
    S.PLANNING: frozenset({S.ACTING, S.WAITING_USER, S.VERIFYING, S.DONE, S.FAILED, S.CANCELLED, S.INTERRUPTED}),
    S.ACTING: frozenset({S.VERIFYING, S.WAITING_USER, S.DONE, S.FAILED, S.CANCELLED, S.INTERRUPTED}),
    S.VERIFYING: frozenset({S.ACTING, S.DONE, S.FAILED, S.CANCELLED, S.INTERRUPTED, S.WAITING_USER}),
    S.WAITING_USER: frozenset({S.PLANNING, S.ACTING, S.CANCELLED, S.FAILED, S.DONE}),
    S.INTERRUPTED: frozenset({S.QUEUED, S.CANCELLED, S.FAILED}),
    S.DONE: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}

# States in which a task is being worked on right now. Found in one of these at start-up, a task was
# cut off by a crash, a power cut or a forced shutdown: it becomes `interrupted`.
RUNNING = frozenset({S.QUEUED, S.PLANNING, S.ACTING, S.VERIFYING})
FINAL = frozenset({S.DONE, S.FAILED, S.CANCELLED})

# TaskResult.status (delta.core.state) -> the state a finished run leaves its task in.
RESULT_STATE: dict[str, TaskState] = {
    "done": S.DONE, "failed": S.FAILED, "cancelled": S.CANCELLED, "needs_user": S.WAITING_USER,
}


class IllegalTransitionError(ValueError):
    def __init__(self, task_id: str, current: str, new: str) -> None:
        super().__init__(f"task {task_id}: {current} -> {new} is not an allowed transition")
        self.task_id, self.current, self.new = task_id, current, new


def can_transition(current: TaskState | str, new: TaskState | str) -> bool:
    try:
        return TaskState(new) in TRANSITIONS[TaskState(current)]
    except ValueError:
        return False
