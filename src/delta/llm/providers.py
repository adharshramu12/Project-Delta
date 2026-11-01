"""OpenAI-compatible provider (Ollama, Groq, NVIDIA NIM all use this one class).

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


class OpenAICompatProvider:
    def __init__(self, name: str, cfg: ProviderConfig, api_key: str | Callable[[], str | None] | None) -> None:
        self.name = name
        self.cfg = cfg
        self.local = cfg.local
        self._key_source = api_key
        self._api_key: str | None = None
        self._key_checked_at = -1e9
        self._client_obj: Any = None

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
        return self.cfg.models.get(task) or self.cfg.models["chat"]

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
        native = bool(tools) and self.cfg.native_tools
        prompt_tools = bool(tools) and not self.cfg.native_tools
        msgs = self._inject_tool_prompt(messages, tools) if prompt_tools else self._flatten(messages, native)

        kwargs: dict[str, Any] = {"model": model, "messages": msgs, "temperature": 0.2}
        if native:
            kwargs["tools"] = tools
        if json_mode or prompt_tools:
            kwargs["response_format"] = {"type": "json_object"}
        if self.cfg.keep_alive:
            kwargs["extra_body"] = {"keep_alive": self.cfg.keep_alive}

        client = self._client
        import openai

        try:
            try:
                resp = await client.chat.completions.create(**kwargs)
            except (openai.NotFoundError, openai.BadRequestError) as e:
                # Providers retire models often. Instead of breaking for every user until they edit
                # config, pick the best model the provider still offers and carry on.
                replacement = await self._replacement_for(task, model, e)
                if replacement is None:
                    raise
                kwargs["model"] = model = replacement
                resp = await client.chat.completions.create(**kwargs)
        except openai.RateLimitError as e:
            raise LLMError(f"{self.name}: rate limited: {e.message[:200]}", kind="rate_limit",
                           retry_after_s=_retry_after(e)) from e
        except openai.APITimeoutError as e:
            raise LLMError(f"{self.name}: timed out after {self.cfg.timeout_s}s") from e
        except openai.APIConnectionError as e:
            raise LLMError(f"{self.name}: not reachable at {self.cfg.base_url}", kind="unreachable") from e
        except openai.APIStatusError as e:
            raise LLMError(f"{self.name}: HTTP {e.status_code}: {e.message}") from e

        choice = resp.choices[0].message
        content = choice.content or ""
        if prompt_tools:
            return self._parse_prompt_tools(content, model)
        calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=_loads(tc.function.arguments))
            for tc in (choice.tool_calls or [])
            if tc.type == "function"
        ]
        return LLMResponse(content=content, tool_calls=calls, provider=self.name, model=model)

    # --- JSON tool protocol -------------------------------------------------------------
    def _inject_tool_prompt(self, messages: list[Message], tools: list[dict] | None) -> list[Message]:
        specs = json.dumps([t["function"] for t in tools or []], indent=1)
        out = self._flatten(messages, native=False)
        sys = JSON_TOOL_PROMPT.format(tools=specs)
        if out and out[0]["role"] == "system":
            out[0] = {"role": "system", "content": out[0]["content"] + "\n\n" + sys}
        else:
            out.insert(0, {"role": "system", "content": sys})
        return out

    def _parse_prompt_tools(self, content: str, model: str) -> LLMResponse:
        try:
            data = extract_json(content)
        except (json.JSONDecodeError, ValueError):
            return LLMResponse(content=content, provider=self.name, model=model)
        calls = [
            ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=c["name"], arguments=c.get("arguments", {}))
            for c in data.get("tool_calls", [])
            if isinstance(c, dict) and "name" in c
        ]
        return LLMResponse(content=str(data.get("final", "")), tool_calls=calls, provider=self.name, model=model)

    @staticmethod
    def _flatten(messages: list[Message], native: bool) -> list[Message]:
        """Without native tools, rewrite tool-call history as plain text turns."""
        if native:
            return list(messages)
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

    async def unload(self) -> None:
        """Free VRAM right away (Ollama only): keep_alive=0 evicts the model."""
        import httpx

        if not self.local:
            return
        root = self.cfg.base_url.removesuffix("/v1").rstrip("/")
        async with httpx.AsyncClient(timeout=5) as client:
            for model in set(self.cfg.models.values()):
                with contextlib.suppress(httpx.HTTPError):
                    await client.post(f"{root}/api/generate", json={"model": model, "keep_alive": 0})


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


def _loads(raw: str | dict | None) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
