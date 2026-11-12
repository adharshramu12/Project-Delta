"""Watch me do it: the user does a task by hand in Delta's browser and Delta saves it as a routine.

A live check in a real browser found two things before any user could: a form sent with GET put a typed password
into the page address the recorder kept, and Enter in a form recorded a second, phantom click on the submit button
(a replay would then fail on a button that is already gone). Both are held here."""

import sqlite3
from types import SimpleNamespace

import pytest

from delta.core import routines
from delta.core.routines import NameArgs, RoutineStore, RunRoutine, routine_command
from delta.core.watch_me import RECORDER_JS, RecordArgs, RecordRoutine, WatchMe, bare, events_to_steps
from delta.tools.base import ToolResult


def ev(kind, t, **kw):
    return {"kind": kind, "t": t, **kw}


@pytest.fixture
def store(tmp_path):
    s = RoutineStore(tmp_path / "routines.db")
    yield s
    s.close()


# --- what the page reports -> steps ------------------------------------------------------------------------


def test_clicks_typing_choices_and_enter_become_steps_by_name():
    events = [ev("click", 2, name="Timetable"), ev("nav", 2.5, url="https://portal.example/tt"),
              ev("fill", 6, field="Year", value="2026"), ev("select", 8, field="Branch", option="CSE"),
              ev("fill", 10, field="Search", value="mat"), ev("enter", 11, field="Search", value="maths")]
    steps, notes = events_to_steps(events, "https://portal.example/")
    assert notes == [] and steps == [
        {"tool": "browser_open", "args": {"url": "https://portal.example/"}},
        {"tool": "browser_click", "args": {"text": "Timetable"}},
        {"tool": "browser_fill", "args": {"field": "Year", "value": "2026", "submit": False}},
        {"tool": "browser_select", "args": {"label": "Branch", "option": "CSE"}},
        {"tool": "browser_fill", "args": {"field": "Search", "value": "maths", "submit": True}},  # one step, last text
    ]


def test_a_page_change_caused_by_an_action_is_not_a_step_but_a_typed_address_is():
    events = [ev("nav", 1, url="https://a.example/"), ev("click", 5, name="Next"),
              ev("nav", 5.4, url="https://a.example/2"),  # the click did this
              ev("nav", 30, url="https://b.example/"),     # typed into the address bar much later
              ev("nav", 30.1, url="https://b.example/")]   # the same address twice is one step
    steps, _ = events_to_steps(events, "about:blank")
    assert [s["args"].get("url", s["args"].get("text")) for s in steps] == \
        ["https://a.example/", "Next", "https://b.example/"]


def test_enter_in_a_form_does_not_also_record_the_browsers_own_click_on_submit():
    events = [ev("enter", 4.00, field="Search", value="maths"), ev("click", 4.02, name="Show"),
              ev("click", 9, name="Download")]  # a real click later is kept
    steps, _ = events_to_steps(events, "https://a.example/")
    assert [s["tool"] for s in steps] == ["browser_open", "browser_fill", "browser_click"]
    assert steps[-1]["args"] == {"text": "Download"}


def test_a_password_is_never_a_step_and_never_stays_in_an_address():
    events = [ev("fill", 2, field="User", value="adharsh"), ev("fill", 4, field="Password", value="", secret=True),
              ev("enter", 5, field="Password", value="", secret=True),
              ev("nav", 30, url="https://a.example/home?user=adharsh&pw=hunter2#x")]
    steps, notes = events_to_steps(events, "https://a.example/login")
    assert "hunter2" not in str(steps) and "Password" not in str(steps)
    assert steps[-1]["args"] == {"url": "https://a.example/home"}  # nothing after ? once a password was typed
    assert len(notes) == 1 and "did not record it" in notes[0]
    assert bare("https://a.example/x?a=1#b") == "https://a.example/x" and bare("https://a.example/") == "https://a.example/"


