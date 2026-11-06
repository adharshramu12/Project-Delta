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
from delta.core.proof import ProofRecorder
from delta.core.state import TaskRequest, TaskResult
from delta.learning.store import Learned, LearningStore
from delta.llm.cli_providers import CLI_KINDS
from delta.llm.providers import OpenAICompatProvider, Provider, lmstudio_base_url
from delta.llm.router import ModelRouter
from delta.memory.chats import ChatStore
from delta.memory.skills import SkillStore
from delta.memory.store import MemoryStore
from delta.memory.tools import memory_tools
from delta.messaging.tools import ChatSessions, messaging_tools
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine
from delta.system.governor import Governor
from delta.system.hardware_guard import Assessment
from delta.system.lifecycle import Lifecycle
from delta.system.logging import log
from delta.system.secrets import get_secret
from delta.tools.apps import app_tools
from delta.tools.deliver import SendFile
from delta.tools.emergency import AlertEmergencyContacts, EmergencyContacts
from delta.tools.filesystem import filesystem_tools
from delta.tools.media import media_tools
from delta.tools.meetings import meeting_tools
from delta.tools.pc import pc_tools
from delta.tools.power import power_tools
from delta.tools.registry import ToolExecutor, ToolRegistry
from delta.tools.shell import RunShell
from delta.tools.web_search import FetchPage, SearchRouter, WebSearch


def _installed(*modules: str) -> bool:
    """Optional extras are detected without importing them (keeps startup light)."""
    return all(importlib.util.find_spec(m) is not None for m in modules)


def make_policy(settings: Settings) -> PolicyEngine:
    """Policy from the current settings. Rebuilt when the user changes settings, so edits apply
    to the very next action. Delta's data dir (access token, audit log, settings) is protected and
    its config dir read-only: no setting or tool can let Delta rewrite its own rules."""
    boundaries = settings.boundaries.model_copy(deep=True)
    boundaries.allowed_paths.append(str(save_dir(settings.files, create=False)))  # always allowed
    if settings.writer.enabled:  # where make_document saves (Documents\\Delta by default)
        from delta.addons.writer.document import documents_dir

        boundaries.allowed_paths.append(str(documents_dir(settings.writer.output_dir, create=False)))
    return PolicyEngine(boundaries, protected=[settings.data_dir], read_only=[settings.config_dir])


