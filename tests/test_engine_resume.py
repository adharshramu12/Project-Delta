"""Crash resume: interrupted tasks are found at start-up, the user decides, and nothing that already
changed something is done again without a human yes."""

import asyncio

import pytest
from engine_helpers import FakeContainer, Rig, no_registry
from fakes import call, js, text

from delta.core.state import TaskRequest
from delta.engine.resume import discard, prepare_resume
from delta.engine.states import TaskState


@pytest.fixture(autouse=True)
def _folders(tmp_path, monkeypatch):
    no_registry(monkeypatch, tmp_path)


class Guard:
    def __init__(self, locked=False, has_pin=True, unlocked=False):
        self.locked, self.has_pin, self._unlocked = locked, has_pin, unlocked

    def is_unlocked(self, user):
        return self._unlocked

    def locked_message(self):
        return "Remote control is locked."


def plan():
    return js({"needs_tools": True, "goal": "g", "success_criteria": ["done"]})


def crash_after(rig, req, write_target, finished=True):
    """A task that wrote a file and then Delta died: rows as a crash would leave them."""
    rig.engine.begin(req, "ask-risky")
    tool = rig.registry.get("write_file")
    args = {"path": write_target, "content": "first", "append": False}
    step = rig.engine.step_started(req.id, "write_file", args, tool)
    if finished:
        from delta.tools.base import ToolResult

        rig.engine.step_finished(req.id, step, "write_file", args, ToolResult(True, "wrote 5 bytes"), 0.01, tool)


async def test_startup_marks_running_tasks_interrupted(tmp_path):
    rig = Rig(tmp_path, {})
    req = TaskRequest("write the report", id="crash1")
    crash_after(rig, req, str(tmp_path / "r.txt"))
    assert rig.store.get("crash1")["state"] == "acting"
    found = rig.engine.recover()
    assert [t["id"] for t in found] == ["crash1"]
    assert rig.store.get("crash1")["state"] == "interrupted"
    assert rig.engine.recover() == []  # only once


async def test_resume_rebuilds_context_and_never_repeats_a_done_side_effect(tmp_path):
    target = str(tmp_path / "r.txt")
    (tmp_path / "r.txt").write_text("first")
    rig = Rig(tmp_path, {}, approve=False, autonomy="autopilot")  # even autopilot asks; the user says no
    req = TaskRequest("write the report", id="crash2")
    crash_after(rig, req, target)
    rig.engine.recover()
    c = FakeContainer(rig)
    decision = prepare_resume(c, "crash2", "api")
    assert decision.ok and decision.request.id == "crash2" and decision.autonomy_cap == "ask-risky"
    assert "ALREADY DONE" in decision.request.history and "write_file" in decision.request.history
    assert rig.store.get("crash2")["state"] == "queued"
    # The model tries the very same write again: the user is asked, says no, the file is untouched.
    rig.more({"plan": [plan()], "chat": [call("write_file", path=target, content="first")]})
    result = await rig.orch.run(decision.request)
    assert result.status == "cancelled"
    assert rig.asked and "already ran" in rig.asked[0].reason and rig.asked[0].for_task is False
    assert (tmp_path / "r.txt").read_text() == "first"
    assert rig.store.get("crash2")["resumes"] == 1


async def test_a_different_step_after_resume_follows_the_normal_rules(tmp_path):
    rig = Rig(tmp_path, {}, approve=True, autonomy="trusted")
    req = TaskRequest("write two files", id="crash3")
    crash_after(rig, req, str(tmp_path / "a.txt"))
    rig.engine.recover()
    decision = prepare_resume(FakeContainer(rig), "crash3", "api")
    rig.more({"plan": [plan()], "chat": [call("write_file", path=str(tmp_path / "b.txt"), content="b"),
                                         text("DONE: wrote b")]})
    result = await rig.orch.run(decision.request)
    assert result.status == "done" and (tmp_path / "b.txt").read_text() == "b"
    assert rig.asked == []  # trusted, inside the allowed folder, not a repeat: no question
    assert rig.store.get("crash3")["state"] == "done"


