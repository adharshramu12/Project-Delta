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
    usage: dict[str, int] = field(default_factory=dict)  # {"input", "cached", "output"} tokens
    final: bool = False  # the model explicitly ended its turn ({"final": ...} in the JSON tool protocol)


Message = dict[str, Any]  # {"role": ..., "content": ..., optional "tool_calls"/"tool_call_id"}


class LLMError(RuntimeError):
    """Raised when a provider fails in a way that should trigger fallback.

    kind: "unreachable" (nothing listening, e.g. Ollama not installed), "rate_limit", "quota" (out of
    credits: skip this provider for a while), "auth" (the API key was refused: expired, revoked or wrong), or
    "error".
    retry_after_s: for rate limits, how long the provider asked us to wait (if it said).
    """

    def __init__(self, message: str, kind: str = "error", retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after_s = retry_after_s


class AllProvidersFailedError(LLMError):
    def __init__(self, message: str, kinds: list[str] | None = None, nothing_set_up: bool = False,
                 errors: list[str] | None = None, user_message: str = "") -> None:
        super().__init__(message)
        self.kinds = kinds or []
        self.errors = errors or []  # each provider's own error, "name: why"
        self.user_message = user_message  # already in plain words for the user (e.g. the main AI's own failure)
        # Every provider tried was a local server (Ollama, LM Studio) that is not running: on a new PC with no key
        # this is "no AI model is set up", not an outage.
        self.nothing_set_up = nothing_set_up

    @property
    def only_rate_limited(self) -> bool:
        return bool(self.kinds) and all(k == "rate_limit" for k in self.kinds if k != "unreachable") \
            and "rate_limit" in self.kinds
