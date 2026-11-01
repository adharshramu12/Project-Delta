"""Composition root: builds every component from Settings and wires shutdown hooks.

Swap any piece (a fake router in tests, a different sensor) by constructing Container yourself.
"""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from delta.config import Settings, ensure_desktop_shortcut, save_dir
from delta.core.events import EventBus
from delta.core.orchestrator import Orchestrator
from delta.core.state import TaskRequest, TaskResult
from delta.llm.providers import OpenAICompatProvider, Provider
from delta.llm.router import ModelRouter
from delta.memory.skills import SkillStore
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


def _installed(*modules: str) -> bool:
    """Optional extras are detected without importing them (keeps startup light)."""
    return all(importlib.util.find_spec(m) is not None for m in modules)


def build_providers(settings: Settings) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    for name, cfg in settings.llm.providers.items():
        # Keys are looked up on first use (not at startup); providers without one are skipped
        # by the router and reported by `delta doctor`.
        key = (lambda s=cfg.secret: get_secret(s)) if cfg.secret else None
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
    closers: list[tuple[str, Callable[[], Awaitable[None]]]] = field(default_factory=list)
    addon_infos: list[Any] = field(default_factory=list)

    async def submit(self, req: TaskRequest) -> TaskResult:
        """One heavy task at a time keeps the laptop responsive."""
        async with self._task_lock:
            return await self.orchestrator.run(req)

    async def start(self) -> None:
        self.lifecycle.acquire_single_instance()
        folder = save_dir(self.settings.files)
        await asyncio.to_thread(ensure_desktop_shortcut, folder)
        await self.governor.start()
        self.lifecycle.on_shutdown("governor", self.governor.stop)
        await self._start_addons()
        self.lifecycle.on_shutdown("models", self.router.unload_local)

        async def close_state() -> None:
            self.approvals.deny_all()
            await self.orchestrator.drain()  # let in-flight learning finish before closing stores
            self.memory.close()
            if self.orchestrator.skills:
                self.orchestrator.skills.close()

        self.lifecycle.on_shutdown("state", close_state)
        for name, closer in self.closers:
            self.lifecycle.on_shutdown(name, closer)
        self.audit.record("delta.start")

    async def _start_addons(self) -> None:
        cfg = self.settings.addons
        if not cfg.enabled:
            return
        from delta.addons.api import AddonContext
        from delta.addons.builtin import builtin_addons
        from delta.addons.loader import start_addons

        def make_ctx(name: str, conf: dict[str, Any]) -> AddonContext:
            folder = self.settings.data_dir / "addon-data" / name
            folder.mkdir(parents=True, exist_ok=True)
            return AddonContext(name, conf, folder, self.router, self.events, self.registry)

        self.addon_infos, contexts = await start_addons(
            cfg.enabled, cfg.config, builtin_addons(), make_ctx, self.settings.data_dir / "addons")
        for ctx in contexts:
            for name, hook in ctx._shutdown:
                self.lifecycle.on_shutdown(name, hook)
        self.audit.record("addons", running=[i.name for i in self.addon_infos if i.status == "running"])

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
    closers: list[tuple[str, Callable[[], Awaitable[None]]]] = []
    if _installed("pywinauto", "pyautogui", "mss"):
        from delta.tools.desktop import DesktopController, desktop_tools
        from delta.tools.screen import screen_tools

        ctl = DesktopController()
        for t in [*desktop_tools(ctl), *screen_tools(router, governor)]:
            registry.register(t)

        async def close_desktop() -> None:
            ctl.close()

        closers.append(("desktop", close_desktop))
    if _installed("playwright"):
        from delta.tools.browser import BrowserManager, browser_tools

        browser = BrowserManager(data)
        for t in browser_tools(browser):
            registry.register(t)
        closers.append(("browser", browser.close))
    # Delta's own save folder always counts as an allowed folder
    boundaries = settings.boundaries.model_copy(deep=True)
    boundaries.allowed_paths.append(str(save_dir(settings.files, create=False)))
    executor = ToolExecutor(registry, PolicyEngine(boundaries), approvals, audit)
    skills = SkillStore(data / "skills.db")
    orchestrator = Orchestrator(settings, router, executor, memory if settings.memory.enabled else None,
                                governor, events, audit, skills)

    async def on_health(a: Assessment) -> None:
        await events.publish("governor.health", health=a.health.name, reasons=a.reasons)

    governor.on_change(on_health)
    return Container(settings, events, governor, router, memory, audit, approvals, registry, orchestrator,
                     Lifecycle(data), asyncio.Semaphore(1), closers)
