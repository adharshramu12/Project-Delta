"""OpenAI-compatible provider (Ollama, LM Studio, Groq, NVIDIA NIM all use this one class).

Small local models often lack reliable native function calling, so a provider can run in
`native_tools=False` mode: tool specs go into the system prompt and the model answers with a
JSON object that we parse back into ToolCalls.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from delta.config import ProviderConfig
from delta.llm.schemas import LLMError, LLMResponse, Message, ToolCall


class Provider(Protocol):
    name: str
    local: bool

    def supports(self, task: str) -> bool: ...
    async def complete(
        self, task: str, messages: list[Message], tools: list[dict] | None, json_mode: bool
    ) -> LLMResponse: ...
    async def unload(self) -> None: ...


JSON_TOOL_PROMPT = """You can use tools. Available tools (JSON schema):
{tools}

To call tools reply with ONLY this JSON, nothing else:
{{"tool_calls": [{{"name": "<tool name>", "arguments": {{...}}}}]}}
When you are finished and need no more tools, reply with ONLY:
{{"final": "<your answer to the user>"}}"""


def extract_json(text: str) -> Any:
    """Parse the first JSON object in `text` (models like to wrap JSON in prose or fences)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start == -1:
            raise
        depth = 0
        for i, ch in enumerate(text[start:], start):
            depth += ch == "{"
            depth -= ch == "}"
            if depth == 0:
                return json.loads(text[start : i + 1])
        raise


def inject_tool_prompt(messages: list[Message], tools: list[dict] | None) -> list[Message]:
    """Flatten tool history to text and append the JSON tool protocol to the system prompt."""
    specs = json.dumps([t["function"] for t in tools or []], separators=(",", ":"))
    out = flatten_messages(messages)
    sys = JSON_TOOL_PROMPT.format(tools=specs)
    if out and out[0]["role"] == "system":
        out[0] = {"role": "system", "content": out[0]["content"] + "\n\n" + sys}
    else:
        out.insert(0, {"role": "system", "content": sys})
    return out


def parse_prompt_tools(content: str, provider: str, model: str) -> LLMResponse:
    try:
        data = extract_json(content)
    except (json.JSONDecodeError, ValueError):
        return LLMResponse(content=content, provider=provider, model=model)
    if not isinstance(data, dict):
        return LLMResponse(content=content, provider=provider, model=model)
    calls = [
        ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=c["name"], arguments=c.get("arguments", {}))
        for c in data.get("tool_calls", [])
        if isinstance(c, dict) and "name" in c
    ]
    return LLMResponse(content=str(data.get("final", "")), tool_calls=calls, provider=provider, model=model,
                       final="final" in data and not calls)


def flatten_messages(messages: list[Message]) -> list[Message]:
    """Rewrite tool-call history as plain text turns (for models without native tools)."""
    out: list[Message] = []
    for m in messages:
        if m["role"] == "tool":
            out.append({"role": "user", "content": f"[tool result {m.get('name', '')}]\n{m['content']}"})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            calls = [
                {"name": c["function"]["name"], "arguments": _loads(c["function"]["arguments"])}
                for c in m["tool_calls"]
            ]
            out.append({"role": "assistant", "content": json.dumps({"tool_calls": calls})})
        else:
            out.append({"role": m["role"], "content": m.get("content") or ""})
    return out


