"""Routines: do a task once, "save this as <name>", then "run <name>" repeats the steps with no AI.

On a real PC the learned-skill replay had never run once (no skill ever got a recipe from the model), so the steps
here come straight from the run, in code. These tests hold what may be saved, how a replay stops, and that every
step still goes through the executor."""

import pytest

from delta.core import routines
from delta.core.routines import (
    DeleteRoutine,
    ListRoutines,
    NameArgs,
    NoArgs,
    RoutineStore,
    RunRoutine,
    SaveArgs,
    SaveRoutine,
    build_steps,
    clean_name,
    describe,
    routine_command,
)
from delta.core.state import CURRENT_REQUEST, StepRecord, TaskRequest
from delta.tools.base import ToolResult


def step(tool, args=None, ok=True, evidence=None):
    return StepRecord(tool, args or {}, ok, "out", evidence or {})


PORTAL = [
    step("browser_open", {"url": "https://portal.example/"}),
    step("browser_read"),
    step("browser_click", {"ref": 12, "text": ""}, evidence={"target": "Timetable"}),
    step("browser_click", {"text": "Missing"}, ok=False),  # a failed try is not part of the routine
    step("browser_fill", {"ref": 4, "field": "", "value": "2026", "submit": True}, evidence={"target": "Year"}),
    step("web_search", {"query": "x"}),
]


@pytest.fixture
def store(tmp_path):
    s = RoutineStore(tmp_path / "routines.db")
    yield s
    s.close()


class Executor:
    """Records what a routine asks for; fails the calls named in `fail` (tool -> evidence)."""

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail or {}

    async def execute(self, tool, args, task_id=""):
        self.calls.append((tool, args, task_id))
        if tool in self.fail:
            return ToolResult(False, "nothing visible called 'Timetable'", self.fail[tool])
        return ToolResult(True, f"did {tool}", {})


@pytest.fixture
def in_chat():
    req = TaskRequest("save this as timetable", channel="api", chat_id="chat-1")
    token = CURRENT_REQUEST.set(req)
    yield req
    CURRENT_REQUEST.reset(token)


# --- what may be saved -----------------------------------------------------------------------------------


def test_only_the_steps_that_worked_are_kept_and_element_numbers_become_names():
    steps, why = build_steps(PORTAL)
    assert why == "" and steps == [
        {"tool": "browser_open", "args": {"url": "https://portal.example/"}},
        {"tool": "browser_click", "args": {"text": "Timetable"}},
        {"tool": "browser_fill", "args": {"field": "Year", "value": "2026", "submit": True}},
    ]
    assert [describe(s) for s in steps] == ["open https://portal.example/", "click 'Timetable'",
                                           "type into 'Year' and press Enter"]


@pytest.mark.parametrize("tool", ["click_at", "do_actions", "look_at_screen", "click_mark"])
def test_a_run_that_aimed_by_position_or_by_looking_cannot_be_saved(tool):
    steps, why = build_steps([step("open_app", {"name": "notepad"}), step(tool, {"x": 5, "y": 9})])
    assert steps == [] and tool in why and "cannot be saved" in why


def test_an_element_number_without_a_recorded_name_cannot_be_saved():
    steps, why = build_steps([step("browser_open", {"url": "https://a.example"}), step("browser_click", {"ref": 3})])
    assert steps == [] and "element number" in why


def test_nothing_to_repeat_too_long_and_never_saved_tools():
    assert build_steps([step("web_search"), step("browser_read")]) == (
        [], "the last task had no steps that can be repeated (it only looked things up or answered).")
    many = [step("open_app", {"name": f"a{i}"}) for i in range(routines.MAX_STEPS + 1)]
    assert build_steps(many)[0] == [] and "at most" in build_steps(many)[1]
    kept, _ = build_steps([step("open_app", {"name": "notepad"}), step("solve_captcha"), step("run_routine"),
                           step("remember", {"fact": "x"})])
    assert kept == [{"tool": "open_app", "args": {"name": "notepad"}}]


# --- the store ---------------------------------------------------------------------------------------------


def test_the_store_keeps_names_tidy_and_saving_again_replaces(store):
    assert clean_name('  My   "Time/Table"!!  ') == "My Time Table"
    store.save("Time Table", "open the timetable", [{"tool": "open_app", "args": {"name": "a"}}])
    store.ran("time table", ok=True)
    assert store.get("TIME   table").runs == 1 and store.names() == {"time table"}
    store.save("time table", "again", [{"tool": "open_app", "args": {"name": "b"}}])
    r = store.get("time table")
    assert r.runs == 0 and r.steps[0]["args"] == {"name": "b"} and len(store.list()) == 1
    assert store.delete("Time Table") and store.get("time table") is None and not store.delete("time table")
    with pytest.raises(ValueError):
        store.save("!!!", "x", [])


# --- save, run, list, delete --------------------------------------------------------------------------------


async def test_save_takes_the_last_finished_task_of_this_chat_and_says_what_it_will_repeat(store, in_chat):
    tool = SaveRoutine(store)
    r = await tool.run(SaveArgs(name="timetable"))
    assert not r.ok and "no finished task in this chat" in r.output
    asked = []
    tool.last_done = lambda chat: asked.append(chat) or ("open my portal timetable", PORTAL)
    r = await tool.run(SaveArgs(name="timetable"))
    assert r.ok and asked == ["chat-1"] and r.evidence["steps"] == 3
    assert "1. open https://portal.example/" in r.output and "3. type into 'Year' and press Enter" in r.output
    assert "still asks" in r.output and store.get("timetable").request == "open my portal timetable"
    tool.last_done = lambda chat: ("click around", [step("click_at", {"x": 1, "y": 2})])
    r = await tool.run(SaveArgs(name="other"))
    assert not r.ok and "could not save" in r.output and store.get("other") is None


