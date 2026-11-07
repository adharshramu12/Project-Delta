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

from delta.llm import usage
from delta.llm.providers import Provider
from delta.llm.schemas import AllProvidersFailedError, LLMError, LLMResponse, Message
from delta.system.logging import log


def _is_cli(p: object) -> bool:
    """Claude Code / Codex / Cline: a CLI that builds its own prompt, so Delta cannot estimate it."""
    try:
        from delta.llm.cli_providers import CLIProvider
    except Exception:
        return False
    return isinstance(p, CLIProvider)


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
QUOTA_COOLDOWN_S = 600.0  # out of credits/quota: no point asking again on every message
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
        self.why_down: dict[str, str] = {}  # last reason each provider failed, in plain words
        self.skipped: dict[str, str] = {}   # providers passed over since reset_meter(): name -> why
        # Opt-in failover (settings router.fallbacks, empty by default): extra providers of the user's own,
        # tried only when every provider on the route failed because of a rate limit, outage or quota.
        self.fallbacks: list[str] = []
        self.fallback_used: str = ""  # the fallback provider that answered this task, if any
        self.model_calls: dict[str, int] = {}  # calls per provider since reset_meter() (token meter: which model)
        # Local servers resting because nothing answered (UNREACHABLE_COOLDOWN_S). One that answers again (the user
        # started LM Studio or Ollama after Delta) is used at once instead of after the rest of the cooldown.
        self._resting_unreachable: set[str] = set()

    def refresh(self) -> None:
        """Pick up new or changed keys immediately and forget earlier failures."""
        for name, p in self.providers.items():
            if callable(refresh := getattr(p, "refresh", None)):
                refresh()
            self.breakers[name] = CircuitBreaker()
        self._resting_unreachable.clear()

    def _ready(self, name: str, p: Provider) -> bool:
        """The provider's breaker lets it be tried, or it is a local server that rested only because nothing
        answered and that answers now (its ping is cached for 30 s, so this costs at most one quick request)."""
        if self.breakers[name].available():
            return True
        if name not in self._resting_unreachable or not callable(ping := getattr(p, "ping", None)):
            return False
        try:
            back = bool(ping())
        except Exception:
            return False
        if back:
            self._resting_unreachable.discard(name)
            self.breakers[name] = CircuitBreaker()
            self.why_down.pop(name, None)
            log.info("router.local_back", provider=name)
        return back

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
            if not p.supports("chat") or not self._ready(name, p):
                continue
            if p.local and callable(ping := getattr(p, "ping", None)) and not ping():
                continue
            out.append(name)
        return out

    def candidates(self, task: str) -> list[Provider]:
        out = []
        for name in self.route(task):
            p = self.providers.get(name)
            if p is None or not p.supports(task):
                continue
            if not self._ready(name, p):
                self.skipped.setdefault(name, self.why_down.get(name, "resting after errors"))
                continue
            if p.local and not self.allow_local():
                log.info("router.skip_local", provider=name, reason="governor")
                continue
            out.append(p)
        return out

    def _first(self, name: str | None, task: str) -> Provider | None:
        """An opt-in ladder rung (one of the user's own providers) usable for this call right now."""
        if not name:
            return None
        p = self.providers.get(name)
        if p is None or not p.supports(task) or not self.breakers[name].available():
            return None
        if p.local and not self.allow_local():
            return None
        return p

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        task: str = "chat",
        json_mode: bool = False,
        prefer: str | None = None,
    ) -> LLMResponse:
        """prefer: a provider to try first for this one call (the opt-in model ladder); on any failure the
        normal route follows. None (the default) = the normal route only, exactly as before."""
        errors: list[str] = []
        kinds: list[str] = []
        candidates = self.candidates(task)
        if (first := self._first(prefer, task)) is not None:
            candidates = [first, *(c for c in candidates if c.name != first.name)]
        try:
            return await self._walk(candidates, messages, tools, task, json_mode, errors, kinds)
        except AllProvidersFailedError:
            extra = self._fallback_candidates(task, {c.name for c in candidates}, errors, kinds)
            if not extra:
                raise
            log.warning("router.fallbacks", task=task, providers=[p.name for p in extra])
            resp = await self._walk(extra, messages, tools, task, json_mode, errors, kinds)
            self.fallback_used = self.last_provider or ""
            return resp

    async def complete_api_only(self, messages: list[Message], tools: list[dict] | None = None,
                                task: str = "helper") -> LLMResponse:
        """One call on the normal route for `task`, skipping CLI providers (Claude Code, Codex, Cline), which
        keep one live session per task and must not be shared by parallel helpers. Changes no route or
        default; raises AllProvidersFailedError when only CLI providers are set up."""
        candidates = [p for p in self.candidates(task) if not _is_cli(p)]
        errors: list[str] = []
        kinds: list[str] = []
        if not candidates:
            raise AllProvidersFailedError(
                f"No API model provider is available for task '{task}' (CLI providers are not used here)", [])
        return await self._walk(candidates, messages, tools, task, False, errors, kinds)

    def _fallback_candidates(self, task: str, tried: set[str], errors: list[str], kinds: list[str]) -> list[Provider]:
        """router.fallbacks (opt-in), only after outage-type failures: rate limit, unreachable, quota."""
        if not self.fallbacks or not kinds:
            return []
        from delta.selfheal.taxonomy import is_outage

        if not all(is_outage(k, e) for k, e in zip(kinds, errors, strict=False)):
            return []
        out = []
        for name in self.fallbacks:
            p = self._first(name, task)
            if p is not None and name not in tried:
                out.append(p)
        return out

    async def _walk(self, candidates: list[Provider], messages: list[Message], tools: list[dict] | None, task: str,
                    json_mode: bool, errors: list[str], kinds: list[str]) -> LLMResponse:
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
                reason = str(e).split(": ", 1)[-1]
                self.why_down[p.name] = reason
                self.skipped.setdefault(p.name, reason)
                if e.kind == "quota":
                    # Out of credits: rest it instead of paying a failed call on every message.
                    self.breakers[p.name].trip(QUOTA_COOLDOWN_S)
                elif e.kind == "unreachable" and p.local:
                    # Nothing listening on this PC (e.g. Ollama not installed): stop trying for a while.
                    self.breakers[p.name].trip(UNREACHABLE_COOLDOWN_S)
                    self._resting_unreachable.add(p.name)
                    if callable(forget := getattr(p, "forget_ping", None)):
                        forget()  # its last "it answers" is stale now: ask again before using it
                elif e.kind not in ("rate_limit", "too_large", "bad_tool_call", "quota"):  # not a provider health issue
                    self.breakers[p.name].record_failure()
                errors.append(str(e))
                kinds.append(e.kind)
                log.warning("router.fallback", provider=p.name, task=task, error=str(e))
                continue
            self.breakers[p.name].record_success()
            self._resting_unreachable.discard(p.name)
            self._count(p, resp, messages, tools)
            if self.last_provider != p.name:
                log.info("router.provider", provider=p.name, model=resp.model, task=task)
            self.last_provider = p.name
            log.debug("router.ok", provider=p.name, ms=round((time.perf_counter() - started) * 1000))
            return resp
        raise AllProvidersFailedError(
            "No model provider is available for task "
            f"'{task}'. Errors: {'; '.join(errors) or 'none configured (add API keys or start Ollama)'}",
            kinds,
            nothing_set_up=bool(candidates) and all(p.local for p in candidates)
            and all(k == "unreachable" for k in kinds),
        )

    def _count(self, p: Provider, resp: LLMResponse, messages: list[Message], tools: list[dict] | None) -> None:
        """Add one call to the task's meter: the provider's own usage when it gave one, else an
        estimate for API providers, or "not reported" for CLI providers (usage.py)."""
        usage.add(self.meter, "calls", 1)
        self.model_calls[p.name] = self.model_calls.get(p.name, 0) + 1
        if resp.usage:
            usage.add(self.meter, "reported_calls", 1)
            for k, v in resp.usage.items():
                usage.add(self.meter, k, v)
            return
        try:
            est_in = usage.estimate_tokens(messages, tools)
            est_out = usage.estimate_output(resp.content or "", resp.tool_calls or [])
        except Exception:  # metering must never fail a call
            est_in = est_out = 0
        if _is_cli(p):
            usage.add(self.meter, "unreported_calls", 1)
            usage.add(self.meter, "unreported_est", est_in + est_out)
        else:
            usage.add(self.meter, "estimated_calls", 1)
            usage.add(self.meter, "est_input", est_in)
            usage.add(self.meter, "est_output", est_out)

    def reset_meter(self) -> None:
        self.meter = {}
        self.skipped = {}
        self.model_calls = {}
        self.fallback_used = ""

    def fallback_note(self) -> str:
        """Why the user's chosen main AI did not answer this task (e.g. out of credits), or ""."""
        note = ""
        if self.preferred in self.skipped and self.last_provider != self.preferred:
            note = f"{self.preferred} skipped: {self.skipped[self.preferred]}"
        if self.fallback_used:  # opt-in failover answered: always said, never silent
            note = (note + "; " if note else "") + f"answered by your fallback provider {self.fallback_used}"
        return note

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