class OpenAICompatProvider:
    def __init__(self, name: str, cfg: ProviderConfig, api_key: str | Callable[[], str | None] | None) -> None:
        self.name = name
        self.cfg = cfg
        self.local = cfg.local
        self._key_source = api_key
        self._api_key: str | None = None
        self._key_checked_at = -1e9
        self._client_obj: Any = None
        self._exhausted: dict[str, float] = {}  # model -> monotonic time its daily quota resets
        self._stand_in: dict[str, str] = {}     # exhausted model -> model used meanwhile
        self._ping_at = -1e9
        self._ping_ok = False
        self._auto: dict[bool, str] = {}  # vision? -> model picked for `models.<task>: auto`
        self._no_json_mode = False  # server rejected response_format=json_object

    def _key(self) -> str | None:
        """Keys are read on first use and re-checked every minute, so a key added while Delta
        runs is picked up without a restart, and startup never waits on the credential store."""
        if not callable(self._key_source):
            return self._key_source
        now = time.monotonic()
        if self._api_key is None and now - self._key_checked_at > 60:
            self._key_checked_at = now
            self._api_key = self._key_source()
            self._client_obj = None
        return self._api_key

    def available(self) -> bool:
        return not self.cfg.secret or bool(self._key())

    def refresh(self) -> None:
        """Re-read the key now (e.g. the user just saved one on the setup page)."""
        self._api_key = None
        self._key_checked_at = -1e9
        self._client_obj = None

    def ping(self) -> bool:
        """Is anything answering at base_url? Cached for 30 s (status screens call this often)."""
        import httpx

        now = time.monotonic()
        if now - self._ping_at < 30:
            return self._ping_ok
        try:
            self._ping_ok = httpx.get(f"{self.cfg.base_url.rstrip('/')}/models", timeout=2).status_code < 500
        except httpx.HTTPError:
            self._ping_ok = False
        self._ping_at = now
        return self._ping_ok

    @property
    def _client(self) -> Any:
        """The openai package takes ~2 s to import, so it loads on the first model call,
        not at startup."""
        if self._client_obj is None:
            import openai

            self._client_obj = openai.AsyncOpenAI(
                base_url=self.cfg.base_url,
                api_key=self._key() or "not-needed",
                timeout=self.cfg.timeout_s,
                max_retries=0,  # the router owns retries/fallback
            )
        return self._client_obj

    def supports(self, task: str) -> bool:
        fits = task in self.cfg.models or (task != "vision" and "chat" in self.cfg.models)
        return fits and self.available()

    def model_for(self, task: str) -> str:
        model = self.cfg.models.get(task) or self.cfg.models["chat"]
        if model == AUTO_MODEL:
            model = self._auto.get(task == "vision", AUTO_MODEL)
        # A model whose daily quota is used up is swapped for a sibling until the quota resets.
        if self._exhausted.get(model, 0) > time.monotonic() and model in self._stand_in:
            return self._stand_in[model]
        return model

    async def _resolve_auto(self, task: str) -> str:
        """`auto` = use whatever the server has (LM Studio, Jan, llama.cpp: model ids differ per
        user, so a fixed name in config would break for most people)."""
        vision = task == "vision"
        if vision not in self._auto:
            try:
                available = [m.id for m in (await self._client.models.list()).data]
            except Exception as e:
                raise LLMError(f"{self.name}: not reachable at {self.cfg.base_url}", kind="unreachable") from e
            choice = pick_model(available, vision=vision)
            if choice is None:
                raise LLMError(f"{self.name}: no chat model downloaded/loaded", kind="unreachable")
            self._auto[vision] = choice
        return self._auto[vision]

    async def _check_room(self, model: str) -> None:
        import httpx
        import psutil

        free_gb = psutil.virtual_memory().available / 1e9
        if free_gb >= self.cfg.min_free_ram_gb:
            return
        root = self.cfg.base_url.removesuffix("/v1").rstrip("/")
        with contextlib.suppress(httpx.HTTPError, ValueError):
            async with httpx.AsyncClient(timeout=3) as client:
                models = (await client.get(f"{root}/api/v0/models")).json().get("data", [])
            if any(m.get("id") == model and m.get("state") == "loaded" for m in models):
                return  # already in memory: answering costs no extra RAM
        raise LLMError(f"{self.name}: only {free_gb:.1f} GB RAM free; loading {model} needs about "
                       f"{self.cfg.min_free_ram_gb:g} GB", kind="unreachable")

    async def _stand_in_for(self, model: str, task: str) -> str | None:
        """Another model on this provider that still has quota (free tiers count quota per model)."""
        try:
            available = [m.id for m in (await self._client.models.list()).data]
        except Exception:
            return None
        now = time.monotonic()
        tired = {m for m, until in self._exhausted.items() if until > now}
        usable = [m for m in available if m not in tired]
        return pick_model(usable, vision=task == "vision", exclude=model)

    async def _replacement_for(self, task: str, model: str, error: Exception) -> str | None:
        text = str(error).lower()
        if "model" not in text or not any(w in text for w in ("not found", "does not exist", "decommission",
                                                               "not supported", "model_not_found", "invalid model")):
            return None
        try:
            available = [m.id for m in (await self._client.models.list()).data]
        except Exception:
            return None
        choice = pick_model(available, vision=task == "vision", exclude=model)
        if choice:
            from delta.system.logging import log

            log.warning("provider.model_replaced", provider=self.name, retired=model, using=choice,
                        hint=f"update llm.providers.{self.name}.models.{task} in config/delta.yaml")
            for key, value in list(self.cfg.models.items()):  # stop asking for the retired model
                if value == model:
                    self.cfg.models[key] = choice
        return choice

    async def complete(
        self, task: str, messages: list[Message], tools: list[dict] | None, json_mode: bool = False
    ) -> LLMResponse:
        model = self.model_for(task)
        if model == AUTO_MODEL:
            model = await self._resolve_auto(task)
        if self.local and self.cfg.min_free_ram_gb:
            await self._check_room(model)
        native = bool(tools) and self.cfg.native_tools
        prompt_tools = bool(tools) and not self.cfg.native_tools
        msgs = self._inject_tool_prompt(messages, tools) if prompt_tools else self._flatten(messages, native)

        kwargs: dict[str, Any] = {"model": model, "messages": msgs, "temperature": 0.2}
        if native:
            kwargs["tools"] = tools
        if (json_mode or prompt_tools) and not self._no_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if self.cfg.keep_alive:
            kwargs["extra_body"] = {"keep_alive": self.cfg.keep_alive}

        client = self._client
        import openai

        try:
            try:
                resp = await client.chat.completions.create(**kwargs)
            except (openai.NotFoundError, openai.BadRequestError) as e:
                if "response_format" in kwargs and "response_format" in f"{e} {e.body}":
                    # LM Studio only takes json_schema/text. The JSON tool protocol is parsed from
                    # plain text anyway, so ask without it from now on.
                    self._no_json_mode = True
                    del kwargs["response_format"]
                    resp = await client.chat.completions.create(**kwargs)
                else:
                    # Providers retire models often. Instead of breaking for every user until they edit
                    # config, pick the best model the provider still offers and carry on.
                    replacement = await self._replacement_for(task, model, e)
                    if replacement is None:
                        raise
                    kwargs["model"] = model = replacement
                    resp = await client.chat.completions.create(**kwargs)
            except openai.RateLimitError as e:
                if not is_daily_limit(e):
                    raise
                # Daily quota of THIS model is gone; free tiers count quota per model, so a sibling
                # model usually still works. Remember the swap until the quota resets.
                configured = self.cfg.models.get(task) or self.cfg.models["chat"]
                self._exhausted[model] = time.monotonic() + (_retry_after(e) or 3600)
                stand_in = await self._stand_in_for(model, task)
                if stand_in is None:
                    raise
                from delta.system.logging import log

                log.warning("provider.daily_quota_swap", provider=self.name, exhausted=model, using=stand_in)
                self._stand_in[configured] = stand_in
                kwargs["model"] = model = stand_in
                resp = await client.chat.completions.create(**kwargs)
        except openai.RateLimitError as e:
            raise LLMError(f"{self.name}: rate limited: {e.message[:200]}", kind="rate_limit",
                           retry_after_s=_retry_after(e)) from e
        except openai.APITimeoutError as e:
            raise LLMError(f"{self.name}: timed out after {self.cfg.timeout_s}s") from e
        except openai.APIConnectionError as e:
            raise LLMError(f"{self.name}: not reachable at {self.cfg.base_url}", kind="unreachable") from e
        except openai.APIStatusError as e:
            text = str(e.message).lower()
            if e.status_code == 413 or "too large" in text or "reduce your message size" in text \
                    or "context length" in text or "maximum context" in text:
                raise LLMError(f"{self.name}: request too large", kind="too_large") from e
            if "tool call" in text or "tool_use_failed" in text or "failed_generation" in text \
                    or "parse tool" in text:
                # The model wrote a malformed tool call; asking again usually works.
                raise LLMError(f"{self.name}: malformed tool call", kind="bad_tool_call") from e
            raise LLMError(f"{self.name}: HTTP {e.status_code}: {e.message}") from e

        choice = resp.choices[0].message
        content = choice.content or ""
        if prompt_tools:
            parsed = self._parse_prompt_tools(content, model)
            parsed.usage = _openai_usage(resp)
            return parsed
        calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=_loads(tc.function.arguments))
            for tc in (choice.tool_calls or [])
            if tc.type == "function"
        ]
        return LLMResponse(content=content, tool_calls=calls, provider=self.name, model=model,
                           usage=_openai_usage(resp))

    # --- JSON tool protocol -------------------------------------------------------------
    def _inject_tool_prompt(self, messages: list[Message], tools: list[dict] | None) -> list[Message]:
        return inject_tool_prompt(messages, tools)

    def _parse_prompt_tools(self, content: str, model: str) -> LLMResponse:
        return parse_prompt_tools(content, self.name, model)

    @staticmethod
    def _flatten(messages: list[Message], native: bool) -> list[Message]:
        return list(messages) if native else flatten_messages(messages)

    async def unload(self) -> None:
        """Free VRAM right away (Ollama only): keep_alive=0 evicts the model."""
        import httpx

        if not self.local or not self.cfg.keep_alive:  # keep_alive is an Ollama setting
            return
        root = self.cfg.base_url.removesuffix("/v1").rstrip("/")
        async with httpx.AsyncClient(timeout=1.5) as client:
            try:  # nothing listening (Ollama not installed or stopped): nothing to free
                await client.get(f"{root}/api/version")
            except httpx.HTTPError:
                return
            for model in set(self.cfg.models.values()):
                with contextlib.suppress(httpx.HTTPError):
                    await client.post(f"{root}/api/generate", json={"model": model, "keep_alive": 0})


