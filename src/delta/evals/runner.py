"""Run eval tasks through Delta's real agent loop and score them with deterministic checks.

Each task gets its own sandbox folder and data folder (never the user's real memory, skills or audit
log), the local fixture site for web tasks, and Delta's real tools, policy, approvals and task engine.
The model is either the scripted one (CI: no keys, no network, no cost) or a real provider the user
picked with --model (manual runs; they cost what that provider costs).

Results: a JSON file and a Markdown report with success rate, tokens, time, a cost estimate and one row
per task.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import shutil
import socket
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from delta.config import (
    REPO_ROOT,
    AgentConfig,
    AppEntry,
    Boundaries,
    EngineConfig,
    GroundingConfig,
    LearningConfig,
    MemoryConfig,
    SelfHealConfig,
    Settings,
    WriterConfig,
)
from delta.evals.checkers import CheckContext, run_checks
from delta.evals.fixtures import FixtureSite
from delta.evals.scripted import ScriptedProvider
from delta.evals.spec import EvalTask, fill

SPECS_DIR = REPO_ROOT / "evals" / "tasks"
SITE_DIR = REPO_ROOT / "evals" / "fixtures" / "site"
# Rough USD per million tokens (input, output) for a cost estimate only. Local models and the user's own
# plans (Claude Code, Codex, Cline) are not billed per token here.
PRICES: dict[str, tuple[float, float]] = {
    "groq": (0.15, 0.60), "deepseek": (0.27, 1.10), "nim": (0.0, 0.0), "ollama": (0.0, 0.0),
    "lmstudio": (0.0, 0.0), "scripted": (0.0, 0.0),
}
PLAN_PROVIDERS = {"claude_code", "codex", "cline"}
MARKER_SCRIPT = ("import pathlib\n"
                 "pathlib.Path(__file__).with_name('opened.txt').write_text('opened', encoding='utf-8')\n")


@dataclass
class TaskOutcome:
    id: str
    suite: str
    status: str = "not run"
    passed: bool = False
    skipped: str = ""
    seconds: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0
    cost_usd: float | None = 0.0
    provider: str = ""
    steps: list[str] = field(default_factory=list)
    step_details: list[dict[str, Any]] = field(default_factory=list)  # tool, ok, the start of its output
    checks: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    error: str = ""


def browser_available() -> bool:
    try:
        from delta.tools.browser import find_browser
    except Exception:
        return False
    return find_browser() is not None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def cost_of(provider: str, tokens_in: int, tokens_out: int) -> float | None:
    if provider in PLAN_PROVIDERS:
        return None  # paid by the user's plan, not per token
    p_in, p_out = PRICES.get(provider, (0.0, 0.0))
    return round((tokens_in * p_in + tokens_out * p_out) / 1_000_000, 6)


def skip_reason(task: EvalTask, has_browser: bool, scripted: bool, has_ocr: bool = True) -> str:
    if scripted and not task.script.get("plan"):
        return "no script for the scripted model"
    if "windows" in task.needs and sys.platform != "win32":
        return "needs Windows"
    if "browser" in task.needs and not has_browser:
        return "needs Chrome or Edge"
    if "ocr" in task.needs and not has_ocr:
        return "needs Windows OCR (not available on this PC)"
    return ""


async def ocr_available() -> bool:
    from delta.perception.ocr import WindowsOcr

    try:
        return await WindowsOcr("auto").probe()
    except Exception:
        return False


class _Router:
    """Builds the model router for a task: scripted, or the user's own providers."""

    def __init__(self, model: str | None) -> None:
        self.model = model

    def make(self, task: EvalTask, values: dict[str, str]) -> Any:
        from delta.llm.router import ModelRouter

        if self.model is None:
            provider = ScriptedProvider(fill(task.script, values))
            return ModelRouter({"scripted": provider}, {j: ["scripted"] for j in ("plan", "chat", "verify",
                                                                                 "vision", "helper")})
        from delta.bootstrap import build_providers
        from delta.config import load_settings

        user = load_settings()
        preferred = self.model if self.model in user.llm.providers else user.llm.preferred
        return ModelRouter(build_providers(user), user.llm.routes, preferred=preferred)


