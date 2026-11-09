"""Telegram keeps working through slow or flaky networks, and the dashboard says so only for a real outage.

Reported on a real PC: "Telegram sometimes stops and a 'timed out' banner appears". Causes found and covered here:
- python-telegram-bot's default timeouts are 5 s for every call (connect, read, write) and 1 s to get a connection.
  On a slow mobile or Wi-Fi link a reply, a photo download or the "typing..." signal timed out, the handler raised,
  and the crash guard put "Something went wrong in Telegram (TimedOut: Timed out)" on the dashboard for good;
- a "typing..." signal that timed out at the start of a request stopped the request itself (it ran before submit);
- a reply that timed out was lost (no retry), so the phone never got the answer;
- Delta could not start Telegram for a moment (network not up yet after boot or sleep) and showed "Telegram could
  not start: Timed out" right away, although the retry a few seconds later worked;
- nothing watched the poll loop after start: if it ended (a bad token, an unexpected error) the bot went silent with
  no sign; two copies of Delta on one bot (Conflict 409) only filled the log;
- a long task showed "typing..." for 5 seconds and then nothing for minutes.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("telegram")

from telegram.error import BadRequest, Conflict, InvalidToken, NetworkError, RetryAfter, TimedOut

from delta.channels import telegram_net as net
from delta.channels.telegram import TelegramChannel
from delta.core.state import TaskResult


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


# ---- health of the poll loop --------------------------------------------------------------------------------------
def test_a_short_blip_is_not_an_outage_but_a_sustained_one_is():
    clock = Clock()
    h = net.PollHealth(clock)
    h.failed(TimedOut())
    clock.t += net.OUTAGE_S - 5
    h.failed(NetworkError("httpx.ConnectError"))
    assert h.outage() is None
    clock.t += 10
    msg = h.outage()
    assert msg is not None and "can't reach Telegram" in msg and "keeps trying" in msg
    assert "Timed out" not in msg  # plain words, not the exception name
    h.ok()
    assert h.outage() is None and h.failures == 0


def test_two_copies_on_one_bot_and_a_bad_token_get_their_own_words():
    clock = Clock()
    h = net.PollHealth(clock)
    h.failed(Conflict("terminated by other getUpdates request"))
    clock.t += net.CONFLICT_S + 1
    h.failed(Conflict("terminated by other getUpdates request"))
    assert "another copy of delta" in (h.outage() or "").lower()
    h = net.PollHealth(clock)
    h.failed(InvalidToken())
    assert "token" in (h.outage() or "").lower()  # a bad token never fixes itself: said at once


def test_stalled_means_no_good_poll_for_a_long_time():
    clock = Clock()
    h = net.PollHealth(clock)
    h.ok()
    clock.t += net.STALL_S - 1
    assert not h.stalled()
    clock.t += 2
    assert h.stalled()


async def test_the_poll_request_reports_every_answer_to_the_health(monkeypatch):
    from telegram.request import HTTPXRequest

    h = net.PollHealth(Clock())
    answers: list[Any] = [(200, b"{}"), (409, b"{}"), NetworkError("down")]

    async def fake(self, *a: Any, **k: Any) -> Any:
        r = answers.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(HTTPXRequest, "do_request", fake)
    req = net.poll_request(h)
    assert await req.do_request("u", "POST") == (200, b"{}") and h.failures == 0 and h.last_ok
    await req.do_request("u", "POST")
    assert h.failures == 1 and h.conflict_since is not None
    with pytest.raises(NetworkError):
        await req.do_request("u", "POST")
    assert h.failures == 2


# ---- sending ------------------------------------------------------------------------------------------------------
async def test_a_reply_that_times_out_is_sent_again(monkeypatch):
    monkeypatch.setattr(net, "BACKOFF_S", (0, 0, 0))
    calls = {"n": 0}

    async def send() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimedOut() if calls["n"] == 1 else NetworkError("connection reset")
        return "ok"

    assert await net.resilient(send) == "ok" and calls["n"] == 3


async def test_flood_control_waits_as_asked_and_bad_requests_are_not_retried(monkeypatch):
    slept: list[float] = []

    async def nap(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(net.asyncio, "sleep", nap)
    calls = {"n": 0}

    async def send() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RetryAfter(dt.timedelta(seconds=3))
        return "ok"

    assert await net.resilient(send) == "ok" and slept and slept[0] >= 3

    async def bad() -> None:
        raise BadRequest("can't parse entities")

    with pytest.raises(BadRequest):
        await net.resilient(bad)


async def test_retries_give_up_after_a_few_tries(monkeypatch):
    monkeypatch.setattr(net, "BACKOFF_S", (0, 0, 0))

    async def down() -> None:
        raise NetworkError("down")

    with pytest.raises(NetworkError):
        await net.resilient(down)


async def test_typing_signal_never_stops_a_request():
    class Chat:
        async def send_action(self, action: str) -> None:
            raise TimedOut()

    class Slow:
        async def send_action(self, action: str) -> None:
            await asyncio.sleep(30)

    await net.safe_action(Chat())
    await asyncio.wait_for(net.safe_action(Slow(), limit_s=0.05), 1)


async def test_a_long_task_says_still_working_once_and_keeps_typing():
    actions: list[str] = []
    said: list[str] = []

    class Chat:
        async def send_action(self, action: str) -> None:
            actions.append(action)

    msg = SimpleNamespace(chat=Chat())

    async def reply(text: str, **kw: Any) -> None:
        said.append(text)

    msg.reply_text = reply
    async with net.working(msg, still_after=0.05, typing_every=0.02):
        await asyncio.sleep(0.2)
    n = len(actions)
    await asyncio.sleep(0.1)
    assert len(actions) == n  # stopped with the task
    assert n >= 3 and len(said) == 1 and "Still working" in said[0]


async def test_a_quick_task_says_nothing_extra():
    said: list[str] = []

    class Chat:
        async def send_action(self, action: str) -> None:
            return None

    msg = SimpleNamespace(chat=Chat())

    async def reply(text: str, **kw: Any) -> None:
        said.append(text)

    msg.reply_text = reply
    async with net.working(msg, still_after=5):
        await asyncio.sleep(0.01)
    assert said == []


# ---- the channel --------------------------------------------------------------------------------------------------
class FlakyChat:
    def __init__(self) -> None:
        self.actions = 0

    async def send_action(self, action: str) -> None:
        self.actions += 1
        raise TimedOut()

    async def send_message(self, text: str, **kw: Any) -> None:
        return None


class Msg:
    def __init__(self, fail_replies: int = 0) -> None:
        self.chat = FlakyChat()
        self.replies: list[str] = []
        self.fail = fail_replies
        self.message_id = 7

    async def reply_text(self, text: str, **kw: Any) -> None:
        if self.fail:
            self.fail -= 1
            raise TimedOut()
        self.replies.append(text)


def channel(tmp_path: Path) -> tuple[TelegramChannel, SimpleNamespace, list[Any]]:
    from delta.memory.chats import ChatStore

    submitted: list[Any] = []

    async def submit(req: Any) -> TaskResult:
        submitted.append(req)
        return TaskResult(req.id, "done", "Your answer is 42.")

    c = SimpleNamespace(audit=SimpleNamespace(record=lambda *a, **k: None), warnings=[],
                        settings=SimpleNamespace(agent=SimpleNamespace(name="Delta"), data_dir=tmp_path),
                        orchestrator=SimpleNamespace(attachments=None), chats=ChatStore(tmp_path / "chats.db"),
                        guard=SimpleNamespace(locked=False), submit=submit)
    c.warn = lambda m: c.warnings.append(m) if m not in c.warnings else None
    return TelegramChannel(c, "123456:TEST-TOKEN", ["42"]), c, submitted  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    import delta.system.notify as notify

    monkeypatch.setattr(notify, "notify", lambda *a, **k: None)
    monkeypatch.setattr(net, "BACKOFF_S", (0, 0, 0))


async def test_a_typing_timeout_no_longer_drops_the_request_and_a_lost_reply_is_resent(tmp_path):
    ch, c, submitted = channel(tmp_path)
    msg = Msg(fail_replies=1)
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=42), message=msg, callback_query=None)
    await ch._run(upd, "42", "what is 6 x 7?")
    assert len(submitted) == 1 and msg.replies and "42" in msg.replies[-1]
    assert c.warnings == []


async def test_a_network_error_in_a_handler_is_not_a_crash_banner(tmp_path):
    ch, c, _ = channel(tmp_path)
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=42), effective_message=Msg())
    await ch._on_error(upd, SimpleNamespace(error=TimedOut()))
    await ch._on_error(upd, SimpleNamespace(error=NetworkError("httpx.ReadError")))
    assert c.warnings == []
    await ch._on_error(upd, SimpleNamespace(error=ValueError("a real bug")))
    assert len(c.warnings) == 1 and "ValueError" in c.warnings[0]  # real bugs still show


def test_timeouts_fit_a_slow_network(tmp_path):
    ch, _, _ = channel(tmp_path)
    poll, main = ch.app.bot._request
    assert main._client.timeout.read >= 20 and main._client.timeout.connect >= 10
    assert main._client.timeout.pool >= 5 and poll._client.timeout.connect >= 10
    assert isinstance(poll, net.TrackingRequest)


class FakeUpdater:
    def __init__(self, running: bool = True) -> None:
        self.running = running
        self.starts: list[dict[str, Any]] = []
        self.stops = 0

    async def start_polling(self, **kw: Any) -> None:
        self.starts.append(kw)
        self.running = True

    async def stop(self) -> None:
        self.stops += 1
        self.running = False


async def test_the_watchdog_restarts_a_dead_poll_loop_without_dropping_messages(tmp_path):
    ch, _c, _ = channel(tmp_path)
    up = FakeUpdater(running=False)
    ch.app = SimpleNamespace(updater=up, bot=ch.app.bot)
    await ch._watch_once()
    assert len(up.starts) == 1 and up.starts[0].get("drop_pending_updates") is False
    assert up.starts[0]["timeout"] == net.POLL_TIMEOUT_S


async def test_the_watchdog_restarts_a_stalled_loop_with_backoff(tmp_path):
    clock = Clock()
    ch, _c, _ = channel(tmp_path)
    ch.health = net.PollHealth(clock)
    ch.health.ok()
    up = FakeUpdater(running=True)
    ch.app = SimpleNamespace(updater=up, bot=ch.app.bot)
    ch._clock = clock
    clock.t += net.STALL_S + 1
    await ch._watch_once()
    assert up.stops == 1 and len(up.starts) == 1
    await ch._watch_once()  # right away again: waits for the backoff
    assert len(up.starts) == 1


async def test_the_banner_shows_only_for_a_sustained_outage_and_goes_away_when_it_ends(tmp_path):
    clock = Clock()
    ch, c, _ = channel(tmp_path)
    ch.health = net.PollHealth(clock)
    ch._clock = clock
    ch.app = SimpleNamespace(updater=FakeUpdater(), bot=ch.app.bot)
    ch.health.failed(TimedOut())
    await ch._watch_once()
    assert c.warnings == []
    clock.t += net.OUTAGE_S + 1
    ch.health.failed(TimedOut())
    await ch._watch_once()
    assert len(c.warnings) == 1 and "Telegram" in c.warnings[0]
    ch.health.ok()
    await ch._watch_once()
    assert c.warnings == []


# ---- starting -----------------------------------------------------------------------------------------------------
async def test_a_slow_start_shows_no_banner_unless_it_keeps_failing(monkeypatch):
    from delta import daemon

    starts = {"n": 0}

    class Flaky:
        def __init__(self, fails: int) -> None:
            self.fails = fails

        async def start(self) -> None:
            starts["n"] += 1
            if starts["n"] <= self.fails:
                raise TimedOut()

        async def stop(self) -> None:
            pass

    def container() -> SimpleNamespace:
        c = SimpleNamespace(settings=SimpleNamespace(channels={"telegram": SimpleNamespace(enabled=True)}),
                            warnings=[], live_channels={},
                            lifecycle=SimpleNamespace(on_shutdown=lambda name, fn: None))
        c.warn = lambda m: c.warnings.append(m)
        return c

    monkeypatch.setattr(daemon, "RETRY_DELAYS_S", (0,))
    c = container()
    monkeypatch.setattr(daemon, "CHANNELS", {"telegram": (lambda _c: Flaky(1), "channels")})
    assert await daemon._start_channels(c) == [] and c.warnings == []  # one slow start: nothing on the dashboard
    for _ in range(50):
        await asyncio.sleep(0.01)
        if "telegram" in c.live_channels:
            break
    assert "telegram" in c.live_channels and c.warnings == []

    starts["n"] = 0
    monkeypatch.setattr(daemon, "START_OUTAGE_S", 0)  # "sustained" from the first failed retry (no wall clock)
    c = container()
    monkeypatch.setattr(daemon, "CHANNELS", {"telegram": (lambda _c: Flaky(10_000), "channels")})
    await daemon._start_channels(c)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if c.warnings:
            break
    assert c.warnings and "can't reach Telegram" in c.warnings[0] and "Retrying" in c.warnings[0]
    for t in asyncio.all_tasks():
        if t.get_name() == "retry-telegram":
            t.cancel()