AUTO_MODEL = "auto"


def lmstudio_base_url(default: str, home: Path | None = None) -> str:
    """LM Studio lets users change the server port, and saves it in http-server-config.json
    (under ~/.lmstudio/.internal or ~/.lmstudio/apps/<app>/.internal, depending on the version)."""
    root = (home or Path.home()) / ".lmstudio"
    for f in [root / ".internal" / "http-server-config.json", *root.glob("apps/*/.internal/http-server-config.json")]:
        with contextlib.suppress(OSError, ValueError, TypeError):
            data = json.loads(f.read_text(encoding="utf-8"))
            host = data.get("networkInterface") or "127.0.0.1"
            if host in ("0.0.0.0", "::"):
                host = "127.0.0.1"
            return f"http://{host}:{int(data['port'])}/v1"
    return default

# Ranked name fragments of models known to handle tool calling well. Order = preference.
# Unknown future models still work: anything not matching falls to a size-based guess.
PREFERRED_MODELS = ["gpt-oss-120b", "qwen3", "llama-4", "llama-3.3-70b", "deepseek-v3", "kimi-k2", "gpt-oss-20b",
                    "mistral-large", "llama-3.1-70b", "gemma"]
NOT_CHAT = ("whisper", "guard", "orpheus", "tts", "embed", "rerank", "safeguard", "allam")