def _perception(task: EvalTask, router: Any, fake_mgr: Any) -> Any:
    from delta.config import PerceptionConfig
    from delta.evals.perception_fixtures import page_capture, text_capture
    from delta.perception.cache import PerceptionCache
    from delta.perception.marks import Capture
    from delta.perception.ocr import WindowsOcr
    from delta.perception.tools import Perception

    spec = dict(task.perception or {})
    cfg = PerceptionConfig(**{k: v for k, v in spec.items() if k in PerceptionConfig.model_fields})
    p = Perception(cfg, router, WindowsOcr(cfg.ocr), PerceptionCache(cfg.cache))
    fixture = str(spec.get("fixture", "page"))
    lines = [str(x) for x in spec.get("text") or []]

    async def capture(where: str, window: str | None) -> Capture:
        if fixture == "text":
            return text_capture(lines)
        if fake_mgr is None or not fake_mgr.current_url:
            raise LookupError("no page is open")
        return page_capture(fake_mgr)

    p.capture_override = capture
    return p


async def run_task(task: EvalTask, root: Path, site: FixtureSite, router_factory: _Router,
                   has_browser: bool) -> TaskOutcome:
    from delta.core.events import EventBus
    from delta.core.orchestrator import Orchestrator
    from delta.core.proof import ProofRecorder
    from delta.core.state import TaskRequest
    from delta.engine.recorder import TaskEngine
    from delta.engine.store import TaskStore
    from delta.engine.watchdog import Watchdog
    from delta.memory.store import MemoryStore
    from delta.memory.tools import memory_tools
    from delta.safety.approvals import ApprovalBroker
    from delta.safety.audit import AuditLog
    from delta.safety.policy import PolicyEngine
    from delta.selfheal.healer import Healer
    from delta.selfheal.inject import FaultInjector
    from delta.selfheal.undo import UndoStore
    from delta.tools.apps import app_tools
    from delta.tools.filesystem import filesystem_tools
    from delta.tools.power import power_tools
    from delta.tools.registry import ToolExecutor, ToolRegistry
    from delta.tools.shell import RunShell
    from delta.tools.web_search import FetchPage

    out = TaskOutcome(task.id, task.suite)
    sandbox = root / task.id
    data = root / f"{task.id}-data"
    sandbox.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    marker_script = sandbox / "marker_app.py"  # a tiny "app" for open_app: writes opened.txt next to itself
    marker_script.write_text(MARKER_SCRIPT, encoding="utf-8")
    values = {"sandbox": sandbox.as_posix(), "site": site.url, "python": Path(sys.executable).as_posix(),
              "marker": f"deltaeval{uuid.uuid4().hex[:8]}", "marker_app": marker_script.as_posix()}
    site.reset()
    setup = fill(task.setup, values)
    for name, content in (setup.get("files") or {}).items():
        path = sandbox / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")

    apps = {"evalmarker": AppEntry(command=Path(sys.executable).name, aliases=["eval marker"])}
    lcfg = {k: v for k, v in (task.learning or {}).items() if k in LearningConfig.model_fields}
    gcfg = {k: v for k, v in (task.grounding or {}).items() if k in GroundingConfig.model_fields}
    settings = Settings(agent=AgentConfig(max_steps=12, max_attempts=2, approval_timeout_s=30),
                        selfheal=SelfHealConfig(max_backoff_s=0.2), learning=LearningConfig(**lcfg),
                        grounding=GroundingConfig(**gcfg),
                        memory=MemoryConfig(auto_extract=True, learn_skills=False), data_dir=data, apps=apps,
                        boundaries=Boundaries(allowed_paths=[str(sandbox)]),
                        writer=WriterConfig(output_dir=str(sandbox), latex="off"))
    router = router_factory.make(task, values)
    events = EventBus()
    memory = MemoryStore(data / "memory.db")
    for fact in setup.get("memory") or []:
        if isinstance(fact, dict):  # with provenance: {content, kind, origin, trust, task_id, detail}
            memory.add(str(fact.get("content", "")), str(fact.get("kind", "profile")), str(fact.get("source", "user")),
                       origin=str(fact.get("origin", "")), trust=str(fact.get("trust", "")),
                       task_id=str(fact.get("task_id", "")), detail=str(fact.get("detail", "")))
        else:
            memory.add(str(fact), "profile", "user")
    audit = AuditLog(data / "audit.jsonl")
    approvals = ApprovalBroker(timeout_s=30)
    decision = task.approve == "approve"

    async def present(req: Any) -> None:
        asyncio.get_running_loop().call_soon(approvals.resolve, req.id, decision, "eval")

    approvals.add_presenter("eval", present)
    tools = [*filesystem_tools(), *power_tools(), *app_tools(settings.apps), RunShell(), FetchPage(),
             *memory_tools(memory)]
    from delta.addons.writer import writer_tools

    writer = writer_tools(router, settings.writer, events)
    tools += [t for t in writer if t.name == "make_document"]
    browser = None
    fake_mgr: Any = None
    if task.fake_browser is not None:  # evals only: the fake browser with failure switches
        from delta.evals.fakebrowser import FakeBrowserManager, fake_browser_tools

        fake_mgr = FakeBrowserManager(sandbox / "page_state.json", task.fake_browser)
        tools += fake_browser_tools(fake_mgr)
    perception = None
    if task.perception is not None:  # set-of-marks and OCR on a picture drawn for the eval
        perception = _perception(task, router, fake_mgr)
        from delta.perception.tools import perception_tools

        tools += perception_tools(perception)
    elif "browser" in task.needs and has_browser:
        from delta.tools import browser as browser_mod

        browser_mod.PORT = _free_port()  # a private port: never attaches to a Delta browser already running
        browser = browser_mod.BrowserManager(data)
        tools += browser_mod.browser_tools(browser)
    registry = ToolRegistry(tools)
    learning_store, skill_store = _learning_stores(task, data, values)
    executor = ToolExecutor(registry, PolicyEngine(settings.boundaries, protected=[data]), approvals, audit)
    from delta.memory.tools import wire_memory_tools

    wire_memory_tools(tools, executor)
    for t in writer:
        if hasattr(t, "executor"):
            t.executor = executor
    proof = ProofRecorder(data / "runs")
    store = TaskStore(data / "tasks.db")
    engine = TaskEngine(store, router, proof, registry)
    executor.recorder = engine
    executor.watchdog = Watchdog(EngineConfig(), audit)
    undo = UndoStore(data / "undo", settings.undo, [sandbox])
    executor.undo = engine.undo = undo
    if task.inject:
        executor.faults = FaultInjector(fill(task.inject, values))
    orch = Orchestrator(settings, router, executor, memory, None, events, audit, skill_store, learning_store, proof)
    orch.engine = engine
    gate = None
    if learning_store is not None and settings.learning.enabled:  # the promotion gate, lessons, recipe repair
        from delta.learning.gate import PromotionGate
        from delta.learning.lessons import LessonBook

        gate = PromotionGate(settings.learning, learning_store, skill_store, engine)
        orch.gate = gate
        orch.lessons = LessonBook(learning_store, gate) if settings.learning.lessons else None
    orch.perception = perception
    if task.helpers is not None:  # parallel read-only helpers, on for this eval
        from delta.config import HelpersConfig
        from delta.helpers.supervisor import HelperSupervisor
        from delta.helpers.tools import helper_tools

        hcfg = HelpersConfig(**{"enabled": True, **{k: v for k, v in task.helpers.items()
                                                    if k in HelpersConfig.model_fields}})
        supervisor = HelperSupervisor(hcfg, router, executor, engine)
        for t in helper_tools(supervisor, read_page=False):
            registry.register(t)
        orch.helpers = supervisor
    if settings.selfheal.enabled:
        orch.healer = Healer(settings.selfheal, executor, events, can_see=lambda: False, settle_s=0.05)
        orch.healer.engine = engine
    req = TaskRequest(fill(task.request, values), channel="api")
    started = time.monotonic()
    try:
        for _ in range(task.runs - 1):  # earlier runs of a repeated task: same data folder, same script
            await asyncio.wait_for(orch.run(TaskRequest(req.text, channel="api")), task.timeout_s)
            await asyncio.wait_for(orch.drain(), 30)
        result = await asyncio.wait_for(orch.run(req), task.timeout_s)
        await asyncio.wait_for(orch.drain(), 30)
        out.status, out.answer, out.provider = result.status, result.answer, result.provider
        usage = result.usage or {}
        out.tokens_in, out.tokens_out, out.calls = (int(usage.get("input", 0)), int(usage.get("output", 0)),
                                                     int(usage.get("calls", 0)))
        out.steps = [s.tool for s in result.steps]
        out.step_details = [{"tool": s.tool, "ok": s.ok, "output": s.output[:300]} for s in result.steps]
        steps = result.steps
    except TimeoutError:
        out.status, out.error, steps = "timeout", f"took longer than {task.timeout_s:g}s", []
    except Exception as e:
        out.status, out.error, steps = "error", f"{type(e).__name__}: {e}", []
    out.seconds = round(time.monotonic() - started, 2)
    out.cost_usd = cost_of(out.provider or "scripted", out.tokens_in, out.tokens_out)
    meter = dict(getattr(router, "meter", {}) or {})
    ctx = CheckContext(sandbox=sandbox, status=out.status, answer=out.answer, steps=steps, meter=meter,
                       page_state=site.snapshot(), memories=[m.content for m in memory.list()],
                       tasks_db=data / "tasks.db", timeline=engine.timeline(req.id), undo=undo,
                       policy=executor.policy, task_id=req.id, request=req.text, memory_store=memory,
                       learning=learning_store, skills=skill_store, gate=gate)
    out.checks = await asyncio.to_thread(run_checks, ctx, fill(task.check, values))
    out.passed = bool(out.checks) and all(c["passed"] for c in out.checks)
    if task.eval_gate and gate is not None:
        # The promotion gate's eval path: a candidate this task created passed a scripted eval only when every
        # check above passed; a failing eval counts against it.
        for item in gate.items(status="candidate"):
            gate.record_eval(item["ref"], out.passed, task.id)
        if task.check_after_gate:
            after = await asyncio.to_thread(run_checks, ctx, fill(task.check_after_gate, values))
            out.checks += [{**c, "check": "after gate: " + c["check"]} for c in after]
            out.passed = out.passed and all(c["passed"] for c in after)
    if browser is not None:
        with contextlib.suppress(Exception):
            await browser.close()
    memory.close()
    store.close()
    for extra in (learning_store, skill_store):
        if extra is not None:
            extra.close()
    return out


