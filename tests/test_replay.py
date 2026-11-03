"""Skill replay: proven skills re-run their recorded tool calls with one small AI call."""

import json
import sqlite3

from fakes import FakeProvider, js
from pydantic import BaseModel

from delta.config import AgentConfig, Boundaries, MemoryConfig, Settings
from delta.core.events import EventBus
from delta.core.orchestrator import Orchestrator
from delta.core.replay import Recipe, fill, match_slots, validate_recipe
from delta.core.state import StepRecord, TaskRequest
from delta.llm.router import ModelRouter
from delta.memory.skills import SkillStore
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine
from delta.tools.base import Risk, Tool, ToolResult
from delta.tools.registry import ToolExecutor, ToolRegistry

RUN = [StepRecord("list_windows", {}, True, "", {}),
       StepRecord("open_app", {"name": "notepad"}, True, "opened", {}),
       StepRecord("read_window", {}, True, "", {}),
       StepRecord("type_text", {"text": "Speed test by Delta"}, True, "typed", {})]
RAW = {"steps": [{"tool": "open_app", "args": {"name": "notepad"}}, {"tool": "read_window", "args": {}},
                 {"tool": "type_text", "args": {"text": "{text}"}}], "slots": {"text": "what to type"}}


def test_recipe_keeps_real_calls_in_order_and_drops_reading_steps():
    r = validate_recipe(RAW, RUN)
    assert r and [s["tool"] for s in r.steps] == ["open_app", "type_text"] and r.slots == {"text": "what to type"}
    assert Recipe.from_json(r.to_json()).steps == r.steps


def test_unsafe_or_invented_recipes_are_refused():
    assert validate_recipe({**RAW, "steps": [{"tool": "delete_file", "args": {"path": "x"}}]}, RUN) is None
    assert validate_recipe({**RAW, "slots": {}}, RUN) is None  # {text} used but not declared
    backwards = {**RAW, "steps": list(reversed(RAW["steps"]))}
    assert validate_recipe(backwards, RUN) is None  # not the order it really happened in
    pixel_run = [*RUN, StepRecord("click_at", {"x": 1, "y": 2}, True, "", {})]
    assert validate_recipe(RAW, pixel_run) is None  # positions change: not replayable


def test_fill_puts_values_everywhere():
    assert fill({"q": "{song} song", "list": ["{song}"], "n": 3}, {"song": "Inthandham"}) == \
        {"q": "Inthandham song", "list": ["Inthandham"], "n": 3}


async def test_slot_values_must_come_from_the_request():
    recipe = Recipe(RAW["steps"], {"text": "what to type"})

    def router(reply):
        return ModelRouter({"h": FakeProvider("h", [js(reply)])}, {"verify": ["h"]})

    req = "open notepad and type hello world"
    assert await match_slots(router({"fits": True, "values": {"text": "hello world"}}), req, "n", "w", recipe) \
        == {"text": "hello world"}
    assert await match_slots(router({"fits": True, "values": {"text": "invented text"}}), req, "n", "w", recipe) \
        is None
    assert await match_slots(router({"fits": False, "values": {}}), req, "n", "w", recipe) is None


def test_old_skill_databases_get_the_recipe_column(tmp_path):
    db = tmp_path / "skills.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE skills(id INTEGER PRIMARY KEY, name TEXT NOT NULL, when_to_use TEXT NOT NULL, "
                "procedure TEXT NOT NULL, tools TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'candidate', "
                "successes INTEGER NOT NULL DEFAULT 1, failures INTEGER NOT NULL DEFAULT 0, "
                "source TEXT NOT NULL DEFAULT 'learned', created TEXT NOT NULL, last_used TEXT)")
    con.execute("INSERT INTO skills(name, when_to_use, procedure, tools, created) VALUES "
                "('old', 'old skill', '[]', '[]', 'x')")
    con.commit()
    con.close()
    store = SkillStore(db)
    assert store.list()[0].name == "old" and store.list()[0].recipe is None
    assert store.set_recipe(store.list()[0].id, "{}")


class NameArgs(BaseModel):
    name: str


class TextArgs(BaseModel):
    text: str


class OpenApp(Tool[NameArgs]):
    name, description, risk, Args = "open_app", "open", Risk.READ, NameArgs

    async def run(self, args):
        return ToolResult(True, f"opened {args.name}")


class TypeText(Tool[TextArgs]):
    name, description, risk, Args = "type_text", "type", Risk.READ, TextArgs

    def __init__(self):
        self.typed = []

    async def run(self, args):
        self.typed.append(args.text)
        return ToolResult(True, f"typed {args.text}", {"verified": True})


async def test_a_proven_skill_replays_with_one_small_call(tmp_path):
    skills = SkillStore(tmp_path / "s.db")
    s = skills.learn("open notepad and type text", "open notepad and type text into it", ["open", "type"],
                     ["open_app", "type_text"], recipe=validate_recipe(RAW, RUN).to_json()).skill
    skills.record(s.id, success=True)  # proven: active
    calls = []

    def handler(task, messages):
        calls.append(task)
        return js({"fits": True, "values": {"text": "hello world"}})

    typer = TypeText()
    executor = ToolExecutor(ToolRegistry([OpenApp(), typer]), PolicyEngine(Boundaries()), ApprovalBroker(timeout_s=1),
                            AuditLog(tmp_path / "a.jsonl"))
    orch = Orchestrator(Settings(agent=AgentConfig(), memory=MemoryConfig(enabled=False), data_dir=tmp_path),
                        ModelRouter({"h": FakeProvider("h", handler=handler)}, {"verify": ["h"], "plan": ["h"]}),
                        executor, None, None, EventBus(), AuditLog(tmp_path / "b.jsonl"), skills)
    r = await orch.run(TaskRequest("open notepad and type hello world"))
    assert r.status == "done" and r.fast and r.skill_id == s.id
    assert calls == ["verify"]  # the slot fill: no planner, no agent loop
    assert typer.typed == ["hello world"]
    assert skills.get(s.id).successes == 3
    assert json.loads(skills.get(s.id).recipe)["slots"] == {"text": "what to type"}
