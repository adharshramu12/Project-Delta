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
    """Raised when a provider fails in a way that should trigger fallback.

    kind: "unreachable" (nothing listening, e.g. Ollama not installed), "rate_limit", or "error".
    retry_after_s: for rate limits, how long the provider asked us to wait (if it said).
    """

    def __init__(self, message: str, kind: str = "error", retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after_s = retry_after_s


class AllProvidersFailedError(LLMError):
    def __init__(self, message: str, kinds: list[str] | None = None) -> None:
        super().__init__(message)
        self.kinds = kinds or []

    @property
    def only_rate_limited(self) -> bool:
        return bool(self.kinds) and all(k == "rate_limit" for k in self.kinds if k != "unreachable") \
            and "rate_limit" in self.kinds
