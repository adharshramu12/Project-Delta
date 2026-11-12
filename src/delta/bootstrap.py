"""Composition root: builds every component from Settings and wires shutdown hooks.

Swap any piece (a fake router in tests, a different sensor) by constructing Container yourself.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from delta.memory.tools import memory_tools, wire_memory_tools
from delta.messaging.tools import ChatSessions, messaging_tools
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog, load_or_create_key
from delta.safety.policy import PolicyEngine
from delta.system import childenv, vault
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
from delta.tools.web_search import FetchPage, SearchRouter, WebAnswer, WebSearch


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


def arm_stop(c: Any) -> None:
    """A new task starts: clear an earlier stop, and make Esc twice / Ctrl+Alt+Shift+S stop this one."""
    from delta.system import input_stop

    input_stop.reset()
    signal = getattr(c, "signal", None)
    if signal is None:
        return
    loop = asyncio.get_running_loop()

    def from_hotkey(kind: str) -> None:  # called on the overlay thread
        from delta.control import stop_running

        loop.call_soon_threadsafe(lambda: stop_running(c, by=f"hotkey:{kind}"))

    try:
        signal.arm(from_hotkey)
    except Exception as e:  # the signal is a convenience: a task never fails because of it
        log.warning("signal.arm_failed", error=str(e))


# The top bar's warning on a PC with no AI model. The way most people fix it comes first (Settings > Keys, a free Groq
# key), then a local app; the terminal command last, for people who use one (fresh-user audit: it led with it).
NO_MODEL_WARNING = ("No AI model is available. Open Settings > Keys and add a free Groq key (console.groq.com/keys), "
                    "or start Ollama or LM Studio on this PC, or sign in to Claude Code or Codex to use your plan. "
                    "In a terminal: delta secrets set groq_api_key")


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
    connectors: Any = None  # delta.connectors.tools.AppTools: connected apps (its .manager connects them)
    logins: Any = None  # delta.logins.service.LoginService: saved site logins
    watchers: Any = None  # delta.watchers.service.WatchService: portals watched on a schedule
    signal: Any = None  # delta.system.agent_cursor.AgentCursor: "Delta is in control" ring and stop hotkeys
    active: dict[str, asyncio.Task[Any]] = field(default_factory=dict)  # every task asked for, waiting or running
    updates: Any = None  # delta.updates.service.UpdateService: newer-version check (its loop starts in `delta run`)

    @property
    def engine(self) -> Any:
        """delta.engine.recorder.TaskEngine: task states, the step log, resume. None when switched off."""
        return self.orchestrator.engine

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
        log.warning("delta.warning", message=message)

    async def _run_within_limit(self, req: TaskRequest) -> TaskResult:
        """A task never runs for ever: after agent.task_timeout_s it is stopped and the user is told where it was."""
        from delta.core.activity import LAST

        limit = getattr(getattr(getattr(self, "settings", None), "agent", None), "task_timeout_s", 0)
        if not limit:
            return await self.orchestrator.run(req)
        work = asyncio.ensure_future(self.orchestrator.run(req))
        try:
            done, _ = await asyncio.wait({work}, timeout=limit)
        except asyncio.CancelledError:  # Stop from the user: stop the work too, not only this wrapper
            work.cancel()
            with contextlib.suppress(BaseException):
                await work
            raise
        if work in done:
            return work.result()
        work.cancel()
        with contextlib.suppress(BaseException):
            await work
        where = LAST.get(req.id, "")
        minutes = f"{limit / 60:g} minutes" if limit >= 60 else f"{limit:g} seconds"
        answer = (f"I stopped this task after {minutes} because it was not finishing"
                  + (f". The last thing I was doing: {where}" if where else "")
                  + ". Nothing more runs for it. Try a smaller request, or ask me to carry on from there.")
        self.audit.record("task.timeout", task=req.id, after_s=limit, last=where)
        with contextlib.suppress(Exception):
            await self.events.publish("task.finished", task_id=req.id, status="failed", answer=answer)
        return TaskResult(req.id, "failed", answer)

    async def submit(self, req: TaskRequest, autonomy_cap: str | None = None) -> TaskResult:
        """One heavy task at a time keeps the laptop responsive. While remote control is locked, nothing
        asked from a remote channel (Telegram, Slack, any later one) runs at all.
        autonomy_cap (scheduled runs): this task never gets a higher permission level than this one."""
        from delta.safety.remote_guard import is_remote

        if self.guard is not None and self.guard.locked and is_remote(req.channel):
            self.audit.record("guard.blocked", task=req.id, channel=req.channel, user=req.user, why="locked")
            return TaskResult(req.id, "failed", self.guard.locked_message())
        if self._task_lock.locked():  # another task is running: say so instead of looking stuck
            with contextlib.suppress(Exception):
                await self.events.publish("task.queued", task_id=req.id, text=req.text)
        me = asyncio.current_task()
        active = getattr(self, "active", None)  # every task asked for, so "stop all" reaches waiting ones too
        if me is not None and active is not None:
            active[req.id] = me
        try:
            async with self._task_lock:
                self.running = me
                capped = self._cap_autonomy(autonomy_cap)
                try:
                    arm_stop(self)
                    return await Container._run_within_limit(self, req)
                finally:
                    self.running = None
                    if (signal := getattr(self, "signal", None)) is not None:
                        signal.stop()  # the ring never outlives its task (done, failed or stopped)
                    if capped is not None and self.orchestrator.executor.policy is capped:
                        self.orchestrator.executor.policy = make_policy(self.settings)  # today's rules again
        finally:
            if active is not None and active.get(req.id) is me:
                active.pop(req.id, None)

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
        try:
            folder = save_dir(self.settings.files)
        except OSError as e:  # a save folder on a drive that is gone (USB, network) must not stop start-up
            self.warn(f"Delta's save folder {self.settings.files.save_dir} is not available ({e.strerror or e}). "
                      "Plug the drive back in, or choose another folder in Settings.")
        else:
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
            if self.watchers is not None:
                self.watchers.store.close()

        self.lifecycle.on_shutdown("state", close_state)
        if self.engine is not None:  # tasks a crash or power cut cut off: shown with Resume / Discard
            if interrupted := self.engine.recover():
                self.warn(f"{len(interrupted)} task(s) were interrupted when Delta last stopped. Resume or discard "
                          "them under Tasks in the dashboard, with `delta tasks`, or from Telegram.")
            self.lifecycle.on_shutdown("engine", self._close_engine)
        for name, closer in self.closers:
            self.lifecycle.on_shutdown(name, closer)
        from delta.system import sqlite_safe

        for original, moved in sqlite_safe.SET_ASIDE:  # damaged databases: kept aside, a new one in their place
            if original.parent == self.settings.data_dir or self.settings.data_dir in original.parents:
                self.warn(sqlite_safe.warning_text(original, moved))
        if why := vault.problem():  # Windows without working DPAPI: chats and memories are refused, not stored plain
            self.warn(why.replace("encrypt this", "encrypt your chats and memories").replace(
                "so it was not saved", "so they are not saved until this is fixed"))
        if getattr(self.audit, "key", None) is None and vault.required():
            self.warn("The audit log is not signed: Windows Credential Manager did not give Delta its audit key. "
                      "It still keeps its hash chain. Run `delta doctor` for details.")
        self.audit.record("delta.start")

    async def _close_engine(self) -> None:
        if self.engine is not None:
            self.engine.store.close()

    def _check_models(self) -> None:
        """Tell new users up front when no AI model can be reached, instead of failing later."""
        cloud = [n for n, p in self.router.providers.items() if not p.local and p.supports("chat")]
        if cloud:
            return
        local = [p for p in self.router.providers.values() if p.local]
        if not any(callable(ping := getattr(p, "ping", None)) and ping() for p in local):
            self.warn(NO_MODEL_WARNING)

    async def _start_addons(self) -> None:
        from delta.addons import mcp_settings

        cfg = self.settings.addons
        try:  # Settings > Add-ons "mcp" moved to Apps (delta.addons.mcp_settings): once, nothing lost
            if moved := mcp_settings.apply_migration(self.settings):
                self.audit.record("settings.migrated", what="addons.mcp", change=moved)
        except Exception:
            log.exception("addons.mcp_migration_failed")
        enabled = mcp_settings.effective_addons(cfg)  # MCP servers run whenever some are configured
        if not enabled:
            return
        from delta.addons.api import AddonContext
        from delta.addons.builtin import builtin_addons
        from delta.addons.loader import start_addons

        def make_ctx(name: str, conf: dict[str, Any]) -> AddonContext:
            folder = self.settings.data_dir / "addon-data" / name
            folder.mkdir(parents=True, exist_ok=True)
            return AddonContext(name, conf, folder, self.router, self.events, self.registry)

        self.addon_infos, contexts = await start_addons(
            enabled, cfg.config, builtin_addons(), make_ctx, self.settings.data_dir / "addons")
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


def attach_engine(settings: Settings, router: ModelRouter, executor: ToolExecutor, orchestrator: Orchestrator,
                  proof: ProofRecorder | None, registry: ToolRegistry, audit: AuditLog) -> Any:
    """The durable task engine (delta.engine): tasks.db, the step log and the tool watchdog. Off in
    settings (engine.enabled: false), nothing is attached and tasks run exactly as before."""
    if not settings.engine.enabled:
        return None
    from delta.engine.recorder import TaskEngine
    from delta.engine.store import TaskStore
    from delta.engine.watchdog import Watchdog

    cfg = settings.engine
    try:
        store = TaskStore(settings.data_dir / "tasks.db", cfg.max_events_per_task)
    except Exception as e:  # a damaged tasks.db must never stop Delta from starting
        log.error("engine.unavailable", error=str(e))
        return None
    engine = TaskEngine(store, router, proof, registry, cfg.retention_days, cfg.max_mb)
    executor.recorder = engine
    executor.watchdog = Watchdog(cfg, audit)
    orchestrator.engine = engine
    return engine


def attach_selfheal(settings: Settings, router: ModelRouter, executor: ToolExecutor, orchestrator: Orchestrator,
                    events: EventBus) -> Any:
    """Self-heal (recovery playbooks, strategy ladder), undo for file changes, and the opt-in model ladder and
    failover. Each piece follows its own setting; the model ladder and fallbacks are empty (off) by default,
    so routing is unchanged unless the user fills them in."""
    from delta.llm.ladder import ModelLadder
    from delta.selfheal.healer import Healer
    from delta.selfheal.undo import UndoStore

    router.fallbacks = [n for n in settings.router.fallbacks if n in router.providers]
    ladder = ModelLadder(settings.router, router.providers)
    orchestrator.ladder = ladder if ladder.enabled else None
    healer = None
    if settings.selfheal.enabled:
        healer = Healer(settings.selfheal, executor, events, can_see=orchestrator._can_see)
        healer.engine = orchestrator.engine
        orchestrator.healer = healer
    if settings.undo.enabled:
        undo = UndoStore(settings.data_dir / "undo", settings.undo, lambda: list(executor.policy.allowed))
        try:
            undo.prune()
        except Exception as e:  # an unreadable undo folder must never stop Delta from starting
            log.warning("undo.prune_failed", error=str(e))
        executor.undo = undo
        if orchestrator.engine is not None:
            orchestrator.engine.undo = undo
    return healer


def attach_perception(settings: Settings, router: ModelRouter, executor: ToolExecutor, orchestrator: Orchestrator,
                      browsers: Any = None, ctl: Any = None, proof: ProofRecorder | None = None,
                      governor: Any = None) -> Any:
    """Set-of-marks, Windows OCR and the perception cache (delta.perception), and the opt-in parallel read-only
    helpers (delta.helpers). The perception tools hide themselves where they cannot work (no vision model, not
    Windows, no OCR language). Helper tools are registered only when helpers.enabled is on, so with the
    default settings the planner and the agent see exactly the tools they saw before."""
    from delta.perception.cache import PerceptionCache
    from delta.perception.ocr import WindowsOcr
    from delta.perception.tools import Perception, perception_tools

    cfg = settings.perception
    perception = Perception(cfg, router, WindowsOcr(cfg.ocr), PerceptionCache(cfg.cache), hub=browsers, ctl=ctl,
                            proof=proof, governor=governor)
    for t in perception_tools(perception):
        executor.registry.register(t)
    orchestrator.perception = perception
    healer = orchestrator.healer
    if healer is not None:
        names = set(executor.registry.names())
        healer.can_ocr = lambda: "click_text" in names and perception.ocr.available()
        healer.can_mark = lambda: cfg.marks and perception.can_see()
    if settings.helpers.enabled:
        from delta.helpers.supervisor import HelperSupervisor
        from delta.helpers.tools import helper_tools

        supervisor = HelperSupervisor(settings.helpers, router, executor, orchestrator.engine)
        for t in helper_tools(supervisor, settings.helpers.read_page):
            executor.registry.register(t)
        orchestrator.helpers = supervisor
    return perception


def attach_attachments(settings: Settings, router: ModelRouter, executor: ToolExecutor, orchestrator: Orchestrator,
                       ocr: Any = None) -> Any:
    """Files and pictures sent in the chat or on Telegram (delta.attachments): the store in the data folder, the
    two read tools (offered only with a request that has files) and, when the user named a provider for pictures
    only (attachments.vision + vision_model), a separate router for it, so the normal routes never change."""
    cfg = settings.attachments
    if not cfg.enabled:
        return None
    from delta.attachments.service import AttachmentService
    from delta.attachments.tools import attachment_tools

    service = AttachmentService(cfg, settings.data_dir, router, ocr, pictures_router(settings))
    try:
        service.prune()
    except Exception as e:  # an unreadable folder must never stop Delta from starting
        log.warning("attachments.prune_failed", error=str(e))
    for t in attachment_tools(service):
        executor.registry.register(t)
    orchestrator.attachments = service
    return service


def pictures_router(settings: Settings) -> ModelRouter | None:
    """attachments.vision names one of the user's API providers and vision_model a model on it: a router with only
    that provider, used for attached pictures only. Anything else (auto, off, a CLI provider) returns None and the
    pictures use the normal vision route."""
    cfg = settings.attachments
    name, model = (cfg.vision or "").strip(), (cfg.vision_model or "").strip()
    pcfg = settings.llm.providers.get(name)
    if not model or pcfg is None or pcfg.kind != "openai":
        return None
    only = settings.model_copy(deep=True)
    only.llm.providers = {name: pcfg.model_copy(update={"models": {"chat": model, "vision": model}})}
    return ModelRouter(build_providers(only), {"vision": [name]}, preferred=name)


def attach_learning(settings: Settings, orchestrator: Orchestrator, learning: LearningStore,
                    skills: SkillStore | None) -> None:
    """The promotion gate (delta.learning.gate): lesson candidates from self-heal and repaired recipe versions,
    used only after they prove themselves. Off in settings (learning.enabled: false), nothing is attached."""
    if not settings.learning.enabled:
        return
    from delta.learning.gate import PromotionGate
    from delta.learning.lessons import LessonBook

    gate = PromotionGate(settings.learning, learning, skills, orchestrator.engine)
    orchestrator.gate = gate
    if settings.learning.lessons:
        orchestrator.lessons = LessonBook(learning, gate)


def build(settings: Settings) -> Container:
    data = settings.data_dir
    data.mkdir(parents=True, exist_ok=True)  # first run: databases need the folder to exist
    solver: Any = None  # the captcha solver, shared by solve_captcha and sign-in
    events = EventBus()
    governor = Governor(settings.governor, watch_gpu=any(p.local for p in settings.llm.providers.values()))
    router = ModelRouter(build_providers(settings), settings.llm.routes, allow_local=governor.allow_local,
                         preferred=settings.llm.preferred, fallback=settings.llm.fallback)
    memory = MemoryStore.from_config(data / "memory.db", settings.memory)
    # Keyed chain (HMAC) when the credential store works; otherwise the plain hash chain as before.
    audit = AuditLog(data / "audit.jsonl", key_loader=load_or_create_key)
    approvals = ApprovalBroker(settings.agent.approval_timeout_s)
    childenv.configure(settings.agent.child_env_allow)  # run_shell / run_python never see secret env vars

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
        *app_tools(settings.apps, data / "cache"),  # installed-apps index cached in the data folder
        *media_tools(),
        *meeting_tools(),
        *messaging_tools(chats),
        AlertEmergencyContacts(emergency),
        WebSearch(search := SearchRouter.from_order(settings.search.order)),
        FetchPage(),
        WebAnswer(search, router),  # a current-facts question in one step: search, read, answer
        *(memory_tools(memory) if settings.memory.enabled else []),
    ])
    from delta.tools.email import email_tools, save_draft_tool

    draft_email, send_email = email_tools(router)  # every email is shown in full and sent only on a human yes
    registry.register(draft_email)
    registry.register(send_email)
    registry.register(save_draft_tool(send_email))  # keep it in Gmail Drafts, checked field by field, never sent
    send_email.registry = registry
    from delta.addons.writer import writer_tools

    writer = writer_tools(router, settings.writer, events)  # [] when writer.enabled is false
    for t in writer:
        registry.register(t)

    from delta.scheduler.setup import attach as attach_scheduler

    scheduler, sched_ctx = attach_scheduler(settings, registry, approvals)  # scheduled tasks (delta.scheduler)
    closers: list[tuple[str, Callable[[], Awaitable[None]]]] = []
    proof = ProofRecorder(data / "runs")
    desktop_ok = _installed("pywinauto", "pyautogui", "mss")
    signal: Any = None  # the "Delta is in control" ring and the stop hotkeys (desktop PCs only)
    if desktop_ok:
        from delta.system.agent_cursor import AgentCursor
        from delta.system.agent_cursor import install as install_signal
        from delta.tools.desktop import DesktopController, desktop_tools
        from delta.tools.screen import screen_tools

        AgentCursor.recover(data)  # an older version that died with the pointer orange gives it back
        cursor = AgentCursor(data, enabled=settings.browser.orange_cursor)  # ring + border + stop hotkeys
        install_signal(cursor)  # Delta's own browser and screenshots find it here
        signal = cursor
        ctl = DesktopController(cursor)
        for t in [*desktop_tools(ctl), *screen_tools(router, governor, proof)]:
            registry.register(t)

        async def close_desktop() -> None:
            cursor.close()
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
        from delta.tools.captcha import captcha_tool, make_solver

        solver = make_solver(settings, router, approvals, audit, proof)
        browsers.add_private([*browser_tools(browser),
                              captcha_tool(browser, settings, router, approvals, audit, proof, solver)], browser)
        closers.append(("browser", browser.close))
    for t in browsers.tools():
        registry.register(t)
    from delta.logins.setup import attach as attach_logins

    # Saved site logins (delta.logins): sign_in / list_logins, typed only on the exact saved site
    logins = attach_logins(settings, registry, browsers, audit, approvals, solver if browsers.mgr else None, proof,
                           live_channels)
    learning = LearningStore(data / "learning.db")

    async def on_learned(item: Learned) -> None:
        await events.publish("learning.added", item=item.to_dict())

    from delta.connectors.setup import attach as attach_connectors

    connectors = attach_connectors(settings, registry, audit)  # connected apps; nothing connects at start-up
    if connectors is not None:
        closers.append(("connectors", connectors.manager.close))
        from delta.core.activity import APP_NAMES

        APP_NAMES.update({a.id: a.name for a in connectors.manager.catalog.list()})  # "Google Drive" in progress lines
    send_email.connectors = connectors  # send through the Gmail app when it is connected and can send
    executor = ToolExecutor(registry, make_policy(settings), approvals, audit, learning, on_learned)
    if (opener := registry.get("open_app")) is not None:  # blocked apps: also what a loose match would open
        opener.denied_apps = lambda: list(executor.policy.b.denied_apps)  # type: ignore[attr-defined]
    # `remember` labels what a task that read outside content saves as untrusted (delta.memory.provenance)
    wire_memory_tools([found for n in registry.names() if (found := registry.get(n)) is not None], executor)
    sched_ctx.executor = executor
    chats.executor = executor  # each delegated reply goes through the same approval rules
    for t in writer:
        if hasattr(t, "executor"):
            t.executor = executor  # documents go back to Telegram/Slack through send_file, with every check
    skills = SkillStore(data / "skills.db")
    task_lock = asyncio.Semaphore(1)  # one heavy task at a time (Container.submit), shared with watcher runs
    from delta.watchers.setup import attach as attach_watchers

    watchers = attach_watchers(settings, registry, logins, browsers, scheduler, skills, audit, task_lock)
    orchestrator = Orchestrator(settings, router, executor, memory if settings.memory.enabled else None,
                                governor, events, audit, skills, learning, proof)
    orchestrator.browsers = browsers  # pins each task to its browser (switches apply at the next task)
    from delta.logins.setup import attach_flow

    attach_flow(orchestrator, logins, executor, events)  # "log in to my ERP": no AI calls (delta.logins.flow)
    orchestrator.connectors = connectors

    async def on_health(a: Assessment) -> None:
        await events.publish("governor.health", health=a.health.name, reasons=a.reasons)

    governor.on_change(on_health)
    attach_engine(settings, router, executor, orchestrator, proof, registry, audit)  # tasks.db, watchdog
    attach_selfheal(settings, router, executor, orchestrator, events)  # recovery, undo, opt-in model ladder
    attach_perception(settings, router, executor, orchestrator, browsers, ctl if desktop_ok else None, proof,
                      governor)  # set-of-marks, OCR, perception cache, opt-in read-only helpers
    perception = orchestrator.perception
    attach_attachments(settings, router, executor, orchestrator, perception.ocr if perception is not None else None)
    attach_learning(settings, orchestrator, learning, skills)  # promotion gate, lessons, recipe repair
    from delta.safety.remote_guard import RemoteGuard
    from delta.system.notify import notify

    guard = RemoteGuard(data, audit, alert=notify)
    logins.guard = guard
    watchers.guard = guard
    send_file.guard = guard
    executor.guard = guard  # Remote Guard for every tool call (PIN for sensitive reads, the lock)
    approvals.guard = guard  # ...and for every approval button pressed on a phone
    container = Container(settings, events, governor, router, memory, audit, approvals, registry, orchestrator,
                          Lifecycle(data), task_lock, closers, chats=ChatStore(data / "chats.db"),
                          live_channels=live_channels, guard=guard, emergency=emergency, scheduler=scheduler,
                          browsers=browsers, connectors=connectors, logins=logins, watchers=watchers,
                          signal=signal)
    from delta.updates.service import UpdateService

    container.updates = UpdateService(settings)  # only looks; nothing is fetched until `delta run` starts it
    scheduler.bind(container)
    return container
