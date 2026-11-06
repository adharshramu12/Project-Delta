"""Stop anytime: Stop / "stop" / "/stop all" / Esc twice / Ctrl+Alt+Shift+S cancel cleanly, from any channel.

Uses a real Container (its submit, its one-task-at-a-time lock) with a stand-in orchestrator."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from delta.bootstrap import Container
from delta.control import stop_command, stop_running, stop_task
from delta.core.state import TaskRequest, TaskResult
from delta.system import agent_cursor, input_stop
from delta.system.agent_cursor import AgentCursor


class SlowOrchestrator:
    """Runs a task until it is cancelled (or released); drives "input" while it runs."""

    def __init__(self, signal: AgentCursor | None = None) -> None:
        self.started: list[str] = []
        self.release = asyncio.Event()
        self.signal = signal
        self.stop_flag_at_start: list[bool] = []

    async def run(self, req: TaskRequest) -> TaskResult:
        self.started.append(req.id)
        self.stop_flag_at_start.append(input_stop.requested())
        if self.signal is not None:
            self.signal.begin()  # an action is driving the mouse
        await self.release.wait()
        if self.signal is not None:
            self.signal.end()
        return TaskResult(req.id, "done", "ok")


class Overlay:
    def __init__(self, on_stop) -> None:
        self.on_stop, self.visible = on_stop, False

    def start(self) -> bool:
        return True

    def show(self, label: bool = True) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False

    def beat(self) -> None:
        pass

    def close(self) -> None:
        pass


def container(signal: AgentCursor | None = None) -> tuple[Container, SlowOrchestrator, list[Any]]:
    audit: list[Any] = []
    orch = SlowOrchestrator(signal)
    c = Container(settings=SimpleNamespace(), events=None, governor=None, router=None, memory=None,  # type: ignore[arg-type]
                  audit=SimpleNamespace(record=lambda *a, **k: audit.append((a, k))), approvals=None,  # type: ignore[arg-type]
                  registry=None, orchestrator=orch, lifecycle=None,  # type: ignore[arg-type]
                  _task_lock=asyncio.Semaphore(1), signal=signal)
    return c, orch, audit


@pytest.fixture
def signal(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_cursor, "HOLD_S", 30)  # anything that hides it must do so at once
    box: dict[str, Overlay] = {}

    def factory(on_stop):
        box["o"] = Overlay(on_stop)
        return box["o"]

    s = AgentCursor(tmp_path, overlay_factory=factory)
    yield s, box
    s.close()
    input_stop.reset()


async def until(cond, within: float = 2.0) -> None:
    end = asyncio.get_running_loop().time() + within
    while not cond():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


def test_stop_words_from_any_channel():
    c, _, _ = container()
    assert stop_command(c, "stop", "t") == "Nothing is running right now."
    assert stop_command(c, " /STOP all! ", "t") == "Nothing is running right now."
    assert stop_command(c, "stop everything", "t") is not None
    for not_a_stop in ("stop the music", "can you stop", "stopwatch", "/stopper", ""):
        assert stop_command(c, not_a_stop, "t") is None  # an ordinary request: handled as usual


async def test_stop_cancels_the_running_task_lets_go_and_hides_the_ring(signal):
    s, box = signal
    c, _orch, audit = container(s)
    t = asyncio.create_task(c.submit(TaskRequest("click around", id="a")))
    await until(lambda: box.get("o") is not None and box["o"].visible)
    assert stop_running(c, by="dashboard")
    assert not box["o"].visible and input_stop.requested()  # gone at once, no more key presses
    with pytest.raises(asyncio.CancelledError):
        await t
    assert c.running is None and c.active == {}
    assert any(a[0] == ("task.cancel_requested",) for a in audit)


async def test_stop_all_cancels_the_running_task_and_every_waiting_one(signal):
    s, _ = signal
    c, orch, _ = container(s)
    tasks = [asyncio.create_task(c.submit(TaskRequest(f"job {i}", id=f"t{i}"))) for i in range(3)]
    await until(lambda: len(c.active) == 3 and orch.started == ["t0"])
    assert stop_command(c, "/stop all", "telegram:5") == "⏹ Stopped 3 tasks."
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, asyncio.CancelledError) for r in results)
    assert orch.started == ["t0"] and c.active == {}  # the waiting ones never started


async def test_stop_one_lets_the_next_task_run_with_a_clean_slate(signal):
    s, _ = signal
    c, orch, _ = container(s)
    first = asyncio.create_task(c.submit(TaskRequest("first", id="f")))
    second = asyncio.create_task(c.submit(TaskRequest("second", id="s")))
    await until(lambda: orch.started == ["f"])
    assert stop_command(c, "stop", "slack:U1") == "⏹ Stopped."
    with pytest.raises(asyncio.CancelledError):
        await first
    await until(lambda: orch.started == ["f", "s"])
    assert orch.stop_flag_at_start == [False, False]  # the earlier stop does not block the next task's keys
    orch.release.set()
    assert (await second).status == "done"


async def test_a_waiting_task_can_be_stopped_by_its_id(signal):
    s, _ = signal
    c, orch, _ = container(s)
    first = asyncio.create_task(c.submit(TaskRequest("first", id="f")))
    queued = asyncio.create_task(c.submit(TaskRequest("queued", id="q")))
    await until(lambda: "q" in c.active)
    assert stop_task(c, "q", by="dashboard") and not stop_task(c, "nope", by="dashboard")
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert not input_stop.requested()  # the running task was not touched
    orch.release.set()
    assert (await first).status == "done"


async def test_the_ring_never_outlives_its_task(signal):
    s, box = signal
    c, orch, _ = container(s)
    orch.release.set()

    async def run_and_leave_it_on(req: TaskRequest) -> TaskResult:
        s.begin()  # an action that forgot to end (or ended inside its hold time)
        return TaskResult(req.id, "done", "ok")

    orch.run = run_and_leave_it_on  # type: ignore[method-assign]
    await c.submit(TaskRequest("x", id="x"))
    assert not box["o"].visible and not s.on


async def test_esc_twice_or_the_hotkey_stops_the_running_task(signal):
    s, box = signal
    c, _orch, _ = container(s)
    t = asyncio.create_task(c.submit(TaskRequest("long job", id="h")))
    await until(lambda: box.get("o") is not None and box["o"].visible)
    threading.Thread(target=box["o"].on_stop, args=("esc",)).start()  # the overlay thread calls back
    with pytest.raises(asyncio.CancelledError):
        await t
    assert not box["o"].visible


def test_stop_lets_go_of_keys_delta_holds(monkeypatch):
    sent: list[tuple] = []
    platform = SimpleNamespace(
        _keyDown=lambda k: sent.append(("down", k)), _keyUp=lambda k: sent.append(("up", k)),
        _mouseDown=lambda x, y, b: sent.append(("mdown", b)), _mouseUp=lambda x, y, b: sent.append(("mup", b)))
    monkeypatch.setattr(input_stop, "_originals", {})
    monkeypatch.setattr(input_stop, "_held_keys", [])
    monkeypatch.setattr(input_stop, "_held_buttons", {})
    input_stop.track_pyautogui(SimpleNamespace(platformModule=platform))
    platform._keyDown("ctrl")
    platform._mouseDown(5, 5, "left")
    c, _, _ = container()
    c.running = SimpleNamespace(done=lambda: False, cancel=lambda: True)  # type: ignore[assignment]
    assert stop_running(c, by="test")
    assert ("up", "ctrl") in sent and ("mup", "left") in sent
    input_stop.reset()


def test_dashboard_stop_endpoints(tmp_path, monkeypatch):
    from engine_helpers import no_registry
    from fastapi.testclient import TestClient

    from delta.api.server import create_app, load_token
    from delta.bootstrap import build
    from delta.config import Settings

    no_registry(monkeypatch, tmp_path)
    c = build(Settings(data_dir=tmp_path))
    token = load_token(tmp_path)
    with TestClient(create_app(c, token)) as tc:
        tc.headers["Authorization"] = f"Bearer {token}"
        assert tc.post("/api/tasks/stop-all").json() == {"stopped": 0}
        assert tc.post("/api/tasks/nope/cancel").json() == {"ok": False}
        assert tc.post("/api/tasks/stop-all", headers={"Authorization": "Bearer wrong"}).status_code == 401


async def test_telegram_stop_all_and_a_plain_stop_message(tmp_path):
    pytest.importorskip("telegram")
    from delta.channels.telegram import TelegramChannel

    c, orch, _ = container()
    ch = TelegramChannel(c, "123456:TEST-TOKEN", ["5"])  # type: ignore[arg-type]
    said: list[str] = []

    async def reply(text, **_):
        said.append(text)

    def update(text):
        return SimpleNamespace(effective_user=SimpleNamespace(id=5),
                               message=SimpleNamespace(text=text, reply_text=reply))

    tasks = [asyncio.create_task(c.submit(TaskRequest(f"job {i}", id=f"t{i}"))) for i in range(2)]
    await until(lambda: len(c.active) == 2)
    await ch._stop_cmd(update("/stop all"), SimpleNamespace(args=["all"]))
    assert said[-1] == "⏹ Stopped 2 tasks."
    await asyncio.gather(*tasks, return_exceptions=True)
    t = asyncio.create_task(c.submit(TaskRequest("job", id="t9")))
    await until(lambda: c.running is not None)
    await ch._on_message(update("Stop"), None)  # a plain "stop" works like /stop, never queued as a task
    assert said[-1] == "⏹ Stopped." and orch.started[-1] == "t9"
    with pytest.raises(asyncio.CancelledError):
        await t
    await ch._stop_cmd(update("/stop"), SimpleNamespace(args=[]))
    assert said[-1] == "Nothing is running right now."