def _learning_stores(task: EvalTask, data: Path, values: dict[str, str]) -> tuple[Any, Any]:
    """learning.db and skills.db for a task with `learning:` (with its seeded skills), else (None, None)."""
    if task.learning is None:
        return None, None
    from delta.learning.store import LearningStore
    from delta.memory.skills import SkillStore

    learning = LearningStore(data / "learning.db")
    skills = SkillStore(data / "skills.db")
    for raw in fill(task.learning.get("skills") or [], values):
        change = skills.learn(str(raw["name"]), str(raw["when_to_use"]), [str(p) for p in raw.get("procedure") or []],
                              [str(t) for t in raw.get("tools") or []],
                              recipe=json.dumps(raw["recipe"]) if raw.get("recipe") else None)
        skills.set_status(change.skill.id, str(raw.get("status", "active")))
    return learning, skills


async def run_suite(tasks: list[EvalTask], model: str | None = None, keep: bool = False,
                    log: Any = print) -> dict[str, Any]:
    # The fixture site is on 127.0.0.1: never send it through a proxy set for the internet.
    local = "127.0.0.1,localhost"
    for var in ("NO_PROXY", "no_proxy"):
        os.environ[var] = ",".join(x for x in (os.environ.get(var, ""), local) if x)
    has_browser = browser_available()
    has_ocr = await ocr_available() if any("ocr" in t.needs for t in tasks) else False
    root = Path(tempfile.mkdtemp(prefix="delta-eval-"))
    outcomes: list[TaskOutcome] = []
    router_factory = _Router(model)
    started = time.monotonic()
    try:
        with FixtureSite(SITE_DIR) as site:
            for task in tasks:
                if why := skip_reason(task, has_browser, model is None, has_ocr):
                    outcomes.append(TaskOutcome(task.id, task.suite, status="skipped", skipped=why))
                    log(f"  - {task.id}: skipped ({why})")
                    continue
                o = await run_task(task, root, site, router_factory, has_browser)
                outcomes.append(o)
                failed = "; ".join(c["detail"] for c in o.checks if not c["passed"]) or o.error
                log(f"  {'PASS' if o.passed else 'FAIL'} {task.id} ({o.seconds:.1f}s)" + ("" if o.passed else
                                                                                         f": {failed}"))
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
    return summarise(outcomes, model or "scripted", time.monotonic() - started, str(root) if keep else "")


