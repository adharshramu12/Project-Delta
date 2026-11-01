"""Composition root: builds every component from Settings and wires shutdown hooks.

Swap any piece (a fake router in tests, a different sensor) by constructing Container yourself.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from delta.config import Settings
from delta.core.events import EventBus
from delta.core.orchestrator import Orchestrator
from delta.core.state import TaskRequest, TaskResult
from delta.llm.providers import OpenAICompatProvider, Provider
from delta.llm.router import ModelRouter
from delta.memory.store import MemoryStore
from delta.memory.tools import memory_tools
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine
from delta.system.governor import Governor
from delta.system.hardware_guard import Assessment
from delta.system.lifecycle import Lifecycle
from delta.system.secrets import get_secret
from delta.tools.apps import app_tools
from delta.tools.filesystem import filesystem_tools
from delta.tools.registry import ToolExecutor, ToolRegistry
from delta.tools.shell import RunShell
from delta.tools.web_search import FetchPage, SearchRouter, WebSearch


def build_providers(settings: Settings) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    for name, cfg in settings.llm.providers.items():
        key = get_secret(cfg.secret) if cfg.secret else None
        if cfg.secret and not key:
            continue  # cloud provider without a key: skip silently, `delta doctor` reports it
        providers[name] = OpenAICompatProvider(name, cfg, key)
    return providers


@dataclass
class Container:
    settings: Settings
    events: EventBus
    governor: Governor
    router: ModelRouter
    memory: MemoryStore
    audit: AuditLog
    approvals: ApprovalBroker
    registry: ToolRegistry
    orchestrator: Orchestrator
    lifecycle: Lifecycle
    _task_lock: asyncio.Semaphore

    async def submit(self, req: TaskRequest) -> TaskResult:
        """One heavy task at a time keeps the laptop responsive."""
        async with self._task_lock:
            return await self.orchestrator.run(req)

    async def start(self) -> None:
        self.lifecycle.acquire_single_instance()
        await self.governor.start()
        self.lifecycle.on_shutdown("governor", self.governor.stop)
        self.lifecycle.on_shutdown("models", self.router.unload_local)

        async def close_state() -> None:
            self.approvals.deny_all()
            self.memory.close()

        self.lifecycle.on_shutdown("state", close_state)
        self.audit.record("delta.start")

    async def stop(self) -> None:
        self.audit.record("delta.stop")
        await self.lifecycle.shutdown()


def build(settings: Settings) -> Container:
    data = settings.data_dir
    events = EventBus()
    governor = Governor(settings.governor)
    router = ModelRouter(build_providers(settings), settings.llm.routes, allow_local=governor.allow_local)
    memory = MemoryStore(data / "memory.db")
    audit = AuditLog(data / "audit.jsonl")
    approvals = ApprovalBroker(settings.agent.approval_timeout_s)

    registry = ToolRegistry([
        *filesystem_tools(),
        RunShell(),
        *app_tools(settings.apps),
        WebSearch(SearchRouter.from_order(settings.search.order)),
        FetchPage(),
        *(memory_tools(memory) if settings.memory.enabled else []),
    ])
    executor = ToolExecutor(registry, PolicyEngine(settings.boundaries), approvals, audit)
    orchestrator = Orchestrator(settings, router, executor, memory if settings.memory.enabled else None,
                                governor, events, audit)

    async def on_health(a: Assessment) -> None:
        await events.publish("governor.health", health=a.health.name, reasons=a.reasons)

    governor.on_change(on_health)
    return Container(settings, events, governor, router, memory, audit, approvals, registry, orchestrator,
                     Lifecycle(data), asyncio.Semaphore(1))
