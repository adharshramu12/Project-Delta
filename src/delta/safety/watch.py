"""Break-in attempts: Delta already refuses them; this tells the owner that someone is trying.

Two things are counted, each a refusal that should almost never happen in normal use:
- api_token: a request to Delta's local API with a wrong or missing token. The dashboard always sends the right
  one, so several in a few minutes mean another program on this PC is trying the door.
- telegram_stranger: a message to the Telegram bot from someone who is not paired. One can be a mistake (a bot's
  name is public); several from the same person are someone trying to get in.

When a count reaches its threshold inside its window, the owner is told once (a Windows notification, a line on
the dashboard, the audit log, and a message to the paired phone unless remote control is locked), then not again
for that kind for half an hour, so an attacker cannot turn the alert itself into noise. Nothing here blocks
anything or changes what Delta allows: the token check, the allow-list and the PIN guard already did that.

Wrong PINs and wrong pairing codes are not counted here; they have their own lockouts and alerts
(delta.safety.remote_guard, delta.channels.telegram).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from delta.system.logging import log

# kind -> (how many, within how many seconds)
THRESHOLDS: dict[str, tuple[int, float]] = {"api_token": (5, 300.0), "telegram_stranger": (3, 600.0)}
QUIET_S = 30 * 60  # after an alert, the same kind stays silent this long
MAX_WHO = 40       # a name or id in an alert is cut to this


class IntrusionWatch:
    def __init__(self, container: Any, alert: Callable[[str, str], None] | None = None,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.c = container
        self.alert = alert or (lambda title, text: None)
        self.now = now
        self._seen: dict[str, deque[float]] = {k: deque(maxlen=n) for k, (n, _) in THRESHOLDS.items()}
        self._quiet_until: dict[str, float] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self.loop: asyncio.AbstractEventLoop | None = None  # the server's loop, for alerts raised in a worker thread
        self.alerts = 0

    def note(self, kind: str, who: str = "") -> str:
        """Count one refused attempt. Returns the alert text when this one crossed the threshold, else ""."""
        limit = THRESHOLDS.get(kind)
        if limit is None:
            return ""
        count, window = limit
        t = self.now()
        seen = self._seen[kind]
        seen.append(t)
        if len(seen) < count or t - seen[0] > window or t < self._quiet_until.get(kind, 0.0):
            return ""
        self._quiet_until[kind] = t + QUIET_S
        seen.clear()
        text = self._text(kind, count, window, "".join(ch for ch in str(who) if ch.isprintable())[:MAX_WHO])
        self.alerts += 1
        log.warning("intrusion.alert", kind=kind, who=who)
        self._tell(kind, who, text)
        return text

    @staticmethod
    def _text(kind: str, count: int, window: float, who: str) -> str:
        minutes = int(window // 60)
        if kind == "api_token":
            return (f"{count} requests to Delta on this PC used a wrong or missing key within {minutes} minutes. "
                    "Delta refused all of them. If you did not cause this, another program on this PC may be "
                    "trying to reach Delta: check what is running, and scan the PC.")
        person = f" (Telegram id {who})" if who else ""
        return (f"Someone who is not paired{person} wrote to your Delta bot {count} times within {minutes} minutes. "
                "They cannot see or control anything. If it keeps happening, make a new bot with BotFather and "
                "connect that one.")

    def _tell(self, kind: str, who: str, text: str) -> None:
        c = self.c
        for step in (lambda: c.audit.record("intrusion.alert", kind=kind, who=who),
                     lambda: self.alert("Delta security", text),
                     lambda: c.warn("Security: " + text)):
            try:
                step()
            except Exception:  # an alert that fails must never break the refusal it reports
                log.exception("intrusion.tell_failed", kind=kind)
        message = "\U0001F6E1 Delta security: " + text
        try:
            task = asyncio.get_running_loop().create_task(self._phone(message))
        except RuntimeError:  # a worker thread (the API's token check): hand it to the server's loop
            if self.loop is not None and self.loop.is_running():
                asyncio.run_coroutine_threadsafe(self._phone(message), self.loop)
            return  # with no loop at all (a test), the PC alert and the audit line are enough
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _phone(self, text: str) -> None:
        """To the paired Telegram owner, unless remote control is locked (a locked phone gets nothing)."""
        c = self.c
        ch = getattr(c, "live_channels", {}).get("telegram")
        allowed = list(getattr(ch, "allowed", None) or [])
        if ch is None or not allowed or getattr(getattr(c, "guard", None), "locked", False):
            return
        try:
            await ch.send_text(str(allowed[0]), text)
        except Exception as e:
            log.warning("intrusion.phone_failed", error=str(e))
