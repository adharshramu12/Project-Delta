"""One live status message per task on a chat channel (Telegram today), so a long task never looks stuck.

It appears only when a task runs longer than SHOW_AFTER_S (quick answers stay one message), then is edited in
place as the task moves: what Delta is doing now in plain words (delta.core.activity), the time so far and the
number of steps. Edits are spaced out (EDIT_EVERY_S) to stay well inside the chat app's limits, and refreshed
every HEARTBEAT_S even when nothing new happens, so the timer keeps moving. When the task ends the message says
how it ended; the answer itself follows as its own message.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

SHOW_AFTER_S = 3.0
EDIT_EVERY_S = 3.0
HEARTBEAT_S = 15.0
SLOW_STEP_S = 30.0

Send = Callable[[str], Awaitable[Any]]          # sends a new message, returns something `edit` understands
Edit = Callable[[Any, str], Awaitable[Any]]     # edits that message


def clock(secs: float) -> str:
    s = max(0, int(secs))
    return f"{s // 60}:{s % 60:02d}"


class LiveStatus:
    def __init__(self, task_id: str, send: Send, edit: Edit, now: Callable[[], float] = time.monotonic) -> None:
        self.task_id, self._send, self._edit, self._now = task_id, send, edit, now
        self.started = now()
        self.step_at = self.started
        self.doing = "Thinking about how to do this"
        self.steps = 0
        self.waiting = False
        self.handle: Any = None
        self.last_text = ""
        self.last_edit = 0.0
        self._loop: asyncio.Task[None] | None = None

    # --- events from the bus ------------------------------------------------------------------------------------
    async def on_event(self, event: str, data: dict[str, Any]) -> None:
        if data.get("task_id") != self.task_id:
            return
        if event == "task.queued":
            self.doing, self.waiting = "Waiting for the task already running to finish", True
        elif event == "task.started":
            self.doing, self.waiting = "Thinking about how to do this", False
        elif event == "task.step":
            self.steps += 1
            self.doing = str(data.get("label") or data.get("tool") or "Working")
            self.waiting, self.step_at = False, self._now()
        elif event == "task.verify":
            self.doing = "Checking the result" if data.get("passed") else "Checking the result again"
        else:
            return
        await self.refresh()

    # --- the message ------------------------------------------------------------------------------------------
    def text(self) -> str:
        elapsed = self._now() - self.started
        slow = not self.waiting and self._now() - self.step_at > SLOW_STEP_S
        now = ("Still waiting: " if slow else "") + self.doing
        steps = f" · {self.steps} step{'s' if self.steps != 1 else ''}" if self.steps else ""
        return f"⏳ Working · {clock(elapsed)}{steps}\nNow: {now}\n(say \"stop\" to cancel)"

    async def refresh(self, force: bool = False) -> None:
        if self.handle is None or (not force and self._now() - self.last_edit < EDIT_EVERY_S):
            return
        text = self.text()
        if text == self.last_text:
            return
        self.last_text, self.last_edit = text, self._now()
        with contextlib.suppress(Exception):  # an edit that fails (network, flood limit) is just skipped
            await self._edit(self.handle, text)

    async def _run(self) -> None:
        await asyncio.sleep(SHOW_AFTER_S)
        with contextlib.suppress(Exception):
            self.handle = await self._send(self.text())
            self.last_text, self.last_edit = self.text(), self._now()
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            await self.refresh(force=True)

    def start(self) -> None:
        self._loop = asyncio.create_task(self._run(), name=f"live-status-{self.task_id}")

    async def finish(self, status: str) -> None:
        """Stop updating and, if the status message was shown, say how the task ended."""
        if self._loop is not None:
            self._loop.cancel()
            with contextlib.suppress(BaseException):
                await self._loop
        if self.handle is None:
            return
        took = clock(self._now() - self.started)
        steps = f" · {self.steps} step{'s' if self.steps != 1 else ''}" if self.steps else ""
        line = {"done": f"✅ Finished in {took}{steps}", "cancelled": f"⏹ Stopped after {took}",
                "needs_user": f"❓ I need your answer to carry on ({took})"}.get(
                    status, f"❌ Could not finish ({took}). The reason is below.")
        with contextlib.suppress(Exception):
            await self._edit(self.handle, line)
