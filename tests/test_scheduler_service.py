"""The scheduler loop: persistence across restarts, missed runs and the grace window, no overlapping runs,
bounded concurrency, quiet hours, reminders without the AI, delivery and graceful shutdown. Fakes only."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from delta.config import QuietHours
from delta.core.state import TaskRequest, TaskResult
from delta.scheduler.rules import Rule, iso
from delta.scheduler.service import Scheduler, in_quiet_hours, lower_autonomy
from delta.scheduler.store import ScheduleStore

T0 = datetime(2026, 11, 6, 3, 0, tzinfo=UTC)


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class Events:
    def __init__(self) -> None:
        self.seen: list[tuple[str, dict]] = []

    async def publish(self, event: str, **data) -> None:
        self.seen.append((event, data))


class Phone:
    name = "telegram"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, user: str, text: str) -> None:
        self.sent.append((user, text))


class FakeContainer:
    def __init__(self, answer: str = "Attendance is 87%", status: str = "done", gate: asyncio.Event | None = None,
                 quiet: QuietHours | None = None) -> None:
        self.calls: list[tuple[TaskRequest, str | None]] = []
        self.answer, self.status, self.gate = answer, status, gate
        self.active = self.max_active = 0
        self.events = Events()
        self.phone = Phone()
        self.live_channels = {"telegram": self.phone}
        self.chats = None
        self.audit = SimpleNamespace(record=lambda *a, **k: None)
        self.settings = SimpleNamespace(boundaries=SimpleNamespace(quiet_hours=quiet or QuietHours()))

    async def submit(self, req: TaskRequest, autonomy_cap: str | None = None) -> TaskResult:
        self.calls.append((req, autonomy_cap))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            return TaskResult(req.id, self.status, self.answer)  # type: ignore[arg-type]
        finally:
            self.active -= 1


def make(tmp_path: Path, clock: Clock, c: FakeContainer | None = None, **kw) -> tuple[Scheduler, ScheduleStore,
                                                                                         FakeContainer]:
    store = ScheduleStore(tmp_path / "schedules.db")
    alerts: list = []
    s = Scheduler(store, clock=clock, alert=lambda t, x: alerts.append((t, x)), **kw)
    c = c or FakeContainer()
    s.bind(c)
    s.alerts = alerts  # type: ignore[attr-defined]
    return s, store, c


async def run_due(s: Scheduler) -> None:
    for t in await s.tick():
        await t


async def test_a_due_task_runs_through_submit_as_its_owner_and_the_result_goes_back_to_that_chat(tmp_path):
    clock = Clock(T0)
    s, store, c = make(tmp_path, clock)
    sch = store.add("ERP", "check my attendance on the ERP", Rule("daily", time="09:00"), tz="Asia/Kolkata",
                    owner_channel="telegram", owner_user="42", autonomy="ask-risky", now=T0 - timedelta(days=1))
    assert sch.next_run is not None and sch.next_run <= T0 + timedelta(minutes=30)
    clock.t = sch.next_run + timedelta(seconds=5)
    await run_due(s)
    req, cap = c.calls[0]
    assert (req.text, req.channel, req.user, cap) == ("check my attendance on the ERP", "telegram", "42", "ask-risky")
    assert c.phone.sent and c.phone.sent[0][0] == "42" and "87%" in c.phone.sent[0][1]
    after = store.get(sch.id, with_history=True)
    assert after.last_status == "done" and after.next_run == sch.next_run + timedelta(days=1)
    assert after.history[0]["status"] == "done"
    assert any(e == "schedule.result" for e, _ in c.events.seen)


async def test_a_reminder_only_sends_the_message_no_ai(tmp_path):
    clock = Clock(T0)
    s, store, c = make(tmp_path, clock)
    r = store.add("Ravi", "call Ravi", Rule("once", at="2026-11-06T18:00"), kind="reminder", tz="Asia/Kolkata",
                  owner_channel="api", owner_user="dashboard", now=T0)
    clock.t = r.next_run
    await run_due(s)
    assert c.calls == []  # no planner, no model
    assert s.alerts and "Reminder: call Ravi" in s.alerts[0][1]  # type: ignore[attr-defined]
    done = store.get(r.id)
    assert done.last_status == "done" and not done.enabled and done.next_run is None  # one-time: finished


async def test_schedules_survive_a_restart(tmp_path):
    clock = Clock(T0)
    _s, store, _ = make(tmp_path, clock)
    sch = store.add("x", "summarise downloads", Rule("weekly", time="10:00", days=[0]), tz="Asia/Kolkata", now=T0)
    store.close()
    store2 = ScheduleStore(tmp_path / "schedules.db")
    again = store2.get(sch.id)
    assert again is not None and again.next_run == sch.next_run and again.rule == sch.rule and again.enabled
    s2 = Scheduler(store2, clock=Clock(sch.next_run))
    c2 = FakeContainer()
    s2.bind(c2)
    await run_due(s2)
    assert len(c2.calls) == 1


async def test_missed_while_off_runs_once_within_grace(tmp_path):
    clock = Clock(T0)
    s, store, c = make(tmp_path, clock, grace_minutes=60)
    sch = store.add("check", "check mail", Rule("interval", minutes=15, anchor=iso(T0)), now=T0)
    clock.t = T0 + timedelta(minutes=50)  # asleep: three runs were due, the first 35 minutes ago
    await run_due(s)
    assert len(c.calls) == 1  # caught up once, not three times
    assert store.get(sch.id).next_run == T0 + timedelta(minutes=60)


async def test_missed_beyond_grace_is_recorded_and_skipped(tmp_path):
    clock = Clock(T0)
    s, store, c = make(tmp_path, clock, grace_minutes=60)
    sch = store.add("ERP", "check attendance", Rule("daily", time="09:00"), tz="Asia/Kolkata",
                    owner_channel="telegram", owner_user="42", now=T0 - timedelta(days=1))
    clock.t = sch.next_run + timedelta(hours=3)  # the PC was off all morning
    await run_due(s)
    assert c.calls == []
    after = store.get(sch.id, with_history=True)
    assert after.last_status == "missed" and after.history[0]["status"] == "missed"
    assert after.next_run == sch.next_run + timedelta(days=1)
    assert "missed" in c.phone.sent[0][1]


async def test_no_overlap_of_the_same_schedule(tmp_path):
    clock = Clock(T0)
    gate = asyncio.Event()
    s, store, c = make(tmp_path, clock, FakeContainer(gate=gate))
    sch = store.add("slow", "long job", Rule("interval", minutes=5, anchor=iso(T0)), now=T0 - timedelta(minutes=1))
    clock.t = T0 + timedelta(seconds=1)
    first = await s.tick()
    await asyncio.sleep(0)
    clock.t = T0 + timedelta(minutes=5, seconds=1)  # due again while the first run is still going
    assert await s.tick() == []
    assert s.is_running(sch.id) and len(c.calls) == 1
    hist = store.history(sch.id)
    assert hist[0]["status"] == "skipped"
    gate.set()
    await asyncio.gather(*first)
    assert not s.is_running(sch.id)


async def test_concurrency_is_bounded(tmp_path):
    clock = Clock(T0)
    gate = asyncio.Event()
    s, store, c = make(tmp_path, clock, FakeContainer(gate=gate), max_concurrent=1)
    for i in range(3):
        store.add(f"job{i}", f"job {i}", Rule("once", at="2026-11-06T09:00"), tz="Asia/Kolkata", now=T0)
    clock.t = datetime(2026, 11, 6, 3, 30, tzinfo=UTC)
    started = await s.tick()
    await asyncio.sleep(0.01)
    assert len(started) == 3 and c.max_active == 1
    gate.set()
    await asyncio.gather(*started)
    assert len(c.calls) == 3 and c.max_active == 1


async def test_quiet_hours_hold_task_runs_until_they_end_but_reminders_arrive(tmp_path, monkeypatch):
    import delta.scheduler.service as svc

    quiet = QuietHours(enabled=True, start="22:00", end="07:00")
    clock = Clock(T0)
    s, store, c = make(tmp_path, clock, FakeContainer(quiet=quiet), grace_minutes=60)
    local = {"now": datetime(2026, 11, 6, 23, 0)}
    monkeypatch.setattr(svc, "to_local", lambda instant, tz: local["now"])
    task = store.add("night", "back up", Rule("once", at="2026-11-06T09:00"), tz="Asia/Kolkata", now=T0)
    rem = store.add("pill", "take pill", Rule("once", at="2026-11-06T09:00"), kind="reminder", tz="Asia/Kolkata",
                    now=T0)
    clock.t = task.next_run
    await run_due(s)
    assert c.calls == [] and store.get(task.id).held  # held, not missed
    assert store.get(rem.id).last_status == "done"  # the reminder still came
    clock.t = task.next_run + timedelta(hours=8)  # far past the grace window, but it was held on purpose
    local["now"] = datetime(2026, 11, 7, 7, 5)
    await run_due(s)
    assert len(c.calls) == 1 and store.get(task.id).last_status == "done"


async def test_a_failure_is_reported_with_the_reason(tmp_path):
    clock = Clock(T0)
    s, store, c = make(tmp_path, clock, FakeContainer(answer="The ERP login page did not load", status="failed"))
    sch = store.add("ERP", "check attendance", Rule("once", at="2026-11-06T09:00"), tz="Asia/Kolkata",
                    owner_channel="slack", owner_user="U1", now=T0)
    c.live_channels["slack"] = c.phone
    clock.t = sch.next_run
    await run_due(s)
    assert store.get(sch.id).last_status == "failed"
    assert "failed" in c.phone.sent[0][1] and "did not load" in c.phone.sent[0][1]


async def test_a_crash_inside_a_run_is_caught_and_recorded(tmp_path):
    clock = Clock(T0)
    s, store, c = make(tmp_path, clock)

    async def boom(req, autonomy_cap=None):
        raise RuntimeError("disk full")

    c.submit = boom  # type: ignore[method-assign]
    sch = store.add("x", "x", Rule("once", at="2026-11-06T09:00"), tz="Asia/Kolkata", now=T0)
    clock.t = sch.next_run
    await run_due(s)
    after = store.get(sch.id)
    assert after.last_status == "failed" and "disk full" in after.last_message


async def test_no_channel_means_the_dashboard_gets_it(tmp_path):
    clock = Clock(T0)
    s, store, c = make(tmp_path, clock)
    c.live_channels = {}  # Telegram is not running right now
    sch = store.add("x", "x", Rule("once", at="2026-11-06T09:00"), tz="Asia/Kolkata", owner_channel="telegram",
                    owner_user="42", now=T0)
    clock.t = sch.next_run
    await run_due(s)
    assert s.alerts  # type: ignore[attr-defined]
    assert next(d for e, d in c.events.seen if e == "schedule.result")["delivered"] == "dashboard"


async def test_run_now_from_a_phone_runs_as_that_phone(tmp_path):
    clock = Clock(T0)
    s, store, c = make(tmp_path, clock)
    sch = store.add("x", "summarise downloads", Rule("daily", time="09:00"), owner_channel="api",
                    owner_user="dashboard", now=T0)
    ok, _ = s.run_now(sch, "telegram", "42")
    assert ok
    await asyncio.gather(*s._running.values())
    assert (c.calls[0][0].channel, c.calls[0][0].user) == ("telegram", "42")
    assert store.get(sch.id).next_run == sch.next_run  # its timetable is unchanged
    _ok2, _ = s.run_now(sch, "dashboard", "dashboard")
    await asyncio.gather(*s._running.values())
    assert c.calls[1][0].channel == "api"


async def test_graceful_shutdown_cancels_and_records(tmp_path):
    clock = Clock(T0)
    s, store, _c = make(tmp_path, clock, FakeContainer(gate=asyncio.Event()))
    sch = store.add("x", "x", Rule("once", at="2026-11-06T09:00"), tz="Asia/Kolkata", now=T0)
    clock.t = sch.next_run
    await s.tick()
    await asyncio.sleep(0)
    await s.stop()
    assert store.get(sch.id).last_status == "cancelled"


async def test_the_loop_starts_runs_and_stops(tmp_path):
    clock = Clock(T0)
    s, store, c = make(tmp_path, clock)
    store.add("x", "x", Rule("interval", minutes=5, anchor=iso(T0)), now=T0 - timedelta(seconds=1))
    await s.start()
    assert s.started
    for _ in range(100):
        if c.calls:
            break
        await asyncio.sleep(0.01)
    await s.stop()
    assert c.calls and not s.started


async def test_disabled_scheduler_does_not_start(tmp_path):
    s, _, _ = make(tmp_path, Clock(T0), enabled=False)
    await s.start()
    assert not s.started


def test_history_is_bounded(tmp_path):
    store = ScheduleStore(tmp_path / "s.db", history_limit=3)
    sch = store.add("x", "x", Rule("daily", time="09:00"), now=T0)
    for i in range(10):
        store.record_run(sch.id, "done", f"run {i}")
    hist = store.history(sch.id)
    assert [h["summary"] for h in hist] == ["run 9", "run 8", "run 7"]


def test_pause_resume_edit_find_delete(tmp_path):
    store = ScheduleStore(tmp_path / "s.db")
    sch = store.add("check my attendance", "check my attendance on the ERP", Rule("daily", time="09:00"), now=T0)
    assert store.find("attendance").id == sch.id and store.find("nothing") is None
    assert not store.set_enabled(sch.id, False).enabled
    assert store.set_enabled(sch.id, True, now=T0).next_run is not None
    edited = store.edit(sch.id, rule=Rule("daily", time="08:00"), now=T0)
    assert edited.rule.time == "08:00"
    assert store.delete(sch.id) and store.get(sch.id) is None


def test_finished_once_cannot_be_resumed(tmp_path):
    import pytest

    store = ScheduleStore(tmp_path / "s.db")
    sch = store.add("x", "x", Rule("once", at="2026-11-06T09:00"), now=T0)
    store.set_enabled(sch.id, False)
    with pytest.raises(ValueError):
        store.set_enabled(sch.id, True, now=T0 + timedelta(days=2))


def test_autonomy_only_goes_down_and_quiet_hours_wrap_midnight():
    assert lower_autonomy("ask-risky", "autopilot") == "ask-risky"
    assert lower_autonomy("autopilot", "ask-risky") == "ask-risky"  # a schedule never raises it
    assert lower_autonomy("", "trusted") == "trusted" and lower_autonomy("bogus", "trusted") == "trusted"
    q = QuietHours(enabled=True, start="23:30", end="07:00")
    assert in_quiet_hours(q, datetime(2026, 1, 1, 2, 0)) and not in_quiet_hours(q, datetime(2026, 1, 1, 12, 0))
    assert not in_quiet_hours(QuietHours(), datetime(2026, 1, 1, 2, 0))
