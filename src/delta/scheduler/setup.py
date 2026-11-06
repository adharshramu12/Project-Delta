"""Wiring for bootstrap: one call builds the store, the scheduler and its tools (kept here so the
composition root only gains a couple of lines)."""

from __future__ import annotations

from typing import Any

from delta.config import Settings
from delta.scheduler.service import Scheduler
from delta.scheduler.store import ScheduleStore
from delta.scheduler.tools import ScheduleContext, schedule_tools


def attach(settings: Settings, registry: Any, approvals: Any) -> tuple[Scheduler, ScheduleContext]:
    """Register the schedule tools. The caller sets ctx.executor once it exists and binds the container."""
    from delta.system.notify import notify

    cfg = settings.scheduler
    store = ScheduleStore(settings.data_dir / "schedules.db", cfg.history_limit)
    scheduler = Scheduler(store, grace_minutes=cfg.grace_minutes, max_concurrent=cfg.max_concurrent,
                          enabled=cfg.enabled, alert=notify)
    ctx = ScheduleContext(store, scheduler, approvals, timezone=cfg.timezone)
    for tool in schedule_tools(ctx):
        registry.register(tool)
    return scheduler, ctx
