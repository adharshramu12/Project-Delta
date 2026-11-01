"""OpenAI-compatible provider (Ollama, Groq, NVIDIA NIM all use this one class).

Small local models often lack reliable native function calling, so a provider can run in
`native_tools=False` mode: tool specs go into the system prompt and the model answers with a
JSON object that we parse back into ToolCalls.
"""

from __future__ import annotations

import contextlib
import json
import re
import uuid
from typing import Any, Protocol

import httpx
import openai

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
    def __init__(self, name: str, cfg: ProviderConfig, api_key: str | None) -> None:
        self.name = name
        self.cfg = cfg
        self.local = cfg.local
        self._client = openai.AsyncOpenAI(
            base_url=cfg.base_url,
            api_key=api_key or "not-needed",
            timeout=cfg.timeout_s,
            max_retries=0,  # the router owns retries/fallback
        )

    def supports(self, task: str) -> bool:
        return task in self.cfg.models or (task != "vision" and "chat" in self.cfg.models)

    def model_for(self, task: str) -> str:
        return self.cfg.models.get(task) or self.cfg.models["chat"]

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

        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError) as e:
            raise LLMError(f"{self.name}: {type(e).__name__}: {e}") from e
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
        if not self.local:
            return
        root = self.cfg.base_url.removesuffix("/v1").rstrip("/")
        async with httpx.AsyncClient(timeout=5) as client:
            for model in set(self.cfg.models.values()):
                with contextlib.suppress(httpx.HTTPError):
                    await client.post(f"{root}/api/generate", json={"model": model, "keep_alive": 0})


def _loads(raw: str | dict | None) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
