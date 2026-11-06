"""Remote Guard: a stolen phone or Telegram account must not be able to use the PC's sensitive powers."""

from types import SimpleNamespace

import pytest

from delta.safety.remote_guard import MAX_FAILURES, UNLOCK_S, RemoteGuard, check_pin, hash_pin, valid_pin


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(tmp_path, store=None, clock=None):
    store = {} if store is None else store
    events, alerts = [], []
    audit = SimpleNamespace(record=lambda e, **k: events.append((e, k)))
    g = RemoteGuard(tmp_path, audit, alert=lambda t, x: alerts.append((t, x)), get_secret=store.get,
                    set_secret=store.__setitem__, now=clock or Clock())
    return g, store, events, alerts


def test_the_pin_is_never_stored_only_a_salted_slow_hash():
    h1, h2 = hash_pin("4821"), hash_pin("4821")
    assert "4821" not in h1 and h1 != h2  # salted: the same PIN never gives the same hash
    assert check_pin("4821", h1) and not check_pin("4822", h1) and not check_pin("4821", "garbage")
    assert valid_pin("4821") and not valid_pin("123") and not valid_pin("1111") and not valid_pin("12ab")


def test_sensitive_actions_need_a_pin_and_it_expires(tmp_path):
    clock = Clock()
    g, store, events, _ = make(tmp_path, clock=clock)
    assert not g.has_pin and not g.is_unlocked("5")
    assert "No PIN is set" in g.try_pin("5", "4821")[1]  # secure by default
    g.set_pin("4821", by="dashboard")
    assert "4821" not in str(store) and g.try_pin("5", "4821")[0] and g.is_unlocked("5")
    assert not g.is_unlocked("6")  # per person, not for everyone
    clock.t += UNLOCK_S + 1
    assert not g.is_unlocked("5")  # 15 minutes, then the PIN again
    assert ("guard.unlocked", {"user": "5", "minutes": 15}) in events
    assert all("4821" not in str(e) for e in events)  # never in the audit log


def test_guessing_locks_everything_and_only_the_pc_can_unlock(tmp_path):
    g, store, _, alerts = make(tmp_path)
    g.set_pin("4821", by="dashboard")
    for _ in range(MAX_FAILURES - 1):
        assert not g.try_pin("5", "0000")[0]
    ok, msg = g.try_pin("5", "9999")
    assert not ok and g.locked and "locked" in msg and alerts  # the PC is told
    assert not g.try_pin("5", "4821")[0]  # even the right PIN cannot undo it from the phone
    again, *_ = make(tmp_path, store)  # survives a restart
    assert again.locked
    again.unlock_from_pc("dashboard")
    assert not again.locked and again.try_pin("5", "4821")[0]


def test_a_new_pin_ends_open_unlocks(tmp_path):
    g, *_ = make(tmp_path)
    g.set_pin("4821", by="dashboard")
    g.try_pin("5", "4821")
    g.set_pin("7350", by="dashboard")
    assert not g.is_unlocked("5")


def test_wrong_pins_are_remembered_across_a_restart(tmp_path):
    """A restart (or a crash, or a reboot) must not hand out a fresh set of guesses (P6)."""
    g, store, _, _ = make(tmp_path)
    g.set_pin("4821", by="dashboard")
    for _ in range(MAX_FAILURES - 1):
        assert not g.try_pin("5", "0000")[0]
    restarted, *_ = make(tmp_path, store)
    assert restarted.failures == MAX_FAILURES - 1 and not restarted.locked
    ok, msg = restarted.try_pin("5", "9999")
    assert not ok and restarted.locked and "locked" in msg


def test_waiting_does_not_reset_the_count_but_a_right_pin_does(tmp_path):
    clock = Clock()
    g, *_ = make(tmp_path, clock=clock)
    g.set_pin("4821", by="dashboard")
    for _ in range(MAX_FAILURES - 1):
        g.try_pin("5", "0000")
        clock.t += 24 * 3600  # one guess a day: still counted
    assert g.failures == MAX_FAILURES - 1
    assert g.try_pin("5", "4821")[0] and g.failures == 0  # the owner got it right: count starts again
    g.try_pin("5", "0000")
    again, *_ = make(tmp_path, {"remote_pin": g._get("remote_pin")})
    assert again.failures == 1
    again.lock("test", by="dashboard")
    again.unlock_from_pc("dashboard")
    assert again.failures == 0 and make(tmp_path)[0].failures == 0


