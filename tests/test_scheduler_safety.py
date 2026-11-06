"""Schedules never get around Delta's safety rules: the PIN for making or turning on a schedule from a phone,
the lock, a confirmation of the parsed schedule before saving (at every permission level, exactly once),
approvals during unattended runs (shown on every approval channel, no answer = no), Remote Guard for runs
made from a phone, and a permission level that can only go down. Fakes only: no Telegram, no network."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from delta.config import Boundaries
from delta.core.intents import match_intent
from delta.core.state import CURRENT_REQUEST, TaskRequest, TaskResult
from delta.safety import remote_guard
from delta.safety.approvals import ApprovalBroker, ApprovalRequest
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine
from delta.safety.remote_guard import RemoteGuard
from delta.scheduler.rules import Rule
from delta.scheduler.service import Scheduler
from delta.scheduler.store import ScheduleStore
from delta.scheduler.tools import ScheduleContext, schedule_tools
from delta.tools.filesystem import filesystem_tools
from delta.tools.registry import ToolExecutor, ToolRegistry

TEXT = "every day at 9 am check my attendance on the ERP"


def make_guard(tmp_path: Path, pin: str | None = "4821") -> RemoteGuard:
    store: dict = {}
    audit = SimpleNamespace(record=lambda e, **k: None)
    g = RemoteGuard(tmp_path / "guard", audit, alert=lambda t, x: None, get_secret=store.get,
                    set_secret=store.__setitem__)
    if pin:
        g.set_pin(pin, by="dashboard")
    return g


class Setup:
    def __init__(self, tmp_path: Path, autonomy: str = "ask-risky", answer: bool | None = True,
                 timeout: float = 1.0) -> None:
        ok = tmp_path / "ok"
        ok.mkdir(exist_ok=True)
        self.store = ScheduleStore(tmp_path / "schedules.db")
        self.broker = ApprovalBroker(timeout_s=timeout)
        self.asked: list[ApprovalRequest] = []

        async def presenter(req: ApprovalRequest) -> None:
            self.asked.append(req)
            if answer is not None:
                self.broker.resolve(req.id, answer, by="dashboard")

        self.broker.add_presenter("dashboard", presenter)
        self.ctx = ScheduleContext(self.store, None, self.broker, timezone="Asia/Kolkata")
        registry = ToolRegistry([*filesystem_tools(), *schedule_tools(self.ctx)])
        self.ex = ToolExecutor(registry, PolicyEngine(Boundaries(autonomy=autonomy, allowed_paths=[str(ok)])),
                               self.broker, AuditLog(tmp_path / "audit.jsonl"))
        self.ctx.executor = self.ex
        self.guard: RemoteGuard | None = None

    def with_guard(self, tmp_path: Path, pin: str | None = "4821") -> RemoteGuard:
        self.guard = make_guard(tmp_path, pin)
        self.ex.guard = self.guard
        self.broker.guard = self.guard
        return self.guard

    async def call(self, tool: str, args: dict, channel: str = "api", user: str = "dashboard"):
        req = TaskRequest("x", channel=channel, user=user)
        token = CURRENT_REQUEST.set(req)
        try:
            return await self.ex.execute(tool, args, req.id)
        finally:
            CURRENT_REQUEST.reset(token)


@pytest.mark.parametrize("autonomy", ["ask-everything", "ask-risky", "trusted", "autopilot"])
async def test_the_parsed_schedule_is_confirmed_exactly_once_at_every_level(tmp_path, autonomy):
    s = Setup(tmp_path, autonomy)
    r = await s.call("schedule_task", {"text": TEXT})
    assert r.ok, r.output
    assert len(s.asked) == 1
    shown = s.asked[0].summary
    assert "Every day at 9:00 AM" in shown and "check my attendance on the ERP" in shown and "Next run:" in shown
    saved = s.store.list()
    assert len(saved) == 1 and saved[0].owner_channel == "api" and saved[0].autonomy == autonomy


async def test_saying_no_saves_nothing(tmp_path):
    s = Setup(tmp_path, "trusted", answer=False)
    r = await s.call("schedule_task", {"text": TEXT})
    assert not r.ok and r.evidence.get("user_denied") and s.store.list() == []


async def test_no_answer_saves_nothing(tmp_path):
    s = Setup(tmp_path, "autopilot", answer=None, timeout=0.05)
    r = await s.call("schedule_task", {"text": TEXT})
    assert not r.ok and s.store.list() == []


@pytest.mark.parametrize("tool", ["schedule_task", "resume_schedule"])
async def test_creating_or_turning_on_from_a_phone_needs_the_pin(tmp_path, tool):
    s = Setup(tmp_path)
    s.with_guard(tmp_path)
    paused = s.store.add("erp", "check attendance", Rule("daily", time="09:00"))
    s.store.set_enabled(paused.id, False)
    args = {"text": TEXT} if tool == "schedule_task" else {"schedule": paused.id}
    r = await s.call(tool, args, channel="telegram", user="42")
    assert not r.ok and r.evidence.get("needs_pin") and "PIN" in r.output
    assert s.asked == []  # refused before any approval is shown
    assert len(s.store.list()) == 1 and not s.store.get(paused.id).enabled


async def test_with_the_pin_a_phone_can_make_a_schedule_that_belongs_to_that_phone(tmp_path):
    s = Setup(tmp_path)
    g = s.with_guard(tmp_path)
    assert g.try_pin("42", "4821")[0]

    async def phone_presenter(req: ApprovalRequest) -> None:  # the owner taps Approve in Telegram
        s.asked.append(req)
        s.broker.answer(req.id, True, by="telegram:42")

    s.broker._presenters = {"telegram": phone_presenter}
    r = await s.call("schedule_task", {"text": TEXT}, channel="telegram", user="42")
    assert r.ok, r.output
    saved = s.store.list()[0]
    assert (saved.owner_channel, saved.owner_user) == ("telegram", "42")


async def test_locked_remote_control_refuses_schedules(tmp_path):
    s = Setup(tmp_path)
    g = s.with_guard(tmp_path)
    g.lock("test", by="test")
    r = await s.call("schedule_task", {"text": TEXT}, channel="telegram", user="42")
    assert not r.ok and "locked" in r.output.lower() and s.store.list() == []


async def test_pausing_listing_and_deleting_from_a_phone_need_no_pin(tmp_path):
    s = Setup(tmp_path, "trusted")
    s.with_guard(tmp_path)
    sch = s.store.add("erp", "check attendance", Rule("daily", time="09:00"))
    assert (await s.call("list_schedules", {}, channel="telegram", user="42")).ok
    assert (await s.call("pause_schedule", {"schedule": "erp"}, channel="telegram", user="42")).ok
    assert not s.store.get(sch.id).enabled
    assert (await s.call("delete_schedule", {"schedule": sch.id}, channel="telegram", user="42")).ok
    assert s.store.get(sch.id) is None


def test_schedules_are_listed_as_sensitive():
    assert "schedule" in remote_guard.SENSITIVE
    assert remote_guard.needs_remote_pin("schedule_task") and remote_guard.needs_remote_pin("resume_schedule")
    assert remote_guard.needs_remote_pin("pause_schedule") is None


# --- unattended runs --------------------------------------------------------------------------------------

class RunContainer:
    """Stands in for Container.submit: runs one tool call the way the orchestrator would, under the request."""

    def __init__(self, ex: ToolExecutor, tool: str, args: dict, guard: RemoteGuard | None = None) -> None:
        self.ex, self.tool, self.args, self.guard = ex, tool, args, guard
        self.events = SimpleNamespace(publish=self._publish)
        self.live_channels: dict = {}
        self.chats = None
        self.audit = SimpleNamespace(record=lambda *a, **k: None)
        self.settings = SimpleNamespace(boundaries=Boundaries())
        self.results: list = []

    async def _publish(self, event: str, **data) -> None:
        self.results.append((event, data))

    async def submit(self, req: TaskRequest, autonomy_cap: str | None = None) -> TaskResult:
        if self.guard is not None and self.guard.locked and remote_guard.is_remote(req.channel):
            return TaskResult(req.id, "failed", self.guard.locked_message())
        token = CURRENT_REQUEST.set(req)
        try:
            r = await self.ex.execute(self.tool, self.args, req.id)
        finally:
            CURRENT_REQUEST.reset(token)
        return TaskResult(req.id, "done" if r.ok else "failed", r.output)


T0 = datetime(2026, 11, 6, 3, 0, tzinfo=UTC)


async def run_once(tmp_path: Path, s: Setup, c: RunContainer, owner: tuple[str, str]) -> object:
    sched = Scheduler(s.store, clock=lambda: T0 + timedelta(hours=1), alert=lambda t, x: None)
    sched.bind(c)
    item = s.store.add("job", "do the job", Rule("once", at="2026-11-06T09:00"), tz="Asia/Kolkata",
                       owner_channel=owner[0], owner_user=owner[1], now=T0)
    for t in await sched.tick():
        await t
    return s.store.get(item.id)


async def test_an_approval_in_an_unattended_run_reaches_the_owner_and_times_out_to_no(tmp_path):
    s = Setup(tmp_path, "ask-risky", answer=None, timeout=0.05)
    target = tmp_path / "ok" / "report.txt"
    c = RunContainer(s.ex, "write_file", {"path": str(target), "content": "hi"})
    after = await run_once(tmp_path, s, c, ("api", "dashboard"))
    assert len(s.asked) == 1 and s.asked[0].tool == "write_file"  # shown to the approval channels
    assert not target.exists()  # nobody answered: denied
    assert after.last_status == "failed" and "DENIED" in after.last_message


async def test_a_run_made_from_a_phone_is_remote_and_needs_the_pin_for_sensitive_tools(tmp_path):
    s = Setup(tmp_path, "autopilot")
    g = s.with_guard(tmp_path)
    secret = tmp_path / "ok" / "notes.txt"
    secret.write_text("bank pin 1234")
    c = RunContainer(s.ex, "read_file", {"path": str(secret)}, guard=g)
    after = await run_once(tmp_path, s, c, ("telegram", "42"))
    assert after.last_status == "failed" and "PIN" in after.last_message and "1234" not in after.last_message


async def test_a_run_from_a_phone_is_refused_while_locked(tmp_path):
    s = Setup(tmp_path)
    g = s.with_guard(tmp_path)
    g.lock("test", by="test")
    c = RunContainer(s.ex, "list_schedules", {}, guard=g)
    after = await run_once(tmp_path, s, c, ("telegram", "42"))
    assert after.last_status == "failed" and "locked" in after.last_message.lower()


async def test_the_same_schedule_from_the_pc_reads_without_a_pin(tmp_path):
    s = Setup(tmp_path, "autopilot")
    s.with_guard(tmp_path)
    note = tmp_path / "ok" / "notes.txt"
    note.write_text("hello")
    c = RunContainer(s.ex, "read_file", {"path": str(note)})
    after = await run_once(tmp_path, s, c, ("api", "dashboard"))
    assert after.last_status == "done"


# --- permission level ---------------------------------------------------------------------------------

async def test_a_scheduled_run_never_gets_a_higher_level_than_when_it_was_made(tmp_path, monkeypatch):
    import delta.bootstrap as boot
    from delta.bootstrap import Container

    built: list[str] = []

    def fake_make_policy(settings):  # make_policy needs Windows folders; the cap logic is what is tested
        built.append(settings.boundaries.autonomy)
        return PolicyEngine(settings.boundaries.model_copy(deep=True))

    monkeypatch.setattr(boot, "make_policy", fake_make_policy)
    from delta.config import Settings

    settings = Settings(data_dir=tmp_path)
    settings.boundaries.autonomy = "autopilot"
    seen: list[str] = []
    executor = SimpleNamespace(policy=PolicyEngine(settings.boundaries.model_copy(deep=True)))

    async def run(req):
        seen.append(str(executor.policy.b.autonomy))
        return TaskResult(req.id, "done", "ok")

    fake = SimpleNamespace(guard=None, audit=SimpleNamespace(record=lambda *a, **k: None),
                           _task_lock=asyncio.Semaphore(1), running=None,
                           orchestrator=SimpleNamespace(run=run, executor=executor), settings=settings)
    fake._cap_autonomy = lambda cap: Container._cap_autonomy(fake, cap)  # type: ignore[attr-defined]
    await Container.submit(fake, TaskRequest("x"), autonomy_cap="ask-risky")  # type: ignore[arg-type]
    assert seen == ["ask-risky"]
    assert executor.policy.b.autonomy == "autopilot"  # today's level is back after the run
    await Container.submit(fake, TaskRequest("y"), autonomy_cap="autopilot")  # type: ignore[arg-type]
    settings.boundaries.autonomy = "ask-risky"
    executor.policy = PolicyEngine(settings.boundaries.model_copy(deep=True))
    await Container.submit(fake, TaskRequest("z"), autonomy_cap="autopilot")  # type: ignore[arg-type]
    assert seen == ["ask-risky", "autopilot", "ask-risky"]  # never raised above the current level


# --- natural language entry ---------------------------------------------------------------------------

def test_schedule_sentences_go_straight_to_schedule_task():
    tools = {"schedule_task", "schedule_meeting"}
    for text in ["remind me at 6 pm to call Ravi", TEXT, "every Monday 10 am summarise my downloads folder"]:
        intent = match_intent(text, tools)
        assert intent is not None and intent.tool == "schedule_task" and intent.args == {"text": text}
    assert match_intent("every Monday at 10 schedule a meeting with Ravi", tools) is None  # a calendar job
    assert match_intent("every file in downloads, list them", tools) is None
    assert match_intent("remind me to call Ravi", tools) is None  # no time: the planner asks
    assert match_intent(TEXT, {"schedule_meeting"}) is None  # tool not available
