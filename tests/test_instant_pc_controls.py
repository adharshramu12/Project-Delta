"""Volume, mute, brightness and dark mode answer at once with no AI call (first-day feedback: "increase the
colume" was slow and sometimes ended in "sorry")."""

import asyncio
from pathlib import Path

from fakes import FakeProvider

from delta.config import AgentConfig, Boundaries, MemoryConfig, Settings
from delta.core.events import EventBus
from delta.core.intents import match_intent
from delta.core.orchestrator import Orchestrator
from delta.core.state import TaskRequest
from delta.llm.router import ModelRouter
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine
from delta.tools.pc import SetVolume
from delta.tools.registry import ToolExecutor, ToolRegistry

PC = {"set_volume", "set_brightness", "set_windows_theme"}


def test_the_ways_people_ask():
    cases = {"increase the colume": ("set_volume", {"change": 10}), "volume up buddy": ("set_volume", {"change": 10}),
             "reduce volume by 20": ("set_volume", {"change": -20}), "set volume to 50": ("set_volume", {"level": 50}),
             "max volume": ("set_volume", {"level": 100}), "make it louder": ("set_volume", {"change": 10}),
             "mute": ("set_volume", {"mute": True}), "brightness 70": ("set_brightness", {"level": 70}),
             "decrease brightness": ("set_brightness", {"change": -10}),
             "turn off dark mode": ("set_windows_theme", {"mode": "light"})}
    for text, (tool, args) in cases.items():
        m = match_intent(text, PC)
        assert m is not None and (m.tool, m.args) == (tool, args), text
    for text in ("mute ravi on whatsapp", "increase my productivity", "the volume of this video is low, why?"):
        assert match_intent(text, PC) is None, text


async def test_increase_volume_needs_no_ai_call(tmp_path: Path, monkeypatch):
    from delta.system import pc_control

    seen = []
    def fake(level=None, step=0, mute=None):
        seen.append(step)
        return 60, False

    monkeypatch.setattr(pc_control, "set_volume", fake)
    ai = FakeProvider("fake", [])  # any AI call would fail the task
    router = ModelRouter({"fake": ai}, {"plan": ["fake"], "chat": ["fake"], "verify": ["fake"]})
    audit = AuditLog(tmp_path / "audit.jsonl")
    approvals = ApprovalBroker(timeout_s=1)
    executor = ToolExecutor(ToolRegistry([SetVolume()]), PolicyEngine(Boundaries()), approvals, audit)
    orch = Orchestrator(Settings(agent=AgentConfig(max_attempts=1), memory=MemoryConfig(auto_extract=False),
                                 data_dir=tmp_path), router, executor, None, None, EventBus(), audit)
    r = await asyncio.wait_for(orch.run(TaskRequest("increase the colume")), 10)
    assert r.status == "done" and seen == [10] and ai.calls == []