def test_unnamed_fields_are_said_and_a_long_recording_is_cut_with_a_note():
    steps, notes = events_to_steps([ev("fill", 1, field="", value="x"), ev("select", 2, field="", option="A")], "")
    assert steps == [] and len(notes) == 2 and "no name" in notes[0]
    many = [ev("click", i * 10, name=f"b{i}") for i in range(routines.MAX_STEPS + 5)]
    steps, notes = events_to_steps(many, "")
    assert len(steps) == routines.MAX_STEPS and "only the first" in notes[0]


def test_the_page_script_reports_real_actions_only_and_no_secret_text():
    assert RECORDER_JS.count("ev.isTrusted") == 3  # clicks, changes and Enter made by the person, not by scripts
    assert 'value: hide ? ""' in RECORDER_JS and '"password"' in RECORDER_JS and "one-time-code" in RECORDER_JS
    assert "clientX" not in RECORDER_JS and "pageX" not in RECORDER_JS  # names, never positions


# --- the recorder and its tool ----------------------------------------------------------------------------


class FakePage:
    def __init__(self, ctx, url="about:blank"):
        self.context, self.url, self.handlers = ctx, url, []

    def on(self, name, fn):
        self.handlers.append(name)

    async def evaluate(self, js):
        return None

    async def goto(self, url, **kw):
        self.url = url

    async def bring_to_front(self):
        return None


class FakeContext:
    def __init__(self):
        self.bindings, self.scripts, self.pages = {}, [], []

    async def expose_binding(self, name, fn):
        self.bindings[name] = fn

    async def add_init_script(self, js):
        self.scripts.append(js)

    def on(self, name, fn):
        pass


def fake_hub():
    ctx = FakeContext()
    page = FakePage(ctx)
    ctx.pages.append(page)

    async def get_page():
        return page

    mgr = SimpleNamespace(page=get_page, hold_until=0.0)
    return SimpleNamespace(mgr=mgr, settings=SimpleNamespace(browser=SimpleNamespace(private_engine="auto"))), ctx


async def test_start_wires_the_browser_once_and_stop_with_a_name_saves_a_private_routine(store):
    hub, ctx = fake_hub()
    watch = WatchMe(hub, store)
    tool = RecordRoutine(watch)
    r = await tool.run(RecordArgs(action="start", url="portal.example"))
    assert r.ok and watch.recording and "never what you type into a password field" in r.output
    assert list(ctx.bindings) == ["__deltaRecord"] and len(ctx.scripts) == 1 and hub.mgr.hold_until > 0
    again = await tool.run(RecordArgs(action="start"))
    assert again.ok and "already recording" in again.output and len(ctx.scripts) == 1  # not wired twice
    heard = ctx.bindings["__deltaRecord"]
    heard(None, {"kind": "click", "name": "Timetable", "url": "https://portal.example/", "junk": "dropped"})
    heard(None, {"kind": "fill", "field": "Password", "value": "leak", "secret": True})  # even if a page sent one
    heard(None, "not an event")
    assert "leak" not in str(watch.rec.events) and "junk" not in str(watch.rec.events)
    r = await tool.run(RecordArgs(action="stop", name="Time Table"))
    assert r.ok and not watch.recording and "1. open https://portal.example" in r.output
    assert "2. click 'Timetable'" in r.output and "did not record it" in r.output
    saved = store.get("time table")
    assert saved.browser == "private" and len(saved.steps) == 2 and watch.pending is None


async def test_stop_without_a_name_keeps_the_steps_until_they_are_named_and_cancel_drops_them(store):
    hub, ctx = fake_hub()
    watch = WatchMe(hub, store)
    tool = RecordRoutine(watch)
    await tool.run(RecordArgs(action="start", url="a.example"))
    ctx.bindings["__deltaRecord"](None, {"kind": "click", "name": "Go"})
    r = await tool.run(RecordArgs(action="stop"))
    assert r.ok and r.evidence["needs_name"] and "save recording as <name>" in r.output and store.list() == []
    r = await tool.run(RecordArgs(action="save", name="daily"))
    assert r.ok and store.get("daily") is not None
    assert not (await tool.run(RecordArgs(action="save", name="again"))).ok  # nothing pending any more
    await tool.run(RecordArgs(action="start"))
    assert "thrown away" in (await tool.run(RecordArgs(action="cancel"))).output and not watch.recording
    assert "no recording" in (await tool.run(RecordArgs(action="cancel"))).output
    assert not (await tool.run(RecordArgs(action="dance"))).ok