async def test_an_unfinished_side_effect_is_confirmed_first(tmp_path):
    rig = Rig(tmp_path, {})
    req = TaskRequest("send the file", id="crash4")
    crash_after(rig, req, str(tmp_path / "x.txt"), finished=False)
    rig.engine.recover()
    c = FakeContainer(rig)
    first = prepare_resume(c, "crash4", "api")
    assert not first.ok and first.needs_confirmation and {o["id"] for o in first.options} == {"done", "not_done"}
    assert rig.store.get("crash4")["state"] == "interrupted"  # nothing moved until the user answers
    plan_ = rig.engine.plan_resume("crash4")
    assert plan_.unconfirmed is not None and plan_.unconfirmed.tool == "write_file"
    second = prepare_resume(c, "crash4", "api", confirm_last="done")
    assert second.ok
    args = {"path": str(tmp_path / "x.txt"), "content": "first", "append": False}
    assert rig.engine.repeat_reason("crash4", "write_file", args)  # it happened: a repeat needs a yes


async def test_not_happened_allows_the_step_again(tmp_path):
    rig = Rig(tmp_path, {})
    req = TaskRequest("send the file", id="crash5")
    crash_after(rig, req, str(tmp_path / "x.txt"), finished=False)
    rig.engine.recover()
    assert prepare_resume(FakeContainer(rig), "crash5", "api", confirm_last="not_done").ok
    args = {"path": str(tmp_path / "x.txt"), "content": "first", "append": False}
    assert rig.engine.repeat_reason("crash5", "write_file", args) is None


@pytest.mark.parametrize(("guard", "words"), [
    (None, "not available"), (Guard(locked=True), "locked"), (Guard(has_pin=False), "set a remote PIN"),
    (Guard(unlocked=False), "/unlock"),
])
async def test_remote_resume_obeys_remote_guard(tmp_path, guard, words):
    rig = Rig(tmp_path, {})
    req = TaskRequest("x", id="crash6")
    crash_after(rig, req, str(tmp_path / "x.txt"))
    rig.engine.recover()
    decision = prepare_resume(FakeContainer(rig, guard), "crash6", "telegram", "42")
    assert not decision.ok and words in decision.message
    assert rig.store.get("crash6")["state"] == "interrupted"


async def test_remote_resume_with_pin_keeps_the_original_cap(tmp_path):
    rig = Rig(tmp_path, {})
    req = TaskRequest("x", id="crash7")
    crash_after(rig, req, str(tmp_path / "x.txt"))
    rig.engine.recover()
    decision = prepare_resume(FakeContainer(rig, Guard(unlocked=True)), "crash7", "telegram", "42")
    assert decision.ok and decision.autonomy_cap == "ask-risky"
    assert decision.request.channel == "telegram" and decision.request.user == "42"


async def test_only_interrupted_tasks_resume_and_discard_is_final(tmp_path):
    rig = Rig(tmp_path, {"plan": [js({"needs_tools": False, "answer": "hi"})]})
    req = TaskRequest("hello")
    await rig.orch.run(req)
    c = FakeContainer(rig)
    assert not prepare_resume(c, req.id, "api").ok
    assert not prepare_resume(c, "nope", "api").ok
    crash_after(rig, TaskRequest("y", id="crash8"), str(tmp_path / "y.txt"))
    rig.engine.recover()
    ok, _ = discard(c, "crash8", "api")
    assert ok and rig.store.get("crash8")["state"] == TaskState.CANCELLED
    assert not discard(c, "crash8", "api")[0]
    assert not prepare_resume(c, "crash8", "api").ok


def test_nothing_resumes_by_itself(tmp_path):
    rig = Rig(tmp_path, {})
    crash_after(rig, TaskRequest("z", id="crash9"), str(tmp_path / "z.txt"))
    rig.engine.recover()
    asyncio.run(asyncio.sleep(0.05))
    assert rig.store.get("crash9")["state"] == "interrupted"
    assert rig.provider.calls == []
