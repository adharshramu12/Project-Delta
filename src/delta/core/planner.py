"""Turns a request into a plan with explicit, checkable success criteria."""

from __future__ import annotations

from delta.core.state import Plan
from delta.llm.providers import extract_json
from delta.llm.router import ModelRouter

PLANNER_PROMPT = """You are the planner of {name}, a personal AI agent running on the user's Windows PC.
Tools the agent can use: {tools}.

Decide whether the request needs tools (touching files, apps, the system, the web, memory, or
facts you are not certain about) or can be answered directly (greetings, general knowledge you
are sure of, opinions).

Reply with JSON only:
{{"needs_tools": true|false,
  "answer": "<only if needs_tools is false: the full reply to the user>",
  "goal": "<one sentence>",
  "steps": ["<short step>", ...],
  "success_criteria": ["<observable fact that proves the task is done, checkable with a tool>", ...]}}

Success criteria must be concrete, e.g. "file C:/Users/x/Desktop/notes.txt exists and contains 'hello'",
"process Antigravity.exe is running", "answer cites at least one source URL".

What you know about the user:
{memory}"""


async def make_plan(router: ModelRouter, text: str, tool_names: list[str], memory: str, name: str) -> Plan:
    resp = await router.complete(
        [{"role": "system", "content": PLANNER_PROMPT.format(
            name=name, tools=", ".join(tool_names), memory=memory or "(nothing yet)")},
         {"role": "user", "content": text}],
        task="plan", json_mode=True)
    try:
        data = extract_json(resp.content)
    except ValueError:
        # Planner did not return JSON: fall back to acting with the request as the only criterion.
        return Plan(needs_tools=True, goal=text, success_criteria=[f"The request is fulfilled: {text}"])
    needs = bool(data.get("needs_tools", True))
    plan = Plan(
        needs_tools=needs,
        goal=str(data.get("goal", text)),
        steps=[str(s) for s in data.get("steps", [])][:15],
        success_criteria=[str(c) for c in data.get("success_criteria", [])][:8],
        answer=str(data.get("answer", "")),
    )
    if not plan.needs_tools and not plan.answer:
        plan.needs_tools = True
    if plan.needs_tools and not plan.success_criteria:
        plan.success_criteria = [f"The request is fulfilled: {text}"]
    return plan
