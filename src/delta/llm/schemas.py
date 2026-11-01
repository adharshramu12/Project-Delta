"""Provider-neutral chat types (OpenAI-shaped, since every provider we use speaks that API)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider: str = ""
    model: str = ""


Message = dict[str, Any]  # {"role": ..., "content": ..., optional "tool_calls"/"tool_call_id"}


class LLMError(RuntimeError):
    """Raised when a provider fails in a way that should trigger fallback."""


class AllProvidersFailedError(LLMError):
    pass