async def test_run_sends_every_step_through_the_executor_in_order(store, in_chat):
    store.save("timetable", "x", build_steps(PORTAL)[0])
    tool = RunRoutine(store)
    tool.executor = Executor()
    r = await tool.run(NameArgs(name="Timetable"))
    assert r.ok and r.evidence["verified"] and "all 3 step(s) worked" in r.output
    assert [c[0] for c in tool.executor.calls] == ["browser_open", "browser_click", "browser_fill"]
    assert all(c[2] == in_chat.id for c in tool.executor.calls)  # under this task: approvals, audit, stop
    assert store.get("timetable").runs == 1 and store.get("timetable").failures == 0


async def test_the_first_failed_step_stops_the_routine_and_the_rest_never_runs(store, in_chat):
    store.save("timetable", "x", build_steps(PORTAL)[0])
    tool = RunRoutine(store)
    tool.executor = Executor(fail={"browser_click": {}})
    r = await tool.run(NameArgs(name="timetable"))
    assert not r.ok and "stopped at step 2 of 3 (click 'Timetable')" in r.output
    assert "Steps 1 to 1 were done" in r.output and "remaining steps did not run" in r.output
    assert [c[0] for c in tool.executor.calls] == ["browser_open", "browser_click"]  # no fill after the failure
    assert store.get("timetable").failures == 1
    tool.executor = Executor(fail={"browser_open": {"user_denied": True}})
    r = await tool.run(NameArgs(name="timetable"))
    assert not r.ok and "you said no to it" in r.output and "Nothing was done" in r.output


async def test_a_step_that_must_never_be_in_a_routine_is_refused_even_if_the_file_was_edited(store, in_chat):
    store.save("bad", "x", [{"tool": "open_app", "args": {"name": "a"}}, {"tool": "click_at", "args": {"x": 1}}])
    tool = RunRoutine(store)
    tool.executor = Executor()
    r = await tool.run(NameArgs(name="bad"))
    assert not r.ok and "not allowed in a routine" in r.output and len(tool.executor.calls) == 1


async def test_unknown_names_list_and_delete(store, in_chat):
    run = RunRoutine(store)
    run.executor = Executor()
    r = await run.run(NameArgs(name="nope"))
    assert not r.ok and "none saved yet" in r.output
    assert "No routines saved yet" in (await ListRoutines(store).run(NoArgs())).output
    store.save("timetable", "x", build_steps(PORTAL)[0])
    store.ran("timetable", ok=False)
    listing = (await ListRoutines(store).run(NoArgs())).output
    assert "timetable (run 1 time(s), 1 stopped early)" in listing and "click 'Timetable'" in listing
    assert (await DeleteRoutine(store).run(NameArgs(name="timetable"))).ok
    assert not (await DeleteRoutine(store).run(NameArgs(name="timetable"))).ok


# --- the user's own words, with no AI ---------------------------------------------------------------------


def test_the_four_commands_are_recognised_and_run_needs_a_name_that_exists(monkeypatch):
    monkeypatch.setattr(routines, "names_now", lambda: {"timetable", "morning check"})
    assert routine_command("save this as timetable") == ("save_routine", {"name": "timetable"})
    assert routine_command("Save that as a routine called Morning Check") == ("save_routine", {"name": "Morning Check"})
    assert routine_command("run timetable") == ("run_routine", {"name": "timetable"})
    assert routine_command("run my morning check routine") == ("run_routine", {"name": "morning check"})
    assert routine_command("do timetable again") == ("run_routine", {"name": "timetable"})
    assert routine_command("timetable") == ("run_routine", {"name": "timetable"})  # just its name
    assert routine_command("my routines") == ("list_routines", {})
    assert routine_command("delete routine timetable") == ("delete_routine", {"name": "timetable"})
    # not routine commands: these go to the planner or to other instant commands as before
    for text in ("run a virus scan", "run notepad", "save this file as report.pdf", "remember this as important",
                 "do my homework", "open timetable.com"):
        assert routine_command(text) is None, text


def test_instant_commands_reach_the_routine_tools_only_when_they_exist(monkeypatch):
    from delta.core.intents import match_intent

    monkeypatch.setattr(routines, "names_now", lambda: {"timetable"})
    tools = {"save_routine", "run_routine", "list_routines", "delete_routine", "open_app"}
    assert match_intent("run timetable", tools).tool == "run_routine"
    assert match_intent("save this as daily", tools).args == {"name": "daily"}
    assert match_intent("run timetable", {"open_app"}) is None
    assert match_intent("open notepad", tools).tool == "open_app"  # the other commands are untouched


def test_the_orchestrator_remembers_only_finished_tasks_and_not_routine_commands(tmp_path):
    from delta.bootstrap import build
    from delta.config import Settings
    from delta.core.state import TaskResult

    c = build(Settings(data_dir=tmp_path))
    assert {"save_routine", "run_routine", "list_routines", "delete_routine"} <= set(c.registry.names())
    o = c.orchestrator
    req = TaskRequest("open my portal", channel="api", chat_id="c1")
    o._note_done(req, TaskResult(req.id, "failed", "no", 1, list(PORTAL)))
    assert o.last_done("c1") is None
    o._note_done(req, TaskResult(req.id, "done", "ok", 1, list(PORTAL)))
    assert o.last_done("c1")[0] == "open my portal" and o.last_done("other chat") is None
    again = TaskRequest("run timetable", channel="api", chat_id="c1")  # running a routine is not "the last task"
    o._note_done(again, TaskResult(again.id, "done", "ran", 1, [step("browser_open"), step("run_routine")]))
    assert o.last_done("c1")[0] == "open my portal"
    assert c.registry.get("save_routine").last_done == o.last_done
