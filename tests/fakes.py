"""Test doubles: scripted LLM providers and sensors. No network, no GPU."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from delta.llm.schemas import LLMError, LLMResponse, Message, ToolCall


class FakeProvider:
    def __init__(self, name: str, replies: list[LLMResponse | Exception] | None = None, local: bool = False,
                 handler: Callable[[str, list[Message]], LLMResponse] | None = None) -> None:
        self.name = name
        self.local = local
        self.replies = list(replies or [])
        self.handler = handler
        self.calls: list[tuple[str, list[Message]]] = []
        self.unloaded = False

    def supports(self, task: str) -> bool:
        return True

    async def complete(self, task: str, messages: list[Message], tools: list[dict] | None,
                       json_mode: bool = False) -> LLMResponse:
        self.calls.append((task, messages))
        if self.handler:
            return self.handler(task, messages)
        r = self.replies.pop(0) if self.replies else LLMError(f"{self.name} has no reply")
        if isinstance(r, Exception):
            raise r
        r.provider = self.name
        return r

    async def unload(self) -> None:
        self.unloaded = True


def text(s: str) -> LLMResponse:
    return LLMResponse(content=s)


def js(obj: Any) -> LLMResponse:
    return LLMResponse(content=json.dumps(obj))


def call(name: str, **args: Any) -> LLMResponse:
    return LLMResponse(content="", tool_calls=[ToolCall(id=f"c_{name}", name=name, arguments=args)])
