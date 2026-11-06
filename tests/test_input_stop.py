"""Stop means stop: no key or click after a stop, everything Delta held down is let go, Esc twice is detected."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from delta.system import input_stop
from delta.system.input_stop import DoubleEsc, StopRequestedError


@pytest.fixture
def fake_pg(monkeypatch):
    """A pyautogui stand-in whose Windows layer records what reaches the OS."""
    sent: list[tuple] = []
    platform = SimpleNamespace(
        _keyDown=lambda k: sent.append(("down", k)), _keyUp=lambda k: sent.append(("up", k)),
        _mouseDown=lambda x, y, b: sent.append(("mdown", b)), _mouseUp=lambda x, y, b: sent.append(("mup", b)),
        _click=lambda x, y, b: sent.append(("click", b)))
    monkeypatch.setattr(input_stop, "_originals", {})
    monkeypatch.setattr(input_stop, "_held_keys", [])
    monkeypatch.setattr(input_stop, "_held_buttons", {})
    input_stop.reset()
    pg = SimpleNamespace(platformModule=platform)
    input_stop.track_pyautogui(pg)
    yield platform, sent
    input_stop.reset()


def test_tracks_what_delta_holds_and_lets_go_on_stop(fake_pg):
    platform, sent = fake_pg
    platform._keyDown("ctrl")
    platform._keyDown("shift")
    platform._mouseDown(10, 20, "left")
    platform._keyUp("shift")
    assert input_stop.held() == (["ctrl"], ["left"])
    released = input_stop.release_held()
    assert released == ["ctrl", "mouse left"]
    assert sent[-2:] == [("up", "ctrl"), ("mup", "left")]
    assert input_stop.held() == ([], [])
    assert input_stop.release_held() == []  # twice is harmless


def test_after_a_stop_no_more_presses_but_letting_go_still_works(fake_pg):
    platform, sent = fake_pg
    platform._keyDown("alt")
    input_stop.request()
    with pytest.raises(StopRequestedError):
        platform._keyDown("f4")
    with pytest.raises(StopRequestedError):
        platform._click(1, 1, "left")
    platform._keyUp("alt")  # a key-up is never blocked
    assert ("down", "f4") not in sent and ("click", "left") not in sent and sent[-1] == ("up", "alt")
    input_stop.reset()  # the next task
    platform._click(1, 1, "left")
    assert sent[-1] == ("click", "left")


def test_tracking_is_installed_once_and_leaves_an_unexpected_pyautogui_alone(fake_pg):
    platform, _ = fake_pg
    wrapped = platform._keyDown
    input_stop.track_pyautogui(SimpleNamespace(platformModule=platform))
    assert platform._keyDown is wrapped
    odd = SimpleNamespace(_keyDown=print)  # missing functions: untouched
    input_stop.track_pyautogui(SimpleNamespace(platformModule=odd))
    assert odd._keyDown is print


def test_check_between_typed_characters():
    input_stop.reset()
    input_stop.check()
    input_stop.request()
    with pytest.raises(StopRequestedError, match=r"^STOPPED: "):  # the self-heal never retries a STOPPED step
        input_stop.check()
    input_stop.reset()


def test_esc_twice_within_one_second():
    now = [0.0]
    esc = DoubleEsc(1.0, clock=lambda: now[0])
    assert not esc.down()
    esc.up()
    now[0] = 0.6
    assert esc.down()  # second press within 1 s
    esc.up()
    now[0] = 0.9
    assert not esc.down()  # starts a new pair, not a third trigger
    esc.up()
    now[0] = 2.5
    assert not esc.down()  # too slow
    esc.up()


def test_holding_esc_is_not_two_presses():
    now = [0.0]
    esc = DoubleEsc(1.0, clock=lambda: now[0])
    assert not esc.down()
    now[0] = 0.3
    assert not esc.down()  # key repeat while held
    now[0] = 0.5
    esc.up()
    now[0] = 0.7
    assert esc.down()
