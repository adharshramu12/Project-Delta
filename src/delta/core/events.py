"""In-process event bus. Channels, the desktop app and the remote dashboard subscribe to show
live progress (plan, each step, verification) without coupling to the orchestrator."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from delta.system.logging import log

Handler = Callable[[str, dict[str, Any]], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: list[Handler] = []

    def subscribe(self, handler: Handler) -> None:
        self._handlers.append(handler)

    def unsubscribe(self, handler: Handler) -> None:
        with contextlib.suppress(ValueError):
            self._handlers.remove(handler)

    async def publish(self, event: str, **data: Any) -> None:
        if event == "task.step" and "label" not in data:  # plain words for the live progress line (no AI call)
            from delta.core.activity import describe

            data["label"] = describe(str(data.get("tool", "")), data.get("args") or {})
        if event == "task.step":
            from delta.core.activity import note

            note(str(data.get("task_id", "")), str(data["label"]))
        for h in self._handlers:
            try:
                await h(event, data)
            except Exception as e:
                log.error("events.handler_failed", event=event, error=str(e))
