"""Undo a task's file changes: the same rules for every channel (CLI, dashboard, Telegram).

- Only the user starts an undo (a command or a button). Delta never undoes on its own.
- From a phone (any remote channel) Remote Guard applies, as for Resume: nothing while remote control is
  locked, and the PIN must have been entered (/unlock) within its window, because undo changes files.
- Restoring checks the current policy: paths no longer inside the allowed folders, or now protected or
  blocked, are skipped (UndoStore.restore). Nothing is deleted for good: files in the way are moved into
  the task's undo folder.
"""

from __future__ import annotations

from typing import Any

from delta.safety.remote_guard import is_remote


def undo_task(c: Any, task_id: str, channel: str, user: str = "owner") -> dict[str, Any]:
    executor = getattr(getattr(c, "orchestrator", None), "executor", None)
    undo = getattr(executor, "undo", None)
    if undo is None or executor is None:
        return {"ok": False, "message": "Undo for file changes is switched off (undo.enabled in settings)."}
    if not task_id.replace("-", "").replace("_", "").isalnum():
        return {"ok": False, "message": "I don't know that task."}
    if is_remote(channel):
        guard = getattr(c, "guard", None)
        if guard is None or guard.locked:
            return {"ok": False, "message": guard.locked_message() if guard is not None
                    else "Remote control is not available."}
        if not guard.has_pin:
            return {"ok": False, "message": ("To undo from your phone, first set a remote PIN on the PC (Delta, "
                                                "Settings, Remote security).")}
        if not guard.is_unlocked(user):
            return {"ok": False, "message": ("Undo changes files on your PC, so it needs your PIN: send /unlock, "
                                                "enter the PIN, then send /undo again.")}
    result = undo.restore(task_id, executor.policy)
    audit = getattr(c, "audit", None)
    if audit is not None:
        audit.record("task.undo", task=task_id, channel=channel, user=user, restored=len(result["restored"]),
                     skipped=len(result["skipped"]))
    return result
