"""In-process event bus. Channels, the desktop app and the remote dashboard subscribe to show
live progress (plan, each step, verification) without coupling to the orchestrator."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from delta.system.logging import log

Handler = Callable[[str, dict[str, Any]], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: list[Handler] = []

    def subscribe(self, handler: Handler) -> None:
        self._handlers.append(handler)

    async def publish(self, event: str, **data: Any) -> None:
        for h in self._handlers:
            try:
                await h(event, data)
            except Exception as e:
                log.error("events.handler_failed", event=event, error=str(e))
