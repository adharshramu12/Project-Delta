"""Shared set-up for the task engine tests: a real orchestrator, executor, policy and tasks.db in a temp
folder, driven by a scripted model (tests/fakes.py)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeProvider

from delta.config import AgentConfig, Boundaries, EngineConfig, MemoryConfig, Settings
from delta.core.events import EventBus
from delta.core.orchestrator import Orchestrator
from delta.core.proof import ProofRecorder
from delta.engine.recorder import TaskEngine
from delta.engine.store import TaskStore
from delta.engine.watchdog import Watchdog
from delta.llm.router import ModelRouter
from delta.memory.store import MemoryStore
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine
from delta.tools.filesystem import filesystem_tools
from delta.tools.registry import ToolExecutor, ToolRegistry


def no_registry(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Off Windows, shell folders come from the registry module that is not there: use plain folders."""
    if sys.platform == "win32":
        return
    import delta.config
    import delta.core.orchestrator

    def known(name: str) -> Path:
        return home / ("Documents" if name == "Personal" else name)

    monkeypatch.setattr(delta.config, "known_folder", known)
    monkeypatch.setattr(delta.core.orchestrator, "known_folder", known)


class Rig:
    def __init__(self, tmp: Path, script: dict[str, list[Any]], approve: bool | None = True,
                 autonomy: str = "ask-risky", engine_cfg: EngineConfig | None = None) -> None:
        self.tmp = tmp
        self.asked: list[Any] = []
        settings = Settings(agent=AgentConfig(max_attempts=2, max_steps=8),
                            memory=MemoryConfig(auto_extract=False, learn_skills=False), data_dir=tmp)
        self.queues = {k: list(v) for k, v in script.items()}
        self.provider = FakeProvider("fake", handler=self._reply)
        self.router = ModelRouter({"fake": self.provider}, {"plan": ["fake"], "chat": ["fake"], "verify": ["fake"]})
        self.approvals = ApprovalBroker(timeout_s=2)
        if approve is not None:
            async def answer(req: Any) -> None:
                self.asked.append(req)
                asyncio.get_running_loop().call_soon(self.approvals.resolve, req.id, approve, "test")
            self.approvals.add_presenter("test", answer)
        self.audit = AuditLog(tmp / "audit.jsonl")
        policy = PolicyEngine(Boundaries(autonomy=autonomy, allowed_paths=[str(tmp)]))  # type: ignore[arg-type]
        self.registry = ToolRegistry(filesystem_tools())
        self.executor = ToolExecutor(self.registry, policy, self.approvals, self.audit)
        self.proof = ProofRecorder(tmp / "runs")
        self.store = TaskStore(tmp / "tasks.db")
        self.engine = TaskEngine(self.store, self.router, self.proof, self.registry)
        self.executor.recorder = self.engine
        self.executor.watchdog = Watchdog(engine_cfg or EngineConfig(), self.audit)
        self.orch = Orchestrator(settings, self.router, self.executor, MemoryStore(tmp / "m.db"), None, EventBus(),
                                 self.audit, proof=self.proof)
        self.orch.engine = self.engine

    def _reply(self, task: str, messages: list[Any]) -> Any:
        from fakes import js, text

        queue = self.queues.get(task) or []
        if queue:
            return queue.pop(0)
        return js({"passed": True, "reason": "ok"}) if task == "verify" else text("DONE: finished")

    def more(self, script: dict[str, list[Any]]) -> None:
        for k, v in script.items():
            self.queues.setdefault(k, []).extend(v)


class FakeContainer:
    """What delta.engine.resume needs from the Container."""

    def __init__(self, rig: Rig, guard: Any = None) -> None:
        self.engine = rig.engine
        self.orchestrator = rig.orch
        self.audit = rig.audit
        self.guard = guard
        self.chats = None
