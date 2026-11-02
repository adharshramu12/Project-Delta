"""ModelRouter: picks a provider per task and falls back on failure.

For each request it walks the configured route (e.g. chat: ollama -> groq -> nim) and skips
providers that are (a) unconfigured, (b) circuit-broken after repeated failures, or
(c) local while the governor says the laptop is under strain.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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


MAX_RATE_LIMIT_WAIT_S = 20.0  # waiting this long beats switching to a weaker model
LAST_RESORT_WAIT_S = 60.0     # when no other provider is left
DEFAULT_RATE_WAIT_S = 15.0    # rate limited but the provider gave no "try again in" hint
UNREACHABLE_COOLDOWN_S = 300.0  # local servers only; cloud blips use the normal breaker
NETWORK_RETRY_DELAY_S = 1.5


class ModelRouter:
    def __init__(
        self,
        providers: dict[str, Provider],
        routes: dict[str, list[str]],
        allow_local: Callable[[], bool] = lambda: True,
        preferred: str = "auto",
    ) -> None:
        self.providers = providers
        self.routes = routes
        self.preferred = preferred  # the user's main AI: first for every task, others only as fallback
        self.meter: dict[str, int] = {}  # tokens since reset_meter(): input / cached / output / calls
        self.allow_local = allow_local
        self.breakers = {name: CircuitBreaker() for name in providers}
        self.last_provider: str | None = None

    def refresh(self) -> None:
        """Pick up new or changed keys immediately and forget earlier failures."""
        for name, p in self.providers.items():
            if callable(refresh := getattr(p, "refresh", None)):
                refresh()
            self.breakers[name] = CircuitBreaker()

    def route(self, task: str) -> list[str]:
        route = self.routes.get(task) or self.routes.get("chat") or list(self.providers)
        if self.preferred in self.providers:
            route = [self.preferred, *(n for n in route if n != self.preferred)]
        return route

    def usable(self) -> list[str]:
        """Providers that can serve chat right now, in the order they are tried: cloud ones need
        a key, local ones (Ollama) must actually be running."""
        out = []
        for name in self.route("chat"):
            p = self.providers.get(name)
            if p is None:
                continue
            if not p.supports("chat") or not self.breakers[name].available():
                continue
            if p.local and callable(ping := getattr(p, "ping", None)) and not ping():
                continue
            out.append(name)
        return out

    def candidates(self, task: str) -> list[Provider]:
        out = []
        for name in self.route(task):
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
        kinds: list[str] = []
        candidates = self.candidates(task)
        for i, p in enumerate(candidates):
            started = time.perf_counter()
            # With nothing left to fall back to, waiting longer beats failing the user's task.
            max_wait = MAX_RATE_LIMIT_WAIT_S if i < len(candidates) - 1 else LAST_RESORT_WAIT_S
            try:
                try:
                    resp = await p.complete(task, messages, tools, json_mode)
                except LLMError as e:
                    if e.kind == "unreachable" and not p.local:
                        # Cloud + connection error is usually a network blip: one quick retry.
                        log.info("router.network_retry", provider=p.name)
                        await asyncio.sleep(NETWORK_RETRY_DELAY_S)
                        resp = await p.complete(task, messages, tools, json_mode)
                    elif e.kind == "rate_limit" and (e.retry_after_s or DEFAULT_RATE_WAIT_S) <= max_wait:
                        # Free tiers often say "try again in 6s": a short wait beats failing the task.
                        # No hint given: a per-minute limit clears within a minute, so wait a bit anyway.
                        wait = e.retry_after_s or DEFAULT_RATE_WAIT_S
                        log.info("router.rate_limit_wait", provider=p.name, seconds=round(wait, 1))
                        await asyncio.sleep(wait + 0.5)
                        resp = await p.complete(task, messages, tools, json_mode)
                    else:
                        raise
            except LLMError as e:
                if e.kind == "unreachable" and p.local:
                    # Nothing listening on this PC (e.g. Ollama not installed): stop trying for a while.
                    self.breakers[p.name].trip(UNREACHABLE_COOLDOWN_S)
                elif e.kind not in ("rate_limit", "too_large", "bad_tool_call"):  # not a provider health issue
                    self.breakers[p.name].record_failure()
                errors.append(str(e))
                kinds.append(e.kind)
                log.warning("router.fallback", provider=p.name, task=task, error=str(e))
                continue
            self.breakers[p.name].record_success()
            self.meter["calls"] = self.meter.get("calls", 0) + 1
            for k, v in resp.usage.items():
                self.meter[k] = self.meter.get(k, 0) + v
            if self.last_provider != p.name:
                log.info("router.provider", provider=p.name, model=resp.model, task=task)
            self.last_provider = p.name
            log.debug("router.ok", provider=p.name, ms=round((time.perf_counter() - started) * 1000))
            return resp
        raise AllProvidersFailedError(
            "No model provider is available for task "
            f"'{task}'. Errors: {'; '.join(errors) or 'none configured (add API keys or start Ollama)'}",
            kinds,
        )

    def reset_meter(self) -> None:
        self.meter = {}

    async def prewarm(self, task: str) -> None:
        """Let the provider that will answer `task` start up now (Claude Code boots in ~6 s)."""
        first = self.candidates(task)
        if first and callable(warm := getattr(first[0], "prewarm", None)):
            await warm(task)

    async def release(self) -> None:
        """End of a task: close long-lived CLI sessions so they do not hold RAM."""
        for p in self.providers.values():
            if hasattr(p, "_live"):
                await p.unload()

    async def unload_local(self) -> None:
        """Free every provider at once; a provider that is not running must not hold shutdown up."""
        async def one(p: Any) -> None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(p.unload(), timeout=2)
        await asyncio.gather(*(one(p) for p in self.providers.values()))