def summarise(outcomes: list[TaskOutcome], model: str, seconds: float, workdir: str = "") -> dict[str, Any]:
    ran = [o for o in outcomes if not o.skipped]
    passed = [o for o in ran if o.passed]
    costs = [o.cost_usd for o in ran if o.cost_usd is not None]
    suites: dict[str, dict[str, int]] = {}
    for o in ran:
        s = suites.setdefault(o.suite, {"ran": 0, "passed": 0})
        s["ran"] += 1
        s["passed"] += int(o.passed)
    return {
        "model": model, "when": datetime.now().isoformat(timespec="seconds"),
        "platform": f"{platform.system()} {platform.release()}", "python": platform.python_version(),
        "tasks": len(outcomes), "ran": len(ran), "skipped": len(outcomes) - len(ran), "passed": len(passed),
        "success_rate": round(len(passed) / len(ran), 4) if ran else 0.0,
        "tokens_in": sum(o.tokens_in for o in ran), "tokens_out": sum(o.tokens_out for o in ran),
        "seconds": round(seconds, 2), "cost_usd": round(sum(costs), 6) if costs else None,
        "cost_note": "estimate from list prices; user plans (Claude Code, Codex, Cline) and local models count as 0",
        "suites": suites, "workdir": workdir, "results": [asdict(o) for o in outcomes],
    }


