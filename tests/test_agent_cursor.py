"""The "Delta is in control" signal: on during actions, gone the moment they end, fail or are stopped; the real
Windows pointer is never changed; an older version's orange pointer is still given back after a crash."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from delta.system import agent_cursor
from delta.system.agent_cursor import AgentCursor


class FakeOverlay:
    def __init__(self, on_stop) -> None:
        self.on_stop = on_stop
        self.calls: list[str] = []
        self.beats = 0
        self.captures = 0

    def start(self) -> bool:
        self.calls.append("start")
        return True

    def show(self, label: bool = True) -> None:
        self.calls.append("show")

    def hide(self) -> None:
        self.calls.append("hide")

    def beat(self) -> None:
        self.beats += 1

    def close(self) -> None:
        self.calls.append("close")

    @contextmanager
    def hidden_for_capture(self) -> Iterator[None]:
        self.captures += 1
        yield


@pytest.fixture
def made(monkeypatch):
    monkeypatch.setattr(agent_cursor, "HOLD_S", 0.05)
    box: dict[str, FakeOverlay] = {}

    def factory(on_stop):
        box["o"] = FakeOverlay(on_stop)
        return box["o"]

    return factory, box


def shows(box) -> list[str]:
    return [c for c in box["o"].calls if c in ("show", "hide")]


def test_on_during_actions_and_off_after(tmp_path, made):
    factory, box = made
    c = AgentCursor(tmp_path, overlay_factory=factory)
    with c.active():
        with c.active():  # nested steps: one show, no flicker
            pass
        assert shows(box) == ["show"] and c.on
    time.sleep(0.2)
    assert shows(box) == ["show", "hide"] and not c.on


def test_back_to_back_steps_do_not_flicker(tmp_path, made):
    factory, box = made
    c = AgentCursor(tmp_path, overlay_factory=factory)
    with c.active():
        pass
    with c.active():  # starts within the hold time
        pass
    time.sleep(0.2)
    assert shows(box) == ["show", "hide"]


def test_a_failed_or_cancelled_action_hides_it_at_once(tmp_path, made, monkeypatch):
    factory, box = made
    monkeypatch.setattr(agent_cursor, "HOLD_S", 30)  # no waiting for the hold: it must be immediate
    c = AgentCursor(tmp_path, overlay_factory=factory)
    with pytest.raises(RuntimeError), c.active():
        raise RuntimeError("boom")
    assert shows(box) == ["show", "hide"] and not c.on
    with pytest.raises(asyncio.CancelledError), c.active(), c.active():
        raise asyncio.CancelledError
    assert shows(box) == ["show", "hide", "show", "hide"]


def test_stop_hides_it_whatever_is_still_running(tmp_path, made, monkeypatch):
    factory, box = made
    monkeypatch.setattr(agent_cursor, "HOLD_S", 30)
    c = AgentCursor(tmp_path, overlay_factory=factory)
    c.begin()
    c.begin()
    c.stop()  # task ended or the user pressed Stop
    assert shows(box) == ["show", "hide"] and not c.on
    c.end()  # the late end of the cancelled action changes nothing
    assert shows(box) == ["show", "hide"]


def test_switched_off_never_shows_but_the_stop_keys_stay(tmp_path, made):
    factory, box = made
    stopped: list[str] = []
    c = AgentCursor(tmp_path, enabled=False, overlay_factory=factory)
    c.arm(stopped.append)
    with c.active():
        pass
    assert "show" not in box["o"].calls
    box["o"].on_stop("esc")  # Esc twice still stops the task
    assert stopped == ["esc"]


def test_turning_it_off_while_on_hides_it_and_on_again_shows_it(tmp_path, made):
    factory, box = made
    c = AgentCursor(tmp_path, overlay_factory=factory)
    c.begin()
    c.set_enabled(False)
    assert shows(box)[-1] == "hide"
    c.set_enabled(True)
    assert shows(box)[-1] == "show"
    c.stop()


async def test_heartbeat_from_the_event_loop_while_on(tmp_path, made, monkeypatch):
    factory, box = made
    monkeypatch.setattr(agent_cursor, "BEAT_S", 0.02)
    c = AgentCursor(tmp_path, overlay_factory=factory)
    c.arm(lambda kind: None)
    c.begin()
    await asyncio.sleep(0.15)
    assert box["o"].beats >= 3  # a hung Delta stops beating, and the overlay hides itself
    c.stop()
    n = box["o"].beats
    await asyncio.sleep(0.1)
    assert box["o"].beats <= n + 1  # beats end with the signal


def test_heartbeat_without_an_event_loop_keeps_it_on(tmp_path, made, monkeypatch):
    factory, box = made
    monkeypatch.setattr(agent_cursor, "BEAT_S", 0.02)
    c = AgentCursor(tmp_path, overlay_factory=factory)
    c.begin()
    time.sleep(0.12)
    assert box["o"].beats >= 2
    c.stop()


def test_overlay_that_cannot_start_never_breaks_an_action(tmp_path):
    def broken(on_stop):
        raise OSError("no desktop")

    c = AgentCursor(tmp_path, overlay_factory=broken)
    with c.active():
        pass  # nothing raised
    c.stop()


def test_screenshots_and_browser_find_the_installed_signal(tmp_path, made):
    factory, box = made
    c = AgentCursor(tmp_path, overlay_factory=factory)
    agent_cursor.install(c)
    try:
        with agent_cursor.driving():
            assert c.on
            with agent_cursor.capture_guard():
                pass
        assert box["o"].captures == 1
    finally:
        agent_cursor.install(None)
        c.stop()
    with agent_cursor.driving(), agent_cursor.capture_guard():  # nothing installed: no-ops
        pass


def test_the_real_windows_pointer_is_never_changed():
    src = inspect.getsource(agent_cursor) + inspect.getsource(__import__("delta.system.control_overlay",
                                                                        fromlist=["x"]))
    assert "SetSystemCursor(" not in src and "LoadCursorFromFile" not in src


def test_crash_recovery_of_an_older_versions_orange_pointer(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(agent_cursor, "reset_system_cursors", lambda: calls.append("restore"))
    (tmp_path / "cursor.active").write_text("1")  # an older Delta died while the pointer was orange
    assert AgentCursor.recover(tmp_path) is True and calls == ["restore"]
    assert AgentCursor.recover(tmp_path) is False  # nothing to do the second time


def test_close_hides_and_closes_the_overlay(tmp_path, made):
    factory, box = made
    c = AgentCursor(tmp_path, overlay_factory=factory)
    c.begin()
    c.close()
    assert box["o"].calls[-2:] == ["hide", "close"]
