"""Independent verification: judges the success criteria against tool *evidence*, not against
what the acting model claims."""

from __future__ import annotations

import json

from delta.core.state import Plan, StepRecord, Verification
from delta.llm.providers import extract_json
from delta.llm.router import ModelRouter
from delta.llm.schemas import LLMError

VERIFIER_PROMPT = """You are a strict verifier. Decide if the task is truly complete.
Judge ONLY from the tool log evidence. The agent's own claims are not evidence.
If a criterion cannot be confirmed from the log, it is NOT met.
If the user denied an action or policy blocked it, the task is not complete unless the
agent found an allowed way or the final answer honestly explains the situation and the
request was only a question.

Reply with JSON only: {"passed": true|false, "reason": "<short>", "missing": ["<unmet criterion>", ...]}"""


def _log(steps: list[StepRecord]) -> str:
    rows = [{"tool": s.tool, "args": s.args, "ok": s.ok, "evidence": s.evidence, "output": s.output[:800]}
            for s in steps[-25:]]
    return json.dumps(rows, default=str, indent=1)


async def verify(router: ModelRouter, request: str, plan: Plan, steps: list[StepRecord],
                 final_answer: str) -> Verification:
    if plan.needs_tools and not steps:
        return Verification(False, "no actions were taken", plan.success_criteria)
    content = (f"REQUEST: {request}\n\nSUCCESS CRITERIA:\n"
               + "\n".join(f"- {c}" for c in plan.success_criteria)
               + f"\n\nTOOL LOG:\n{_log(steps)}\n\nAGENT FINAL ANSWER:\n{final_answer[:3000]}")
    try:
        resp = await router.complete(
            [{"role": "system", "content": VERIFIER_PROMPT}, {"role": "user", "content": content}],
            task="verify", json_mode=True)
        data = extract_json(resp.content)
    except (LLMError, ValueError) as e:
        return Verification(False, f"verifier unavailable: {e}")
    return Verification(bool(data.get("passed")), str(data.get("reason", "")),
                        [str(m) for m in data.get("missing", [])])