def markdown(report: dict[str, Any]) -> str:
    rate = f"{report['success_rate'] * 100:.1f}%"
    cost = "n/a" if report["cost_usd"] is None else f"${report['cost_usd']:.4f}"
    lines = [f"# Delta eval report ({report['model']})", "",
             f"{report['when']} on {report['platform']}, Python {report['python']}", "",
             "| Tasks | Ran | Passed | Success rate | Tokens in / out | Time | Cost estimate |",
             "|---|---|---|---|---|---|---|",
             f"| {report['tasks']} | {report['ran']} | {report['passed']} | {rate} | "
             f"{report['tokens_in']:,} / {report['tokens_out']:,} | {report['seconds']:.1f}s | {cost} |", "",
             "| Suite | Passed / ran |", "|---|---|"]
    lines += [f"| {name} | {s['passed']} / {s['ran']} |" for name, s in sorted(report["suites"].items())]
    lines += ["", "| Task | Result | Time | Tokens | Steps | Notes |", "|---|---|---|---|---|---|"]
    for r in report["results"]:
        if r["skipped"]:
            result, notes = "skipped", r["skipped"]
        else:
            result = "PASS" if r["passed"] else "FAIL"
            notes = "; ".join(c["detail"] for c in r["checks"] if not c["passed"]) or r["error"]
        notes = notes.replace("|", "/").replace("\n", " ")[:160]
        lines.append(f"| {r['id']} | {result} | {r['seconds']:.1f}s | {r['tokens_in'] + r['tokens_out']:,} | "
                     f"{len(r['steps'])} | {notes} |")
    lines += ["", report["cost_note"] + "."]
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"eval-{report['model'].replace('/', '-')}-{stamp}"
    j, m = out_dir / f"{name}.json", out_dir / f"{name}.md"
    j.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    m.write_text(markdown(report), encoding="utf-8")
    return j, m
