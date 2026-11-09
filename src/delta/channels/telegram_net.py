"""Telegram's connection: timeouts that fit slow networks, sends that survive a blip, a poll loop that is watched and
restarted, and an outage message only for a real, sustained outage.

Why this exists (a real PC, November 2026): Telegram "sometimes stopped" and the dashboard showed a "timed out"
banner. python-telegram-bot's defaults are 5 s for connect, read and write and 1 s to get a connection, for every
call. On a slow mobile hotspot or Wi-Fi a reply, a photo download or the "typing..." signal timed out; the handler
raised; the crash guard showed "Something went wrong in Telegram (TimedOut: Timed out)" and kept it. A typing signal
that timed out even stopped the request before it ran, and a reply that timed out was simply lost.

  timeouts            CONNECT_S / READ_S / WRITE_S / MEDIA_WRITE_S / POOL_S for normal calls; the long poll waits
                      POLL_TIMEOUT_S at Telegram and gets its own request object (TrackingRequest) that tells
                      PollHealth how every poll went.
  resilient(send)     a send that hit a network error is tried again (BACKOFF_S), flood control (RetryAfter) waits
                      as long as Telegram asks; a bad request (wrong formatting) is not retried.
  safe_action(chat)   "typing..." is best effort: it never raises and never waits long.
  working(message)    while a task runs: "typing..." every few seconds, and one "Still working on it..." after
                      STILL_WORKING_S, so a long task never looks like a dead bot.
  PollHealth          when the last good poll was, since when polls fail and why. outage() is a plain sentence only
                      after OUTAGE_S of failures (CONFLICT_S for two copies of Delta on one bot; a bad token at once).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from delta.system.logging import log

CONNECT_S = 15.0
READ_S = 30.0
WRITE_S = 30.0
MEDIA_WRITE_S = 120.0     # a photo or document going up on a slow link
POOL_S = 10.0             # waiting for a free connection (several replies at once)
POLL_TIMEOUT_S = 30       # long poll: Telegram holds the request this long when there is nothing new
POLL_READ_MARGIN_S = 15.0  # added to POLL_TIMEOUT_S for the poll's own read timeout
DOWNLOAD_READ_S = 120.0   # reading a 20 MB file from Telegram
BACKOFF_S = (1.0, 3.0, 8.0)  # waits between tries of one send
MAX_RETRY_AFTER_S = 60.0
ACTION_LIMIT_S = 4.0      # the most a "typing..." signal may hold anything up
TYPING_EVERY_S = 4.5      # Telegram shows "typing..." for about 5 s
STILL_WORKING_S = 45.0
OUTAGE_S = 120.0          # polls failing this long is an outage worth a dashboard line
CONFLICT_S = 60.0         # two copies polling one bot: said after this long
STALL_S = 300.0           # no good poll for this long although the loop "runs": restart it
WATCH_EVERY_S = 30.0
RESTART_BACKOFF_S = (10.0, 30.0, 60.0, 120.0, 300.0)

OUTAGE_PREFIX = "Telegram: "


def _err() -> Any:
    from telegram import error

    return error


def describe(exc: BaseException | int | None) -> str:
    """The plain-words reason behind a Telegram failure, for the dashboard."""
    e = _err()
    if isinstance(exc, e.InvalidToken) or exc in (401, 404):
        return ("Telegram says the bot token is wrong or was revoked. Put a new token in Settings > Telegram "
                "(from @BotFather).")
    if isinstance(exc, e.Conflict) or exc == 409:
        return ("another copy of Delta (or another program) is using this Telegram bot at the same time, and "
                "Telegram allows only one. Close the other one (another PC, or a second Delta window).")
    return ("Delta can't reach Telegram right now (no internet, or a firewall, VPN or proxy is blocking "
            "api.telegram.org).")


class PollHealth:
    """How the long poll is going. Fed by TrackingRequest (every poll) and the poll loop's error callback."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.started = clock()
        self.last_ok = 0.0
        self.failing_since: float | None = None
        self.conflict_since: float | None = None
        self.failures = 0
        self.last_error: BaseException | int | None = None

    def ok(self) -> None:
        if self.failing_since is not None:
            log.info("telegram.poll_recovered", failures=self.failures,
                     seconds=round(self.clock() - self.failing_since))
        self.last_ok = self.clock()
        self.failing_since = self.conflict_since = None
        self.failures = 0
        self.last_error = None

    def failed(self, exc: BaseException | int | None) -> None:
        now = self.clock()
        if self.failing_since is None:
            self.failing_since = now
        conflict = exc == 409 or isinstance(exc, _err().Conflict)
        if conflict and self.conflict_since is None:
            self.conflict_since = now
        self.failures += 1
        self.last_error = exc

    def outage(self) -> str | None:
        """One sentence for the dashboard when Telegram has really been unusable, else None."""
        if self.failing_since is None:
            return None
        now = self.clock()
        e = _err()
        if isinstance(self.last_error, e.InvalidToken) or self.last_error in (401, 404):
            return OUTAGE_PREFIX + describe(self.last_error)
        if self.conflict_since is not None and now - self.conflict_since >= CONFLICT_S:
            return OUTAGE_PREFIX + describe(409) + " Delta keeps trying."
        if now - self.failing_since >= OUTAGE_S:
            minutes = max(2, round((now - self.failing_since) / 60))
            return (f"{OUTAGE_PREFIX}{describe(self.last_error)} Messages from your phone are not arriving for about "
                    f"{minutes} minutes. Delta keeps trying and catches up when the connection is back.")
        return None

    def stalled(self) -> bool:
        """No good poll for STALL_S (counted from start when there never was one)."""
        return self.clock() - (self.last_ok or self.started) >= STALL_S


