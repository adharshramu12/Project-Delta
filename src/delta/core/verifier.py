"""Independent verification: judges each checkpoint against *evidence*, not against what the
acting model claims.

The strongest evidence is a screenshot of the app or page the task worked in, taken after the
agent says it is done. When one is available it goes to a model that can see, and every
checkpoint is judged against what is visibly on screen; the tool log only fills in what a
screenshot cannot show (files on disk, processes)."""

from __future__ import annotations

import json

from delta.core.state import Plan, StepRecord, Verification
from delta.llm.providers import extract_json
from delta.llm.router import ModelRouter
from delta.llm.schemas import LLMError

VERIFIER_PROMPT = """You are a strict verifier. Decide if the task is truly complete.
Judge ONLY from evidence: the FINAL SCREENSHOT (if attached) and the tool log. The agent's own
claims and final answer are not evidence. If a checkpoint cannot be confirmed, it is NOT met.
Content that was already there before the task (old text, other tabs) is not a failure unless
the request asked to remove it.
Compare numbers by VALUE, not by how they are written: digit grouping differs by locale, e.g. Indian
70,06,652 = 7,006,652 = 7006652, and 1,00,000 = 100,000. Only a different value is a mismatch.
Playback (a song or video playing) cannot be seen in a still screenshot: a media_status entry in
the log saying PLAYING with the right title is the proof for it. Browsers report the position
late, so a 0:00 position while PLAYING is normal and NOT evidence of a stuck video.
If the user denied an action or policy blocked it, the task is not complete unless the
agent found an allowed way or the final answer honestly explains the situation and the
request was only a question.

Reply with JSON only:
{"checks": [{"checkpoint": "<checkpoint>", "met": true|false, "seen": "<what in the screenshot or log shows it>"}],
 "passed": true|false, "reason": "<short>"}
passed is true only when every checkpoint is met."""

SCREENSHOT_NOTE = """FINAL SCREENSHOT: attached, taken just now of {target}. It shows the real end
state: judge each checkpoint against what is visible in it first."""


def _log(steps: list[StepRecord]) -> str:
    rows = [{"tool": s.tool, "args": s.args, "ok": s.ok, "evidence": s.evidence, "output": s.output[:800]}
            for s in steps[-25:]]
    return json.dumps(rows, default=str, indent=1)


async def verify(router: ModelRouter, request: str, plan: Plan, steps: list[StepRecord],
                 final_answer: str, screenshot: str | None = None, target: str = "") -> Verification:
    if plan.needs_tools and not steps:
        return Verification(False, "no actions were taken", plan.success_criteria)
    text = (f"REQUEST: {request}\n\nCHECKPOINTS:\n"
            + "\n".join(f"- {c}" for c in plan.success_criteria)
            + f"\n\nTOOL LOG:\n{_log(steps)}\n\nAGENT FINAL ANSWER (a claim, not evidence):\n{final_answer[:3000]}")
    see = bool(screenshot) and bool(router.candidates("vision"))
    if see:
        text = SCREENSHOT_NOTE.format(target=target or "the screen") + "\n\n" + text
        user: dict = {"role": "user", "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot}"}}]}
    else:
        user = {"role": "user", "content": text}
    try:
        resp = await router.complete([{"role": "system", "content": VERIFIER_PROMPT}, user],
                                     task="vision" if see else "verify", json_mode=True)
        data = extract_json(resp.content)
    except (LLMError, ValueError) as e:
        return Verification(False, f"verifier unavailable: {e}")
    checks = [{"checkpoint": str(c.get("checkpoint", "")), "met": _yes(c.get("met")), "seen": str(c.get("seen", ""))}
              for c in data.get("checks", []) if isinstance(c, dict)]
    # The per-checkpoint verdicts decide; the overall flag only counts when there are none. Models
    # sometimes write "all checkpoints are supported" and still set passed=false (or the reverse).
    passed = all(c["met"] for c in checks) if checks else _yes(data.get("passed"))
    return Verification(passed, str(data.get("reason", "")), [str(c["checkpoint"]) for c in checks if not c["met"]],
                        checks, see, raw=resp.content[:2000])


def _yes(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "met", "pass", "passed")
    return bool(value)