def test_the_state_file_is_written_atomically_and_old_files_still_load(tmp_path):
    import json

    (tmp_path / "remote_guard.json").write_text(json.dumps({"locked": False, "reason": ""}))  # before the count
    g, *_ = make(tmp_path)
    assert g.failures == 0 and not g.locked
    g.set_pin("4821", by="dashboard")
    g.try_pin("5", "0000")
    assert json.loads((tmp_path / "remote_guard.json").read_text())["failures"] == 1
    assert [p.name for p in tmp_path.iterdir()] == ["remote_guard.json"]  # no temporary file left behind
    (tmp_path / "remote_guard.json").write_text('{"locked": false, "failures": "lots"}')
    assert make(tmp_path)[0].failures == 0  # a damaged count never crashes start-up


# --- in Telegram ---------------------------------------------------------------------------------------
telegram = pytest.importorskip("telegram")


def channel(tmp_path):
    from delta.channels.telegram import TelegramChannel

    g, *_ = make(tmp_path)
    c = SimpleNamespace(audit=g.audit, guard=g)
    return TelegramChannel(c, "123456:TEST-TOKEN", ["5"]), g  # type: ignore[arg-type]


class Chat:
    def __init__(self):
        self.said = []

    async def send_message(self, text, **kw):
        self.said.append(text)


def message(text, chat, deleted):
    async def reply_text(t, **kw):
        chat.said.append(t)

    async def delete():
        deleted.append(text)

    return SimpleNamespace(text=text, chat=chat, reply_text=reply_text, delete=delete)


async def test_camera_from_the_phone_asks_for_the_pin_then_runs(tmp_path, monkeypatch):
    from delta.system import notify

    monkeypatch.setattr(notify, "notify", lambda *a: None)
    ch, g = channel(tmp_path)
    chat, deleted, ran = Chat(), [], []

    async def shoot():
        ran.append("camera")

    await ch._guarded("5", "camera", chat.send_message, shoot)
    assert ran == [] and "set a remote PIN on the PC" in chat.said[-1]  # no PIN: refused

    g.set_pin("4821", by="dashboard")
    await ch._guarded("5", "camera", chat.send_message, shoot)
    assert ran == [] and "Enter your Delta PIN" in chat.said[-1]
    assert await ch._take_pin(SimpleNamespace(message=message("1234", chat, deleted)), "5")
    assert ran == [] and "Wrong PIN" in chat.said[-1]
    assert await ch._take_pin(SimpleNamespace(message=message("4821", chat, deleted)), "5")
    assert ran == ["camera"] and deleted == ["1234", "4821"]  # no PIN stays in the chat
    await ch._guarded("5", "camera", chat.send_message, shoot)
    assert ran == ["camera", "camera"]  # unlocked for 15 minutes: no PIN again


async def test_lock_from_the_phone_blocks_everything(tmp_path):
    ch, g = channel(tmp_path)
    chat = Chat()
    lock = SimpleNamespace(effective_user=SimpleNamespace(id=5), message=message("/lock", chat, []))
    await ch._lock_cmd(lock, None)
    assert g.locked and "Only someone at the PC" in chat.said[-1]
    assert await ch._locked(chat.send_message) and "locked" in chat.said[-1]
    assert not ch._ok("5")  # /volume, /apps... stop answering too


async def test_send_file_needs_an_unlocked_owner(tmp_path):
    from delta.core.state import CURRENT_REQUEST, TaskRequest
    from delta.tools.deliver import SendFile, SendFileArgs

    g, *_ = make(tmp_path)
    g.set_pin("4821", by="dashboard")
    sent = []
    tool = SendFile({"telegram": SimpleNamespace(send_file=lambda *a: sent.append(a))})
    tool.guard = g
    f = tmp_path / "salary.pdf"
    f.write_bytes(b"x")
    token = CURRENT_REQUEST.set(TaskRequest("send me salary.pdf", channel="telegram", user="5"))
    try:
        r = await tool.run(SendFileArgs(path=str(f)))
    finally:
        CURRENT_REQUEST.reset(token)
    assert not r.ok and r.evidence["needs_pin"] and "/unlock" in r.output and sent == []
