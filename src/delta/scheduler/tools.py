"""Schedules as agent tools: schedule_task, list_schedules, pause_schedule, resume_schedule,
delete_schedule, run_schedule_now.

Every call passes the normal ToolExecutor gate (policy, approvals, Remote Guard). On top of that:
- schedule_task always shows the parsed schedule in plain words, with its next run time, and saves only
  after a yes. When the permission level already asks for this action, that approval is the
  confirmation (its summary is the same plain words); otherwise the tool asks itself. So the user
  confirms exactly once at every permission level, and a learned "trust" can never skip it.
- From a phone (Telegram, Slack) creating or turning a schedule back on needs the owner's PIN: these
  tools are in remote_guard.REMOTE_PIN_TOOLS, checked centrally before anything runs.
- A new schedule records who asked (channel and user) and the permission level at that moment; its runs
  go back to that chat and never get a higher level (see delta.scheduler.service).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from delta.core.state import CURRENT_REQUEST
from delta.safety.approvals import ApprovalRequest
from delta.scheduler.parse import Parsed, ParseError, parse_schedule
from delta.scheduler.rules import describe, fmt_when, next_run, now_utc, zone_label
from delta.scheduler.store import Schedule, ScheduleStore
from delta.tools.base import Risk, Tool, ToolResult


@dataclass
class ScheduleContext:
    """What the tools need. Filled in by bootstrap once the executor and scheduler exist."""
    store: ScheduleStore
    scheduler: Any = None  # delta.scheduler.service.Scheduler
    approvals: Any = None  # ApprovalBroker
    executor: Any = None  # ToolExecutor (for its current policy)
    timezone: str = ""


def preview(parsed: Parsed, tz: str) -> str:
    """The plain-words confirmation: what, when, the next run and the time zone."""
    nxt = next_run(parsed.rule, tz, now_utc())
    what = f"remind you to {parsed.prompt}" if parsed.kind == "reminder" else parsed.prompt
    when = describe(parsed.rule, tz)
    return (f"Save this schedule? {when[0].upper()}{when[1:]}: {what}. Next run: {fmt_when(nxt, tz)} "
            f"({zone_label(tz)}).")


def origin() -> tuple[str, str]:
    req = CURRENT_REQUEST.get()
    if req is None:
        return "api", ""
    return req.channel or "api", req.user or ""


def _line(s: Schedule) -> str:
    last = f" · last run: {s.last_status}" if s.last_status else ""
    kind = " (reminder)" if s.kind == "reminder" else ""
    return f"[{s.id}] {s.name}{kind}: {s.when}. Next: {fmt_when(s.next_run, s.tz) if s.enabled else 'paused'}{last}"


class ScheduleArgs(BaseModel):
    text: str = Field(description="The user's own words with the time and the task, e.g. 'every day at 9 am check my "
                                  "attendance on the ERP' or 'remind me at 6 pm to call Ravi'.")
    name: str = Field("", description="Optional short name for the schedule.")


class ScheduleTask(Tool[ScheduleArgs]):
    name = "schedule_task"
    description = ("Save a task or reminder to run later or on repeat: once (at 6 pm, tomorrow 9 am, in 20 minutes), "
                   "daily, weekly (every Monday 10 am, every weekday), every N minutes/hours (5 min or more), or "
                   "cron. Pass the user's words in `text`. 'remind me ...' only sends the message, no AI. "
                   "The user confirms the parsed schedule before it is saved.")
    risk = Risk.ACT
    Args = ScheduleArgs

    def __init__(self, ctx: ScheduleContext) -> None:
        self.ctx = ctx

    def _parse(self, args: ScheduleArgs) -> Parsed:
        return parse_schedule(args.text, self.ctx.timezone)

    def summary(self, args: ScheduleArgs) -> str:
        try:
            return preview(self._parse(args), self.ctx.timezone)
        except ParseError as e:
            return f"Save a schedule from: {args.text!r} ({e})"

    def _policy_asked(self, args: ScheduleArgs) -> bool:
        """True when ToolExecutor already asked the user with this same summary before running us."""
        executor = self.ctx.executor
        if executor is None:
            return False
        try:
            return str(executor.policy.evaluate(self, args).verdict) == "ask"
        except Exception:
            return False

    async def run(self, args: ScheduleArgs) -> ToolResult:
        tz = self.ctx.timezone
        try:
            parsed = self._parse(args)
        except ParseError as e:
            return ToolResult(False, f"I could not tell when to run it: {e}")
        text = preview(parsed, tz)
        if not self._policy_asked(args):
            if self.ctx.approvals is None:
                return ToolResult(False, "Schedules need a confirmation, and nobody can be asked right now.")
            req = CURRENT_REQUEST.get()
            outcome = await self.ctx.approvals.request(ApprovalRequest(
                tool=self.name, summary=text, risk=str(Risk.ACT), reason="please confirm the schedule before I save it",
                task_id=req.id if req else ""))
            if not outcome.approved:
                return ToolResult(False, "The user did not confirm the schedule, so nothing was saved.",
                                  {"user_denied": outcome.by != "timeout", "denied_action": text})
        channel, user = origin()
        autonomy = str(getattr(getattr(getattr(self.ctx.executor, "policy", None), "b", None), "autonomy", "") or "")
        s = self.ctx.store.add(args.name or parsed.name, parsed.prompt, parsed.rule, kind=parsed.kind, tz=tz,
                               owner_channel=channel, owner_user=user, autonomy=autonomy)
        if self.ctx.scheduler is not None:
            self.ctx.scheduler.poke()
        return ToolResult(True, f"Saved. {s.plain()} (id {s.id}; say 'pause {s.name}' or 'delete {s.name}' to change "
                                "it.)", {"verified": True, "schedule_id": s.id, "next_run": s.to_dict()["next_run"]})


class NoArgs(BaseModel):
    pass


class ListSchedules(Tool[NoArgs]):
    name = "list_schedules"
    description = "List the user's saved schedules and reminders with their next run and last result."
    risk = Risk.READ
    Args = NoArgs

    def __init__(self, ctx: ScheduleContext) -> None:
        self.ctx = ctx

    async def run(self, args: NoArgs) -> ToolResult:
        items = self.ctx.store.list()
        if not items:
            return ToolResult(True, "No schedules yet.", {"verified": True, "count": 0})
        return ToolResult(True, "\n".join(_line(s) for s in items), {"verified": True, "count": len(items)})


class RefArgs(BaseModel):
    schedule: str = Field(description="The schedule's id or a few words of its name.")


class _RefTool(Tool[RefArgs]):
    Args = RefArgs

    def __init__(self, ctx: ScheduleContext) -> None:
        self.ctx = ctx

    def _find(self, args: RefArgs) -> Schedule | None:
        return self.ctx.store.find(args.schedule)

    def summary(self, args: RefArgs) -> str:
        s = self._find(args)
        return f"{self.name.replace('_', ' ')}: {s.name} ({s.when})" if s else f"{self.name}({args.schedule!r})"

    def _missing(self, args: RefArgs) -> ToolResult:
        return ToolResult(False, f"No single schedule matches '{args.schedule}'. Use list_schedules and pass its id.")


class PauseSchedule(_RefTool):
    name = "pause_schedule"
    description = "Pause a saved schedule (it stays saved and can be resumed)."
    risk = Risk.ACT

    async def run(self, args: RefArgs) -> ToolResult:
        s = self._find(args)
        if s is None:
            return self._missing(args)
        self.ctx.store.set_enabled(s.id, False)
        return ToolResult(True, f"Paused \"{s.name}\".", {"verified": True, "schedule_id": s.id})


class ResumeSchedule(_RefTool):
    name = "resume_schedule"
    description = "Turn a paused schedule back on."
    risk = Risk.ACT

    async def run(self, args: RefArgs) -> ToolResult:
        s = self._find(args)
        if s is None:
            return self._missing(args)
        try:
            s2 = self.ctx.store.set_enabled(s.id, True)
        except ValueError as e:
            return ToolResult(False, str(e))
        if self.ctx.scheduler is not None:
            self.ctx.scheduler.poke()
        nxt = fmt_when(s2.next_run, s2.tz) if s2 else "?"
        return ToolResult(True, f"Resumed \"{s.name}\". Next run: {nxt}.", {"verified": True, "schedule_id": s.id})


class DeleteSchedule(_RefTool):
    name = "delete_schedule"
    description = "Delete a saved schedule and its history."
    risk = Risk.WRITE

    async def run(self, args: RefArgs) -> ToolResult:
        s = self._find(args)
        if s is None:
            return self._missing(args)
        self.ctx.store.delete(s.id)
        return ToolResult(True, f"Deleted \"{s.name}\".", {"verified": True, "schedule_id": s.id})


class RunScheduleNow(_RefTool):
    name = "run_schedule_now"
    description = "Run a saved schedule right now, once, without changing its timetable."
    risk = Risk.ACT

    async def run(self, args: RefArgs) -> ToolResult:
        s = self._find(args)
        if s is None:
            return self._missing(args)
        if self.ctx.scheduler is None:
            return ToolResult(False, "Schedules run in Delta's background service, which is not running here.")
        channel, user = origin()
        ok, message = self.ctx.scheduler.run_now(s, channel, user)
        return ToolResult(ok, message, {"verified": ok, "schedule_id": s.id})


def schedule_tools(ctx: ScheduleContext) -> list[Tool[Any]]:
    return [ScheduleTask(ctx), ListSchedules(ctx), PauseSchedule(ctx), ResumeSchedule(ctx), DeleteSchedule(ctx),
            RunScheduleNow(ctx)]
