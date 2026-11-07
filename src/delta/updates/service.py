"""The background update check: soon after Delta starts, then every 12 hours (with jitter, so many PCs never ask
GitHub at the same moment), and one Telegram message per new version when Telegram is connected.

It only ever looks. Installing needs the person to press Update now (delta.updates.install)."""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable
from typing import Any

from delta.config import Settings
from delta.system.logging import log
from delta.updates.checker import UpdateChecker

START_DELAY_S = (20.0, 90.0)  # after start-up, so the first check never slows Delta's own start
MIN_SLEEP_S = 60.0

Notifier = Callable[[dict[str, Any]], Awaitable[bool]]


class UpdateService:
    def __init__(self, settings: Settings, checker: UpdateChecker | None = None) -> None:
        self.settings = settings
        self.checker = checker or UpdateChecker(settings)
        self.notify: Notifier | None = None  # set by the daemon: tell Telegram about a new version
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="update-check")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    def wake(self) -> None:
        """A setting changed (checks turned on, another channel): check again soon instead of in 12 hours."""
        self.checker.update_state(next_at=0)
        self._wake.set()

    async def _loop(self) -> None:
        await self._sleep(random.uniform(*START_DELAY_S))  # nosec B311: jitter, not security
        while True:
            try:
                if self.checker.enabled:
                    view = await asyncio.to_thread(self.checker.check)
                    await self.tell(view)
            except Exception as e:  # never let a check end the loop or reach the crash log
                log.info("updates.loop_error", error=type(e).__name__)
            due = self.checker.due() if self.checker.enabled else self.settings.updates.interval_hours * 3600
            await self._sleep(min(max(due, MIN_SLEEP_S), self.settings.updates.interval_hours * 3600 * 1.1))

    async def _sleep(self, seconds: float) -> None:
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), seconds)

    async def tell(self, view: dict[str, Any]) -> None:
        """One Telegram message per new version (remembered in updates.json), if the person wants it."""
        version = view.get("latest")
        if not (view.get("newer") and version and self.settings.updates.telegram and self.notify):
            return
        state = self.checker.load()
        sent = [v for v in state.get("telegram_sent", []) if isinstance(v, str)]
        if version in sent or state.get("skipped") == version:
            return
        try:
            delivered = await self.notify(view)
        except Exception as e:
            log.info("updates.telegram_failed", error=type(e).__name__)
            return
        if delivered:
            self.checker.update_state(telegram_sent=[*sent, version][-10:])


def telegram_text(view: dict[str, Any]) -> str:
    lines = [f"Delta {view['latest']} is out."]
    if view.get("highlights"):
        lines += ["", "What's new:", *[f"- {h}" for h in view["highlights"][:4]]]
    lines += ["", "To update, open Delta on your PC and press Update now. Nothing is installed until you do."]
    return "\n".join(lines)


async def notify_telegram(c: Any, view: dict[str, Any]) -> bool:
    """Send the new-version message to every allowed Telegram user. True when at least one got it."""
    ch = getattr(c, "live_channels", {}).get("telegram")
    if ch is None:
        return False
    text = telegram_text(view)
    ok = False
    for user in list(getattr(ch, "allowed", [])):
        try:
            await ch.send_text(str(user), text)
            ok = True
        except Exception as e:
            log.info("updates.telegram_user_failed", error=type(e).__name__)
    return ok
