"""The orchestrator writes every task's states and steps through the task engine."""

import asyncio
from pathlib import Path

import pytest
from engine_helpers import Rig, no_registry
from fakes import call, js, text

from delta.core.state import TaskRequest
from delta.engine.states import TaskState


@pytest.fixture(autouse=True)
def _folders(tmp_path, monkeypatch):
    no_registry(monkeypatch, tmp_path)


def plan(**extra):
    return js({"needs_tools": True, "goal": "g", "success_criteria": ["it is done"], **extra})


async def test_a_task_moves_through_its_states_and_logs_each_step(tmp_path):
    target = str(tmp_path / "notes.txt")
    rig = Rig(tmp_path, {"plan": [plan()],
                         "chat": [call("write_file", path=target, content="hi"), call("path_info", path=target),
                                  text("DONE: written")],
                         "verify": [js({"passed": True, "reason": "exists"})]})
    req = TaskRequest("write notes")
    result = await rig.orch.run(req)
    assert result.status == "done"
    task = rig.store.get(req.id)
    assert task["state"] == "done" and task["steps"] == 2 and task["autonomy"] == "ask-risky"
    states = [e["summary"] for e in rig.store.events(req.id, ["state"])]
    assert states == ["-> queued", "queued -> planning", "planning -> acting", "acting -> verifying",
                      "verifying -> done"]
    timeline = rig.engine.timeline(req.id)
    steps = timeline["steps"]
    assert [s["tool"] for s in steps] == ["write_file", "path_info"]
    assert steps[0]["side_effect"] and not steps[1]["side_effect"]
    assert all(s["ok"] and s["duration_ms"] is not None and not s["unconfirmed"] for s in steps)
    assert steps[0]["model"] == "fake" and steps[0]["tokens_in"] > 0  # the call that chose this step
    assert timeline["checks"] and timeline["checks"][0]["ok"]


async def test_a_question_leaves_the_task_waiting_for_the_user(tmp_path):
    rig = Rig(tmp_path, {"plan": [plan()], "chat": [text("NEED_USER: which folder?")]})
    req = TaskRequest("tidy my files")
    result = await rig.orch.run(req)
    assert result.status == "needs_user"
    assert rig.store.get(req.id)["state"] == TaskState.WAITING_USER


async def test_a_refused_step_is_logged_without_running(tmp_path):
    target = tmp_path / "x.txt"
    rig = Rig(tmp_path, approve=False, script={"plan": [plan()],
                                               "chat": [call("write_file", path=str(target), content="x")]})
    req = TaskRequest("make x")
    result = await rig.orch.run(req)
    assert result.status == "cancelled" and not target.exists()
    steps = rig.engine.timeline(req.id)["steps"]
    assert len(steps) == 1 and steps[0]["ran"] is False and steps[0]["ok"] is False
    assert rig.store.get(req.id)["state"] == "cancelled"


async def test_stop_marks_the_task_cancelled(tmp_path):
    rig = Rig(tmp_path, {"plan": [plan()], "chat": [call("write_file", path=str(tmp_path / "a"), content="a")]},
              approve=None)  # the approval waits forever: Stop arrives while it waits
    req = TaskRequest("slow")
    task = asyncio.create_task(rig.orch.run(req))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rig.store.get(req.id)["state"] == "cancelled"


async def test_a_broken_task_db_never_fails_the_task(tmp_path):
    rig = Rig(tmp_path, {"plan": [plan()], "chat": [call("path_info", path=str(tmp_path)), text("DONE: ok")]})
    rig.store.close()  # every write now raises inside the engine
    result = await rig.orch.run(TaskRequest("look"))
    assert result.status == "done"


async def test_engine_off_runs_exactly_as_before(tmp_path):
    rig = Rig(tmp_path, {"plan": [plan()], "chat": [call("path_info", path=str(tmp_path)), text("DONE: ok")]})
    rig.orch.engine = None
    rig.executor.recorder = None
    rig.executor.watchdog = None
    req = TaskRequest("look")
    assert (await rig.orch.run(req)).status == "done"
    assert rig.store.get(req.id) is None


def test_recorded_args_never_hold_secrets(tmp_path):
    rig = Rig(tmp_path, {"plan": [plan()],
                         "chat": [call("write_file", path=str(tmp_path / "k.txt"),
                                       content="sk-proj-SUPERSECRETVALUE1234567890"), text("DONE: ok")]})
    req = TaskRequest("save my key sk-proj-SUPERSECRETVALUE1234567890")
    assert asyncio.run(rig.orch.run(req)).status == "done"
    rig.store.close()
    raw = b"".join(p.read_bytes() for p in Path(tmp_path).glob("tasks.db*"))
    assert b"SUPERSECRETVALUE" not in raw
    assert (tmp_path / "k.txt").read_text() == "sk-proj-SUPERSECRETVALUE1234567890"