def pick_model(available: list[str], vision: bool = False, exclude: str = "") -> str | None:
    """Best general chat/tool model from a provider's list (used when a configured model is gone)."""
    usable = [m for m in available if m != exclude and not any(x in m.lower() for x in NOT_CHAT)]
    if vision:
        usable = [m for m in usable if any(x in m.lower() for x in ("vision", "-vl", "scout", "maverick", "gemma"))]
    for fragment in PREFERRED_MODELS:
        for m in usable:
            if fragment in m.lower():
                return m
    return usable[0] if usable else None


def is_daily_limit(e: Any) -> bool:
    """Per-day quotas (Groq: TPD/RPD) won't clear by waiting a few seconds."""
    text = str(e).lower()
    return "per day" in text or "(tpd)" in text or "(rpd)" in text or "daily" in text


def _retry_after(e: Any) -> float | None:
    """Seconds the provider asked us to wait: Retry-After header, or Groq's 'try again in 6.7s'."""
    with contextlib.suppress(Exception):
        header = e.response.headers.get("retry-after")
        if header:
            return float(header)
    m = re.search(r"try again in ([\d.]+)\s*(ms|s)", str(e))
    if m:
        return float(m.group(1)) / (1000 if m.group(2) == "ms" else 1)
    return None


def _openai_usage(resp: Any) -> dict[str, int]:
    u = getattr(resp, "usage", None)
    if u is None:
        return {}
    cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    return {"input": (u.prompt_tokens or 0) - cached, "cached": cached, "output": u.completion_tokens or 0}


def _loads(raw: str | dict | None) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
