"""Orange pointer while Delta acts: shown during actions, always restored."""

import struct
import time

import pytest

from delta.system import agent_cursor
from delta.system.agent_cursor import AgentCursor, make_orange_cursor


@pytest.fixture
def calls(monkeypatch):
    log: list[str] = []
    monkeypatch.setattr(AgentCursor, "_apply", lambda self: (log.append("orange"), setattr(self, "_on", True),
                                                             self.marker.write_text("1")))
    monkeypatch.setattr(agent_cursor, "reset_system_cursors", lambda: log.append("restore"))
    monkeypatch.setattr(agent_cursor, "HOLD_S", 0.05)
    return log


def test_cursor_file_is_a_valid_cur_with_tip_hotspot():
    data = make_orange_cursor()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    hot_x, hot_y = struct.unpack_from("<HH", data, 6 + 4)
    assert (reserved, kind, count) == (0, 2, 1) and (hot_x, hot_y) == (1, 1)


def test_orange_during_actions_and_restored_after(tmp_path, calls):
    c = AgentCursor(tmp_path)
    with c.active():
        with c.active():  # nested steps: one switch, no flicker
            pass
        assert calls == ["orange"]
    time.sleep(0.15)
    assert calls == ["orange", "restore"] and not c.marker.exists()


def test_back_to_back_steps_do_not_flicker(tmp_path, calls):
    c = AgentCursor(tmp_path)
    with c.active():
        pass
    with c.active():  # starts within the hold time
        pass
    time.sleep(0.15)
    assert calls == ["orange", "restore"]


def test_restored_even_if_the_action_fails(tmp_path, calls):
    c = AgentCursor(tmp_path)
    with pytest.raises(RuntimeError), c.active():
        raise RuntimeError("boom")
    time.sleep(0.15)
    assert calls[-1] == "restore"


def test_crash_recovery_on_next_start(tmp_path, calls):
    (tmp_path / "cursor.active").write_text("1")  # a previous run died while orange
    assert AgentCursor.recover(tmp_path) is True and calls == ["restore"]
    assert AgentCursor.recover(tmp_path) is False  # nothing to do the second time


def test_disabled_never_changes_the_cursor(tmp_path, calls):
    c = AgentCursor(tmp_path, enabled=False)
    with c.active():
        pass
    assert calls == []