def build_providers(settings: Settings) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    for name, cfg in settings.llm.providers.items():
        if cfg.kind in CLI_KINDS:
            providers[name] = CLI_KINDS[cfg.kind](name, cfg)
            continue
        if cfg.discover == "lmstudio":
            cfg = cfg.model_copy(update={"base_url": lmstudio_base_url(cfg.base_url)})
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
    warnings: list[str] = field(default_factory=list)  # setup problems shown to the user, not just logged
    chats: ChatStore | None = None  # separate conversations (dashboard sidebar)
    live_channels: dict[str, Any] = field(default_factory=dict)  # running chat channels, by name
    guard: Any = None  # RemoteGuard: PIN and lock for remote control
    emergency: Any = None  # EmergencyContacts (encrypted)
    running: asyncio.Task[Any] | None = None  # the task being worked on now (any channel can stop it)
    scheduler: Any = None  # delta.scheduler.service.Scheduler (its loop starts in `delta run`)
    browsers: Any = None  # delta.tools.browser_switch.BrowserHub: which browser each task uses

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
        log.warning("delta.warning", message=message)

    async def submit(self, req: TaskRequest, autonomy_cap: str | None = None) -> TaskResult:
        """One heavy task at a time keeps the laptop responsive. While remote control is locked, nothing
        asked from a remote channel (Telegram, Slack, any later one) runs at all.
        autonomy_cap (scheduled runs): this task never gets a higher permission level than this one."""
        from delta.safety.remote_guard import is_remote

        if self.guard is not None and self.guard.locked and is_remote(req.channel):
            self.audit.record("guard.blocked", task=req.id, channel=req.channel, user=req.user, why="locked")
            return TaskResult(req.id, "failed", self.guard.locked_message())
        async with self._task_lock:
            self.running = asyncio.current_task()
            capped = self._cap_autonomy(autonomy_cap)
            try:
                return await self.orchestrator.run(req)
            finally:
                self.running = None
                if capped is not None and self.orchestrator.executor.policy is capped:
                    self.orchestrator.executor.policy = make_policy(self.settings)  # today's rules again

    def _cap_autonomy(self, cap: str | None) -> Any:
        """Lower the permission level for the task about to run (tasks run one at a time); never raises it."""
        if not cap:
            return None  # every ordinary request: nothing changes
        from delta.scheduler.service import lower_autonomy

        executor = self.orchestrator.executor
        now = str(executor.policy.b.autonomy)
        lower = lower_autonomy(cap, now)
        if lower == now:
            return None
        settings = self.settings.model_copy(deep=True)
        settings.boundaries.autonomy = lower  # type: ignore[assignment]
        executor.policy = make_policy(settings)
        return executor.policy

    async def start(self) -> None:
        self.lifecycle.acquire_single_instance()
        folder = save_dir(self.settings.files)
        await asyncio.to_thread(ensure_desktop_shortcut, folder)
        await self.governor.start()
        self.lifecycle.on_shutdown("governor", self.governor.stop)
        await self._start_addons()
        await asyncio.to_thread(self._check_models)
        self.lifecycle.on_shutdown("models", self.router.unload_local)

        async def close_state() -> None:
            self.approvals.deny_all()
            await self.orchestrator.drain()  # let in-flight learning finish before closing stores
            self.memory.close()
            if self.chats:
                self.chats.close()
            if self.orchestrator.skills:
                self.orchestrator.skills.close()
            if self.orchestrator.learning:
                self.orchestrator.learning.close()
            if self.scheduler is not None:
                self.scheduler.store.close()

        self.lifecycle.on_shutdown("state", close_state)
        for name, closer in self.closers:
            self.lifecycle.on_shutdown(name, closer)
        self.audit.record("delta.start")

    def _check_models(self) -> None:
        """Tell new users up front when no AI model can be reached, instead of failing later."""
        cloud = [n for n, p in self.router.providers.items() if not p.local and p.supports("chat")]
        if cloud:
            return
        local = [p for p in self.router.providers.values() if p.local]
        if not any(callable(ping := getattr(p, "ping", None)) and ping() for p in local):
            self.warn("No AI model is available. Add a free Groq key (delta secrets set groq_api_key), "
                      "sign in to Claude Code or Codex to use your existing plan, or start Ollama / "
                      "LM Studio to run models on this PC.")

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
        for info in self.addon_infos:
            if info.status != "running":
                self.warn(f"Add-on '{info.name}' {info.status}: {info.error}")
        self.audit.record("addons", running=[i.name for i in self.addon_infos if i.status == "running"])

    async def stop(self) -> None:
        self.audit.record("delta.stop")
        await self.lifecycle.shutdown()


