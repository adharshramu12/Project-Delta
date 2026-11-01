"""ModelRouter: picks a provider per task and falls back on failure.

For each request it walks the configured route (e.g. chat: ollama -> groq -> nim) and skips
providers that are (a) unconfigured, (b) circuit-broken after repeated failures, or
(c) local while the governor says the laptop is under strain.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from delta.llm.providers import Provider
from delta.llm.schemas import AllProvidersFailedError, LLMError, LLMResponse, Message
from delta.system.logging import log


@dataclass
class CircuitBreaker:
    threshold: int = 3
    cooldown_s: float = 60.0
    failures: int = 0
    opened_at: float = 0.0

    def available(self, now: float | None = None) -> bool:
        if self.failures < self.threshold:
            return True
        return (now or time.monotonic()) - self.opened_at >= self.cooldown_s  # half-open: try again

    def record_success(self) -> None:
        self.failures = 0

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()

    def trip(self, cooldown_s: float) -> None:
        """Open immediately, e.g. nothing is listening: retrying every call only wastes time."""
        self.failures = self.threshold
        self.opened_at = time.monotonic()
        self.cooldown_s = cooldown_s


MAX_RATE_LIMIT_WAIT_S = 20.0  # waiting this long beats failing or switching to a weaker model
UNREACHABLE_COOLDOWN_S = 300.0


class ModelRouter:
    def __init__(
        self,
        providers: dict[str, Provider],
        routes: dict[str, list[str]],
        allow_local: Callable[[], bool] = lambda: True,
    ) -> None:
        self.providers = providers
        self.routes = routes
        self.allow_local = allow_local
        self.breakers = {name: CircuitBreaker() for name in providers}
        self.last_provider: str | None = None

    def usable(self) -> list[str]:
        """Providers that can serve chat right now (configured, keyed), for status displays."""
        return [n for n, p in self.providers.items() if p.supports("chat")]

    def candidates(self, task: str) -> list[Provider]:
        route = self.routes.get(task) or self.routes.get("chat") or list(self.providers)
        out = []
        for name in route:
            p = self.providers.get(name)
            if p is None or not p.supports(task) or not self.breakers[name].available():
                continue
            if p.local and not self.allow_local():
                log.info("router.skip_local", provider=name, reason="governor")
                continue
            out.append(p)
        return out

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        task: str = "chat",
        json_mode: bool = False,
    ) -> LLMResponse:
        errors: list[str] = []
        for p in self.candidates(task):
            started = time.perf_counter()
            try:
                try:
                    resp = await p.complete(task, messages, tools, json_mode)
                except LLMError as e:
                    # Free tiers often say "try again in 6s": a short wait beats failing the task.
                    if e.kind != "rate_limit" or not e.retry_after_s or e.retry_after_s > MAX_RATE_LIMIT_WAIT_S:
                        raise
                    log.info("router.rate_limit_wait", provider=p.name, seconds=round(e.retry_after_s, 1))
                    await asyncio.sleep(e.retry_after_s + 0.5)
                    resp = await p.complete(task, messages, tools, json_mode)
            except LLMError as e:
                if e.kind == "unreachable":
                    self.breakers[p.name].trip(UNREACHABLE_COOLDOWN_S)
                elif e.kind != "rate_limit":  # being throttled says nothing about provider health
                    self.breakers[p.name].record_failure()
                errors.append(str(e))
                log.warning("router.fallback", provider=p.name, task=task, error=str(e))
                continue
            self.breakers[p.name].record_success()
            if self.last_provider != p.name:
                log.info("router.provider", provider=p.name, model=resp.model, task=task)
            self.last_provider = p.name
            log.debug("router.ok", provider=p.name, ms=round((time.perf_counter() - started) * 1000))
            return resp
        raise AllProvidersFailedError(
            "No model provider is available for task "
            f"'{task}'. Errors: {'; '.join(errors) or 'none configured (add API keys or start Ollama)'}"
        )

    async def unload_local(self) -> None:
        for p in self.providers.values():
            await p.unload()