async def test_a_recording_with_nothing_usable_makes_no_routine_and_no_browser_is_a_clear_error(store):
    hub, _ctx = fake_hub()
    tool = RecordRoutine(WatchMe(hub, store))
    await tool.run(RecordArgs(action="start"))
    r = await tool.run(RecordArgs(action="stop", name="empty"))
    assert not r.ok and "no routine was made" in r.output and store.list() == []
    none = RecordRoutine(WatchMe(SimpleNamespace(mgr=None), store))
    r = await none.run(RecordArgs(action="start"))
    assert not r.ok and "Delta's own browser" in r.output


# --- commands, the store and running in the right browser ------------------------------------------------------


def test_the_recording_commands_are_the_users_own_words():
    assert routine_command("start recording") == ("record_routine", {"action": "start", "url": ""})
    assert routine_command("watch me") == ("record_routine", {"action": "start", "url": ""})
    assert routine_command("start recording on github.com")[1]["url"] == "github.com"
    assert routine_command("stop recording and save as Time Table") == \
        ("record_routine", {"action": "stop", "name": "Time Table"})
    assert routine_command("stop recording")[1] == {"action": "stop", "name": ""}
    assert routine_command("save recording as daily")[1] == {"action": "save", "name": "daily"}
    assert routine_command("cancel recording")[1] == {"action": "cancel"}
    for text in ("record a video of my screen", "stop the music", "start a meeting", "watch a movie"):
        assert routine_command(text) is None, text


def test_an_older_store_gets_the_browser_column_and_keeps_its_routines(tmp_path):
    path = tmp_path / "routines.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE routines(name TEXT PRIMARY KEY, title TEXT NOT NULL, request TEXT NOT NULL, steps TEXT "
               "NOT NULL, created REAL NOT NULL, runs INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, "
               "last_run REAL)")
    db.execute("INSERT INTO routines(name, title, request, steps, created) VALUES ('old', 'Old', 'r', '[]', 1)")
    db.commit()
    db.close()
    s = RoutineStore(path)
    assert s.get("old").browser == "" and s.save("new", "r", [], browser="private").browser == "private"
    s.close()


async def test_a_recorded_routine_runs_in_deltas_own_browser_and_the_pin_is_released(store):
    from delta.core.state import CURRENT_REQUEST, TaskRequest
    from delta.tools.browser_switch import TASK_BROWSER

    seen = []

    class Executor:
        async def execute(self, tool, args, task_id=""):
            tb = TASK_BROWSER.get()
            seen.append(tb.pick.kind if tb else None)
            return ToolResult(True, "ok", {})

    hub, _ = fake_hub()
    store.save("by hand", "(recorded by hand)", [{"tool": "browser_open", "args": {"url": "https://a.example"}}],
               browser="private")
    store.save("by ai", "open a", [{"tool": "browser_open", "args": {"url": "https://a.example"}}])
    run = RunRoutine(store)
    run.executor, run.hub = Executor(), hub
    token = CURRENT_REQUEST.set(TaskRequest("run", channel="api"))
    try:
        assert (await run.run(NameArgs(name="by hand"))).ok and (await run.run(NameArgs(name="by ai"))).ok
    finally:
        CURRENT_REQUEST.reset(token)
    assert seen == ["private", None] and TASK_BROWSER.get() is None


def test_recording_is_registered_and_is_never_a_step_of_a_routine(tmp_path):
    from delta.bootstrap import build
    from delta.config import Settings

    c = build(Settings(data_dir=tmp_path))
    assert "record_routine" in c.registry.names() and "record_routine" in routines.NEVER
    assert c.registry.get("run_routine").hub is not None
