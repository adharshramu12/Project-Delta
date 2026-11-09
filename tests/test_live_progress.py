"""Live progress: plain-words step labels, the Telegram status message, the queue notice and the task time limit."""

import asyncio
from types import SimpleNamespace

from delta.channels import live_status
from delta.channels.live_status import LiveStatus
from delta.core import activity
from delta.core.events import EventBus
from delta.core.state import TaskRequest, TaskResult


def test_steps_get_plain_labels():
    activity.APP_NAMES["google_drive"] = "Google Drive"
    assert activity.describe("use_app_tool", {"tool": "notion__notion-search"}) == "Working in Notion: search"
    label = activity.describe("use_app_tool", {"tool": "google_drive__search_files"})
    assert label == "Working in Google Drive: search files"
    assert activity.describe("browser_open", {"url": "https://www.youtube.com/watch?v=1"}) == "Opening youtube.com"
    assert activity.describe("read_file", {}) == "Reading a file"
    assert len(activity.describe("web_search", {"query": "x" * 500})) <= activity.MAX_LABEL


async def test_the_bus_labels_steps_and_remembers_the_last_one():
    bus, seen = EventBus(), []

    async def h(event, data):
        seen.append(data)

    bus.subscribe(h)
    await bus.publish("task.step", task_id="t1", tool="use_app_tool", args={"tool": "kite__get_holdings"})
    assert seen[0]["label"].startswith("Working in ") and activity.LAST["t1"] == seen[0]["label"]
    bus.unsubscribe(h)
    await bus.publish("task.step", task_id="t1", tool="read_file", args={})
    assert len(seen) == 1  # unsubscribed handlers hear nothing more


async def test_telegram_status_appears_for_a_long_task_is_edited_and_says_how_it_ended(monkeypatch):
    monkeypatch.setattr(live_status, "SHOW_AFTER_S", 0.05)
    monkeypatch.setattr(live_status, "EDIT_EVERY_S", 0)
    sent, edits = [], []

    async def send(text):
        sent.append(text)
        return "msg"

    async def edit(handle, text):
        edits.append(text)

    s = LiveStatus("t1", send, edit)
    s.start()
    await asyncio.sleep(0.12)
    assert sent and sent[0].startswith("⏳ Working")
    await s.on_event("task.step", {"task_id": "t1", "label": "Working in Notion: search"})
    await s.on_event("task.step", {"task_id": "other", "label": "not mine"})
    assert "Now: Working in Notion: search" in edits[-1] and "1 step" in edits[-1]
    await s.finish("failed")
    assert edits[-1].startswith("❌ Could not finish")


async def test_a_quick_task_sends_no_status_message():
    sent = []

    async def send(text):
        sent.append(text)

    s = LiveStatus("t1", send, lambda h, t: asyncio.sleep(0))
    s.start()
    await s.finish("done")
    assert sent == []


def _container(run, limit):
    from delta.bootstrap import Container

    bus, events = EventBus(), []

    async def h(event, data):
        events.append(event)

    bus.subscribe(h)
    c = Container(settings=SimpleNamespace(agent=SimpleNamespace(task_timeout_s=limit)), events=bus,  # type: ignore[arg-type]
                  governor=None, router=None, memory=None, audit=SimpleNamespace(record=lambda *a, **k: None),  # type: ignore[arg-type]
                  approvals=None, registry=None, orchestrator=SimpleNamespace(run=run),  # type: ignore[arg-type]
                  lifecycle=None, _task_lock=asyncio.Semaphore(1))  # type: ignore[arg-type]
    return c, bus, events


async def test_a_task_that_never_ends_is_stopped_and_says_where_it_was():
    async def run(req):
        await c.events.publish("task.step", task_id=req.id, tool="use_app_tool", args={"tool": "notion__notion-search"})
        await asyncio.sleep(60)

    c, _, events = _container(run, 0.2)
    r = await c.submit(TaskRequest("find my notes", id="slow"))
    assert r.status == "failed" and "Notion: search" in r.answer and "task.finished" in events


async def test_a_waiting_request_is_announced():
    gate = asyncio.Event()

    async def run(req):
        await gate.wait()
        return TaskResult(req.id, "done", "ok")

    c, _, events = _container(run, 0)
    first = asyncio.create_task(c.submit(TaskRequest("one", id="a")))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(c.submit(TaskRequest("two", id="b")))
    await asyncio.sleep(0.05)
    assert "task.queued" in events
    gate.set()
    assert (await first).status == (await second).status == "done"