def _tracking_class() -> type:
    from telegram.request import HTTPXRequest

    class TrackingRequest(HTTPXRequest):
        """The long poll's own connection: every answer is reported to PollHealth."""

        health: PollHealth

        async def do_request(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
            try:
                code, payload = await super().do_request(*args, **kwargs)
            except Exception as e:
                self.health.failed(e)
                raise
            if code == 200:
                self.health.ok()
            else:
                self.health.failed(code)
            return code, payload

    return TrackingRequest


try:  # the class must exist at import time for isinstance checks; python-telegram-bot is optional
    TrackingRequest: type = _tracking_class()
except ImportError:  # pragma: no cover - Telegram not installed: the channel is not used either
    TrackingRequest = object


def poll_request(health: PollHealth) -> Any:
    req = TrackingRequest(connection_pool_size=1, connect_timeout=CONNECT_S, read_timeout=POLL_READ_MARGIN_S,
                          write_timeout=WRITE_S, pool_timeout=POOL_S)
    req.health = health
    return req


def build_app(token: str, health: PollHealth) -> Any:
    """The Application with timeouts for slow networks (instead of 5 s everywhere) and the tracked poll."""
    from telegram.ext import Application

    return (Application.builder().token(token).concurrent_updates(True)
            .connect_timeout(CONNECT_S).read_timeout(READ_S).write_timeout(WRITE_S)
            .media_write_timeout(MEDIA_WRITE_S).pool_timeout(POOL_S)
            .get_updates_request(poll_request(health))
            .build())


async def resilient[T](send: Callable[[], Awaitable[T]], what: str = "send") -> T:
    """Run one Telegram call, trying again after a network error (BACKOFF_S) or flood control (as long as Telegram
    asks). Other errors (a bad request, a blocked bot) are raised at once."""
    e = _err()
    tries = len(BACKOFF_S) + 1
    for attempt in range(tries):
        try:
            return await send()
        except e.BadRequest:  # a subclass of NetworkError, but sending it again cannot help
            raise
        except e.RetryAfter as x:
            ra: Any = getattr(x, "_retry_after", None) or getattr(x, "retry_after", 1) or 1
            wait = float(ra.total_seconds()) if hasattr(ra, "total_seconds") else float(ra)
            if attempt == tries - 1 or wait > MAX_RETRY_AFTER_S:
                raise
            log.info("telegram.flood_wait", what=what, seconds=wait)
            await asyncio.sleep(wait + 0.5)
        except e.NetworkError as x:  # TimedOut is one too; BadRequest is not
            if attempt == tries - 1:
                raise
            log.info("telegram.send_retry", what=what, attempt=attempt + 1, error=type(x).__name__)
            await asyncio.sleep(BACKOFF_S[attempt])
    raise RuntimeError("unreachable")  # pragma: no cover


async def safe_action(chat: Any, action: str = "typing", limit_s: float = ACTION_LIMIT_S) -> None:
    """Show "typing..." if Telegram answers quickly; never raise and never hold the request up."""
    with contextlib.suppress(Exception):
        await asyncio.wait_for(chat.send_action(action), limit_s)


@contextlib.asynccontextmanager
async def working(message: Any, still_after: float = STILL_WORKING_S,
                  typing_every: float = TYPING_EVERY_S) -> AsyncIterator[None]:
    """While the block runs: "typing..." again and again, and one "Still working on it..." for a long task."""
    async def keep() -> None:
        start = time.monotonic()
        told = False
        while True:
            await safe_action(message.chat)
            if not told and time.monotonic() - start >= still_after:
                told = True
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(message.reply_text(
                        "⏳ Still working on it. I'll send the answer here when it's done (say \"stop\" to cancel)."),
                        ACTION_LIMIT_S * 2)
            await asyncio.sleep(typing_every)

    task = asyncio.create_task(keep(), name="telegram-working")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task


def is_network(exc: BaseException | None) -> bool:
    """A Telegram network problem (time-out, connection reset, flood control): not a bug in Delta."""
    if exc is None:
        return False
    try:
        e = _err()
    except ImportError:
        return False
    return isinstance(exc, e.NetworkError | e.RetryAfter) and not isinstance(exc, e.BadRequest)
