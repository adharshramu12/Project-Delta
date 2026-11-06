"""The scheduler loop, inside the background service (`delta run`).

Every few seconds (or right away when a schedule changes) it looks for due schedules and:
- runs each one through Container.submit, the same path as a typed request: Orchestrator, ToolExecutor,
  policy, approvals and Remote Guard. A schedule made from Telegram or Slack runs as that remote user,
  so sensitive tools need the PIN and a locked Remote Guard refuses it. Its permission level is never
  higher than when it was made (Container.submit autonomy_cap), and nothing here can raise it.
- reminders skip the AI completely: the message goes straight to the owner.
- never starts a schedule that is still running (the late run is recorded as skipped).
- caught up after the PC was off or asleep: a run that is late by no more than the grace window
  (default 1 hour) runs once; an older one is recorded as missed and the schedule moves on.
- holds task runs during quiet hours and runs them once when quiet hours end (reminders still arrive).
- runs at most `max_concurrent` scheduled tasks at once (default 1); Delta still does one heavy task at
  a time overall.

next_run is moved forward before a run starts, so a crash or power cut during a run never repeats it.
Approvals needed by an unattended run go to every approval channel (Telegram buttons when paired, the
dashboard) like any other approval, and an unanswered one times out to "no".
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Callable
from datetime import datetime, timedelta
from datetime import time as dtime
from typing import Any

from delta.core.state import TaskRequest, TaskResult
from delta.safety.remote_guard import is_remote
from delta.scheduler.rules import fmt_when, next_run, now_utc, to_local
from delta.scheduler.store import Schedule, ScheduleStore
from delta.system.logging import log

AUTONOMY_ORDER = ["ask-everything", "ask-risky", "trusted", "autopilot"]
CHECK_EVERY_S = 20.0  # longest sleep between checks: also notices a wake from sleep within 20 s
SCHEDULED_CHAT = "Scheduled tasks"
ICON = {"done": "✅", "failed": "❌", "cancelled": "⏹", "needs_user": "❓", "missed": "⏰", "skipped": "⏭",
        "reminder": "⏰"}


def lower_autonomy(a: str | None, b: str) -> str:
    """The more careful of two permission levels (unknown or empty `a` changes nothing)."""
    if not a or a not in AUTONOMY_ORDER or b not in AUTONOMY_ORDER:
        return b
    return AUTONOMY_ORDER[min(AUTONOMY_ORDER.index(a), AUTONOMY_ORDER.index(b))]


def in_quiet_hours(quiet: Any, now_local: datetime) -> bool:
    if quiet is None or not getattr(quiet, "enabled", False):
        return False
    start, end = dtime.fromisoformat(quiet.start), dtime.fromisoformat(quiet.end)
    t = now_local.time()
    return start <= t < end if start < end else (t >= start or t < end)


def short(text: str, limit: int = 700) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class Scheduler:
    def __init__(self, store: ScheduleStore, *, grace_minutes: float = 60, max_concurrent: int = 1,
                 enabled: bool = True, clock: Callable[[], datetime] = now_utc,
                 alert: Callable[[str, str], None] | None = None) -> None:
        self.store = store
        self.grace = timedelta(minutes=max(0.0, grace_minutes))
        self.enabled = enabled
        self.clock = clock
        self.alert = alert
        self._sem = asyncio.Semaphore(max(1, max_concurrent))
        self._running: dict[str, asyncio.Task[None]] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self.c: Any = None  # the Container, bound after it is built (bootstrap)

    def bind(self, container: Any) -> None:
        self.c = container

    # --- lifecycle ------------------------------------------------------------------------------------
    @property
    def started(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    async def start(self) -> None:
        if not self.enabled or self.started:
            return
        self._stopping = False
        self._loop_task = asyncio.create_task(self._loop(), name="scheduler")
        log.info("scheduler.started", schedules=len(self.store.list()))

    async def stop(self) -> None:
        """Graceful: no new runs, running ones are stopped and recorded as cancelled."""
        self._stopping = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._loop_task
            self._loop_task = None
        running = list(self._running.values())
        for t in running:
            t.cancel()
        if running:
            await asyncio.wait(running, timeout=2)
        log.info("scheduler.stopped")

    def poke(self) -> None:
        """A schedule changed: check now instead of at the next tick."""
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler.tick_failed")
            wait = CHECK_EVERY_S
            if (soon := self.store.earliest()) is not None:
                wait = max(0.5, min(wait, (soon - self.clock()).total_seconds()))
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), wait)

    # --- one check --------------------------------------------------------------------------------------
    def _quiet_now(self, s: Schedule) -> bool:
        quiet = getattr(getattr(getattr(self.c, "settings", None), "boundaries", None), "quiet_hours", None)
        return in_quiet_hours(quiet, to_local(self.clock(), ""))  # quiet hours are in the PC's own time

    async def tick(self) -> list[asyncio.Task[None]]:
        """Start every due run. Returns the started runs (tests await them)."""
        now = self.clock()
        started: list[asyncio.Task[None]] = []
        for s in self.store.due(now):
            due = s.next_run or now
            if s.id in self._running:  # no overlap: the earlier run is still going
                nxt = next_run(s.rule, s.tz, now)
                self.store.record_run(s.id, "skipped", "the previous run was still going", due=due, started=now)
                self.store.set_next(s.id, nxt)
                continue
            if not s.held and now - due > self.grace:
                await self._missed(s, due, now)
                continue
            if s.kind == "task" and self._quiet_now(s):
                if not s.held:
                    self.store.set_held(s.id, True)
                    self.store.record_run(s.id, "held", "quiet hours: runs when they end", due=due, started=now)
                continue
            self.store.set_next(s.id, next_run(s.rule, s.tz, max(now, due)))  # before running: never repeated
            started.append(self._launch(s, due, "schedule"))
        return started

    async def _missed(self, s: Schedule, due: datetime, now: datetime) -> None:
        nxt = next_run(s.rule, s.tz, now)
        why = f"missed: due {fmt_when(due, s.tz)}, the PC was off or asleep for longer than the grace window"
        self.store.record_run(s.id, "missed", why, due=due, started=now)
        self.store.finish(s.id, now, "missed", why, nxt, disable=nxt is None)
        log.info("scheduler.missed", schedule=s.id, due=due.isoformat())
        what = f"reminder: {s.prompt}" if s.kind == "reminder" else f"scheduled task \"{s.name}\""
        await self.deliver(s, f"{ICON['missed']} I missed your {what} (due {fmt_when(due, s.tz)}) because the PC "
                              "was off or asleep. "
                           + (f"Next run: {fmt_when(nxt, s.tz)}." if nxt else "It will not run again."),
                           s.owner_channel, s.owner_user, "missed")

    def _launch(self, s: Schedule, due: datetime | None, trigger: str, channel: str = "", user: str = "",
                ) -> asyncio.Task[None]:
        task = asyncio.create_task(self._execute(s, due, trigger, channel or s.owner_channel,
                                                 user or s.owner_user), name=f"schedule-{s.id}")
        self._running[s.id] = task
        task.add_done_callback(lambda _: self._running.pop(s.id, None))
        return task

    def is_running(self, sid: str) -> bool:
        return sid in self._running

    def run_now(self, s: Schedule, by_channel: str = "", by_user: str = "") -> tuple[bool, str]:
        """Start a schedule now, outside its timetable (its next run stays). A run asked for from a phone
        runs as that phone, so Remote Guard applies even to a schedule made on the PC."""
        if s.id in self._running:
            return False, f"\"{s.name}\" is already running."
        channel, user = s.owner_channel, s.owner_user
        if by_channel and is_remote(by_channel):
            channel, user = by_channel, by_user
        self._launch(s, None, "manual", channel, user)
        return True, f"Started \"{s.name}\" now. I'll send the result when it's done."

    async def _execute(self, s: Schedule, due: datetime | None, trigger: str, channel: str, user: str) -> None:
        started = self.clock()
        status, summary, task_id = "failed", "", ""
        try:
            if s.kind == "reminder":
                status, summary = "done", s.prompt
                await self.deliver(s, f"{ICON['reminder']} Reminder: {s.prompt}", channel, user, "done")
            else:
                async with self._sem:
                    res = await self._submit(s, channel, user)
                task_id = res.task_id
                status, summary = res.status, res.answer
                await self.deliver(s, self._result_text(s, res), channel, user, status)
        except asyncio.CancelledError:
            status, summary = "cancelled", "stopped (Delta was shutting down or the run was stopped)"
            self._record(s, due, started, status, summary, task_id, trigger)
            raise
        except Exception as e:
            log.exception("scheduler.run_failed", schedule=s.id)
            status, summary = "failed", f"{type(e).__name__}: {e}"
            with contextlib.suppress(Exception):
                await self.deliver(s, f"{ICON['failed']} Scheduled task \"{s.name}\" failed: {short(summary, 300)}",
                                   channel, user, status)
        self._record(s, due, started, status, summary, task_id, trigger)

    def _record(self, s: Schedule, due: datetime | None, started: datetime, status: str, summary: str,
                task_id: str, trigger: str) -> None:
        finished = self.clock()
        try:
            self.store.record_run(s.id, status, short(summary, 1000), due=due, started=started, finished=finished,
                                  task_id=task_id, trigger=trigger)
            current = self.store.get(s.id)
            if current is None:  # deleted while it ran
                return
            once_done = current.rule.type == "once" and trigger == "schedule"
            nxt = None if once_done else current.next_run
            self.store.finish(s.id, started, status, short(summary, 500), nxt,
                              disable=once_done or (current.enabled and nxt is None))
            if self.c is not None and getattr(self.c, "audit", None) is not None:
                self.c.audit.record("schedule.run", schedule=s.id, status=status, trigger=trigger, task=task_id)
        except Exception:
            log.exception("scheduler.record_failed", schedule=s.id)

    async def _submit(self, s: Schedule, channel: str, user: str) -> TaskResult:
        if self.c is None:
            raise RuntimeError("the scheduler is not connected to Delta")
        req = TaskRequest(s.prompt, channel=channel or "api", user=user or "owner")
        submit = self.c.submit
        if "autonomy_cap" in inspect.signature(submit).parameters:
            result: TaskResult = await submit(req, autonomy_cap=s.autonomy or None)
            return result
        result = await submit(req)  # a stand-in container (tests)
        return result

    @staticmethod
    def _result_text(s: Schedule, res: TaskResult) -> str:
        if res.status == "done":
            return f"{ICON['done']} {s.name}\n{short(res.answer)}"
        if res.status == "needs_user":
            return f"{ICON['needs_user']} Scheduled task \"{s.name}\" needs you: {short(res.answer)}"
        if res.status == "cancelled":
            return f"{ICON['cancelled']} Scheduled task \"{s.name}\" stopped: {short(res.answer, 300)}"
        return f"{ICON['failed']} Scheduled task \"{s.name}\" failed: {short(res.answer, 400)}"

    # --- delivery ---------------------------------------------------------------------------------------
    async def deliver(self, s: Schedule, text: str, channel: str, user: str, status: str) -> str:
        """To the chat the schedule came from (Telegram, Slack); always also shown on the dashboard.
        Returns where it went."""
        where = "dashboard"
        c = self.c
        if c is not None and is_remote(channel):
            ch = getattr(c, "live_channels", {}).get(channel)
            send = getattr(ch, "send_text", None)
            if callable(send) and user:
                try:
                    await send(user, text)
                    where = channel
                except Exception as e:
                    log.warning("scheduler.deliver_failed", channel=channel, error=str(e))
        if c is not None:
            with contextlib.suppress(Exception):
                await c.events.publish("schedule.result", schedule_id=s.id, name=s.name, status=status,
                                       text=text, delivered=where)
            chats = getattr(c, "chats", None)
            if chats is not None and where == "dashboard":
                with contextlib.suppress(Exception):
                    found = next((ch for ch in chats.list(SCHEDULED_CHAT) if ch.title == SCHEDULED_CHAT), None)
                    chat_id = (found or chats.create(SCHEDULED_CHAT)).id
                    chats.add(chat_id, "delta", text, {"status": status if status in ("done", "failed") else "done",
                                                       "schedule_id": s.id})
        if where == "dashboard" and self.alert is not None:
            with contextlib.suppress(Exception):
                self.alert("Delta: " + s.name[:60], short(text, 200))
        return where
