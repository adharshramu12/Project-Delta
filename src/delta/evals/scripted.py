"""The scripted model: plays a task's `script` back instead of calling an AI, so the whole eval suite runs
in CI with no keys, no network and no cost. It tests the harness, the checkers and Delta's own agent loop,
tools, policy and task engine, not a model's judgment (that is what real-model runs are for).

Per model job (plan, chat, verify, vision, helper), replies are used in order:
  plan / verify:  a mapping (sent as JSON) or a string
  chat / helper:  {call: {tool, args}} | {calls: [{tool, args}, ...]} | {text: "DONE: ..."} | a string
  vision:         a string (e.g. "MARK 2 - the Save button")
  any job:        {error: rate_limit | too_large | bad_tool_call | unreachable, retry_after_s: 0} raises that
                  model error once (failure injection for the self-heal evals)
When a job's replies run out: chat says "DONE: (script finished)", verify passes, plan fails the task.
"""

from __future__ import annotations

import json
from typing import Any

from delta.llm.schemas import LLMError, LLMResponse, Message, ToolCall


class ScriptedProvider:
    local = False

    def __init__(self, script: dict[str, list[Any]], name: str = "scripted", sees: bool = True) -> None:
        self.name = name
        self.sees = sees  # False: a model that cannot take pictures (attachments: {vision: false})
        self.queues = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []
        self._n = 0

    def supports(self, task: str) -> bool:
        return self.sees or task != "vision"

    async def unload(self) -> None:
        return None

    async def complete(self, task: str, messages: list[Message], tools: list[dict] | None,
                       json_mode: bool = False) -> LLMResponse:
        self.calls.append(task)
        queue = self.queues.get(task) or []
        if not queue:
            if task == "plan":
                raise LLMError("scripted model: the script has no plan reply left")
            if task in ("verify", "vision"):
                return self._done(LLMResponse(content=json.dumps({"passed": True, "reason": "scripted check"})))
            return self._done(LLMResponse(content="DONE: (script finished)"))
        reply = queue.pop(0)
        if isinstance(reply, str):
            return self._done(LLMResponse(content=reply))
        if not isinstance(reply, dict):
            raise LLMError(f"scripted model: bad reply {reply!r}")
        if "error" in reply:  # an injected model failure (rate limit, too large, ...)
            raise LLMError(f"{self.name}: scripted {reply['error']}", kind=str(reply["error"]),
                           retry_after_s=float(reply.get("retry_after_s", 0) or 0) or None)
        if task not in ("chat", "helper") or not ({"call", "calls", "text"} & reply.keys()):
            return self._done(LLMResponse(content=json.dumps(reply)))
        if "text" in reply:
            return self._done(LLMResponse(content=str(reply["text"])))
        calls = reply.get("calls") or [reply["call"]]
        tool_calls = []
        for c in calls:
            self._n += 1
            tool_calls.append(ToolCall(id=f"s{self._n}", name=str(c["tool"]), arguments=dict(c.get("args") or {})))
        return self._done(LLMResponse(content=str(reply.get("say", "")), tool_calls=tool_calls))

    def _done(self, resp: LLMResponse) -> LLMResponse:
        resp.provider = self.name
        return resp