def build(settings: Settings) -> Container:
    data = settings.data_dir
    data.mkdir(parents=True, exist_ok=True)  # first run: databases need the folder to exist
    events = EventBus()
    governor = Governor(settings.governor, watch_gpu=any(p.local for p in settings.llm.providers.values()))
    router = ModelRouter(build_providers(settings), settings.llm.routes, allow_local=governor.allow_local,
                         preferred=settings.llm.preferred)
    memory = MemoryStore(data / "memory.db")
    audit = AuditLog(data / "audit.jsonl")
    approvals = ApprovalBroker(settings.agent.approval_timeout_s)

    chats = ChatSessions(router, events, audit)
    live_channels: dict[str, Any] = {}
    send_file = SendFile(live_channels)
    emergency = EmergencyContacts(data)
    registry = ToolRegistry([
        *filesystem_tools(),
        send_file,
        *power_tools(),
        *pc_tools(send_file),
        RunShell(),
        *app_tools(settings.apps),
        *media_tools(),
        *meeting_tools(),
        *messaging_tools(chats),
        AlertEmergencyContacts(emergency),
        WebSearch(SearchRouter.from_order(settings.search.order)),
        FetchPage(),
        *(memory_tools(memory) if settings.memory.enabled else []),
    ])
    from delta.addons.writer import writer_tools

    writer = writer_tools(router, settings.writer, events)  # [] when writer.enabled is false
    for t in writer:
        registry.register(t)

    from delta.scheduler.setup import attach as attach_scheduler

    scheduler, sched_ctx = attach_scheduler(settings, registry, approvals)  # scheduled tasks (delta.scheduler)
    closers: list[tuple[str, Callable[[], Awaitable[None]]]] = []
    proof = ProofRecorder(data / "runs")
    desktop_ok = _installed("pywinauto", "pyautogui", "mss")
    if desktop_ok:
        from delta.system.agent_cursor import AgentCursor
        from delta.tools.desktop import DesktopController, desktop_tools
        from delta.tools.screen import screen_tools

        AgentCursor.recover(data)  # a previous run that died while orange gives the cursor back
        cursor = AgentCursor(data, enabled=settings.browser.orange_cursor)
        ctl = DesktopController(cursor)
        for t in [*desktop_tools(ctl), *screen_tools(router, governor, proof)]:
            registry.register(t)

        async def close_desktop() -> None:
            cursor.restore()
            ctl.close()

        closers.append(("desktop", close_desktop))
    # Both browser kits are built when this PC can run them; which one a task uses is chosen per task
    # (delta.tools.browser_switch), so switching from the dashboard, Telegram or chat needs no restart.
    from delta.tools.browser_switch import BrowserHub

    browsers = BrowserHub(settings, audit)
    if desktop_ok:
        # The user's own signed-in Chrome/Edge, driven like a person (see user_browser.py). The default.
        from delta.tools.user_browser import user_browser_tools

        browsers.add_user(user_browser_tools(ctl, settings.browser.prefer))
    if _installed("playwright"):
        # Delta's private profile, fully automatable in the background but signed out until the user signs in.
        from delta.tools.browser import BrowserManager, browser_tools

        browser = BrowserManager(data)
        browser.diff_snapshots = settings.browser.snapshot_diffs
        browser.full_every = settings.browser.full_snapshot_every
        from delta.tools.captcha import captcha_tool

        browsers.add_private([*browser_tools(browser),
                              captcha_tool(browser, settings, router, approvals, audit, proof)], browser)
        closers.append(("browser", browser.close))
    for t in browsers.tools():
        registry.register(t)
    learning = LearningStore(data / "learning.db")

    async def on_learned(item: Learned) -> None:
        await events.publish("learning.added", item=item.to_dict())

    executor = ToolExecutor(registry, make_policy(settings), approvals, audit, learning, on_learned)
    sched_ctx.executor = executor
    chats.executor = executor  # each delegated reply goes through the same approval rules
    for t in writer:
        if hasattr(t, "executor"):
            t.executor = executor  # documents go back to Telegram/Slack through send_file, with every check
    skills = SkillStore(data / "skills.db")
    orchestrator = Orchestrator(settings, router, executor, memory if settings.memory.enabled else None,
                                governor, events, audit, skills, learning, proof)
    orchestrator.browsers = browsers  # pins each task to its browser (switches apply at the next task)

    async def on_health(a: Assessment) -> None:
        await events.publish("governor.health", health=a.health.name, reasons=a.reasons)

    governor.on_change(on_health)
    from delta.safety.remote_guard import RemoteGuard
    from delta.system.notify import notify

    guard = RemoteGuard(data, audit, alert=notify)
    send_file.guard = guard
    executor.guard = guard  # Remote Guard for every tool call (PIN for sensitive reads, the lock)
    approvals.guard = guard  # ...and for every approval button pressed on a phone
    container = Container(settings, events, governor, router, memory, audit, approvals, registry, orchestrator,
                          Lifecycle(data), asyncio.Semaphore(1), closers, chats=ChatStore(data / "chats.db"),
                          live_channels=live_channels, guard=guard, emergency=emergency, scheduler=scheduler,
                          browsers=browsers)
    scheduler.bind(container)
    return container
