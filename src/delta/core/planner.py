"""Turns a request into a plan with explicit, checkable success criteria, and picks the small set
of tools the task needs (so each model call carries only those tool descriptions)."""

from __future__ import annotations

from delta.core.state import Plan
from delta.llm.providers import extract_json
from delta.llm.router import ModelRouter
from delta.memory.skills import Skill

PLANNER_PROMPT = """You are the planner of {name}, a personal AI agent running on the user's Windows PC.
{environment}
Tools the agent can use: {tools}.

Decide whether the request needs tools (touching files, apps, the system, the web, memory, or
facts you are not certain about) or can be answered directly (greetings, general knowledge you
are sure of, opinions).

Reply with JSON only:
{{"needs_tools": true|false,
  "answer": "<only if needs_tools is false: the full reply to the user>",
  "goal": "<one sentence>",
  "steps": ["<short step>", ...],
  "tools": ["<tool names the steps need, from the list above>"],
  "success_criteria": ["<observable fact that proves the task is done, checkable with a tool>", ...],
  "skill_id": <id of a skill below that fits this request, or null>}}

Skills the agent learned from earlier verified successes on this PC (reuse one when it fits;
adapt its steps to this request):
{skills}

Use the folders above directly; do not plan steps to search for them.
Success criteria must be concrete, e.g. "file C:/Users/x/Desktop/notes.txt exists and contains 'hello'",
"process Antigravity.exe is running", "answer cites at least one source URL".

What you know about the user:
{memory}"""


async def make_plan(router: ModelRouter, text: str, tool_names: list[str], memory: str, name: str,
                    skills: list[Skill] | None = None, environment: str = "") -> Plan:
    skills = skills or []
    resp = await router.complete(
        [{"role": "system", "content": PLANNER_PROMPT.format(
            name=name, tools=", ".join(tool_names), memory=memory or "(nothing yet)", environment=environment,
            skills="\n".join(s.prompt_line() for s in skills) or "(none yet)")},
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
        tools=[t for t in data.get("tools", []) if isinstance(t, str) and t in tool_names],
    )
    sid = data.get("skill_id")
    if isinstance(sid, int) and any(s.id == sid for s in skills):  # never trust an invented id
        plan.skill_id = sid
        plan.tools = list(dict.fromkeys([*plan.tools, *next(s.tools for s in skills if s.id == sid)]))
    if not plan.needs_tools and not plan.answer:
        plan.needs_tools = True
    if plan.needs_tools and not plan.success_criteria:
        plan.success_criteria = [f"The request is fulfilled: {text}"]
    return plan
