"""The agent loop: plan -> act -> observe -> verify -> (reflect + retry) -> report."""

from __future__ import annotations

import asyncio
import json
import platform
from datetime import datetime
from pathlib import Path
from typing import Any

from delta.config import Settings, known_folder, save_dir
from delta.core.events import EventBus
from delta.core.planner import make_plan
from delta.core.state import Plan, StepRecord, TaskRequest, TaskResult, Verification
from delta.core.verifier import verify
from delta.llm.router import ModelRouter
from delta.llm.schemas import AllProvidersFailedError, LLMError, Message
from delta.memory.distill import distill
from delta.memory.extractor import extract_facts
from delta.memory.skills import Skill, SkillStore
from delta.memory.store import MemoryStore
from delta.safety.audit import AuditLog
from delta.system.governor import Governor
from delta.system.hardware_guard import Health
from delta.system.logging import log
from delta.tools.registry import ToolExecutor

ACTOR_PROMPT = """You are {name}, {owner}'s personal AI agent with access to their Windows PC
({os}). Today is {now}. Home folder: {home}.
Desktop: {desktop}   Documents: {documents}
SAVE FOLDER: {save_dir}  (the user opens it from the "Delta" item on their Desktop)
Save every file you create in the SAVE FOLDER unless the user names another folder.
Always use full paths (Save dialogs otherwise fall back to their own default, e.g. OneDrive).

Work through the plan using tools, one step at a time, checking results as you go.
Rules:
- Prefer the simplest tool. Read before you write. After changing something, check it
  (path_info, list_running_apps, read_file) so there is evidence.
- If a tool says BLOCKED or DENIED, do not retry it. Find an allowed way or explain.
- If you are unsure about a fact, use web_search; cite URLs you used.
- Desktop apps: read_window first (fast, free). Fill boxes with set_field (more reliable than
  typing; dialogs like 'Save as' can be targeted by title). Click by name with click_element; use
  click_at / look_at_screen only when read_window cannot see it. Re-read after acting.
- Websites: use browser_* tools (Delta's signed-in Chrome). browser_read after each step.
- If a desktop tool says STOPPED, the user took over: stop and report.
- Never ask for or handle passwords, card numbers or API keys.
- When everything is done, reply with a short, friendly summary for the user (no tool call).

GOAL: {goal}
PLAN:
{steps}
SUCCESS CRITERIA:
{criteria}
{skill}
What you know about the user:
{memory}"""


def friendly_model_error(e: LLMError) -> str:
    """What a non-technical user should read when no model could answer."""
    if isinstance(e, AllProvidersFailedError):
        if e.only_rate_limited:
            return ("The AI service is busy right now (free-tier usage limit). Please try again in a minute. "
                    "Adding a second provider key (e.g. NVIDIA NIM) or a local model avoids this.")
        if not e.kinds:
            return ("No AI model is set up yet. Add a free Groq key with `delta secrets set groq_api_key`, "
                    "or install Ollama to run a model on this PC.")
    return f"I couldn't reach an AI model to finish this. Details: {e}"


# Read-only helpers every task may need for checking its own work.
ALWAYS_TOOLS = {"path_info", "read_file", "list_dir", "web_search", "recall"}


class Orchestrator:
    def __init__(self, settings: Settings, router: ModelRouter, executor: ToolExecutor,
                 memory: MemoryStore | None, governor: Governor | None, events: EventBus,
                 audit: AuditLog, skills: SkillStore | None = None) -> None:
        self.s = settings
        self.router = router
        self.executor = executor
        self.memory = memory
        self.governor = governor
        self.events = events
        self.audit = audit
        self.skills = skills
        self._learning: set[asyncio.Task[None]] = set()

    async def drain(self) -> None:
        """Wait for background learning (used at shutdown and in tests)."""
        if self._learning:
            await asyncio.gather(*self._learning, return_exceptions=True)

    async def run(self, req: TaskRequest) -> TaskResult:
        self.audit.record("task.start", task=req.id, channel=req.channel, text=req.text)
        await self.events.publish("task.started", task_id=req.id, text=req.text)
        try:
            result = await self._run(req)
        except LLMError as e:
            result = TaskResult(req.id, "failed", friendly_model_error(e))
        finally:
            self.executor.forget_task(req.id)
        self.audit.record("task.end", task=req.id, status=result.status, attempts=result.attempts)
        await self.events.publish("task.finished", task_id=req.id, status=result.status, answer=result.answer)
        # Learning happens after the answer is delivered, so the user never waits for it.
        t = asyncio.create_task(self._learn(req, result))
        self._learning.add(t)
        t.add_done_callback(self._learning.discard)
        return result

    async def _run(self, req: TaskRequest) -> TaskResult:
        memory_ctx = self.memory.context_for(req.text, self.s.memory.recall_k) if self.memory else ""
        known = self.skills.relevant(req.text) if self.skills else []
        environment = (f"Save folder for new files: {save_dir(self.s.files)}. Desktop: {known_folder('Desktop')}. "
                       f"Documents: {known_folder('Personal')}. Home: {Path.home()}.")
        plan = await make_plan(self.router, req.text, self.executor.registry.names(), memory_ctx,
                               self.s.agent.name, known, environment)
        await self.events.publish("task.plan", task_id=req.id, plan=plan.__dict__)
        if not plan.needs_tools:
            return TaskResult(req.id, "done", plan.answer, provider=self.router.last_provider or "")

        skill = next((s for s in known if s.id == plan.skill_id), None)
        messages = self._initial_messages(req, plan, memory_ctx, skill)
        steps: list[StepRecord] = []
        verification: Verification | None = None
        final = ""
        for attempt in range(1, self.s.agent.max_attempts + 1):
            # First attempt: only the tools the plan needs. A retry gets everything, in case the
            # planner missed one.
            tools = self._tool_specs(plan.tools if attempt == 1 else [])
            final, stopped = await self._act(req, messages, steps, tools)
            if stopped:
                return TaskResult(req.id, "cancelled", stopped, attempt, steps)
            verification = await verify(self.router, req.text, plan, steps, final)
            await self.events.publish("task.verify", task_id=req.id, attempt=attempt,
                                      passed=verification.passed, reason=verification.reason)
            self.audit.record("verify", task=req.id, attempt=attempt, passed=verification.passed,
                              reason=verification.reason)
            if verification.passed:
                return TaskResult(req.id, "done", final, attempt, steps, verification,
                                  self.router.last_provider or "", plan.skill_id)
            messages.append({"role": "user", "content": (
                f"Verification FAILED: {verification.reason}. Unmet: {verification.missing}. "
                "Inspect the current state with tools, fix what is missing, then summarise again.")})
        answer = (f"{final}\n\n⚠ I could not verify this task is complete after "
                  f"{self.s.agent.max_attempts} attempts: {verification.reason if verification else ''}")
        return TaskResult(req.id, "failed", answer.strip(), self.s.agent.max_attempts, steps, verification,
                          self.router.last_provider or "", plan.skill_id)

    def _initial_messages(self, req: TaskRequest, plan: Plan, memory_ctx: str,
                          skill: Skill | None = None) -> list[Message]:
        system = ACTOR_PROMPT.format(
            name=self.s.agent.name, owner=self.s.agent.owner_name or "the user",
            os=f"{platform.system()} {platform.release()}", now=datetime.now().strftime("%A %d %B %Y %H:%M"),
            home=Path.home(), desktop=known_folder("Desktop"), documents=known_folder("Personal"),
            save_dir=save_dir(self.s.files),
            goal=plan.goal,
            steps="\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan.steps)) or "(decide as you go)",
            criteria="\n".join(f"- {c}" for c in plan.success_criteria), memory=memory_ctx or "(nothing yet)",
            skill=(f"\nFOLLOW LEARNED SKILL #{skill.id} ({skill.name}), adapting it to this request:\n"
                   + "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(skill.procedure)) + "\n") if skill else "")
        return [{"role": "system", "content": system}, {"role": "user", "content": req.text}]

    def _tool_specs(self, wanted: list[str]) -> list[dict[str, Any]]:
        """Specs for the planned tools plus a few cheap read-only helpers; all tools if none planned.
        Sending 6 tool descriptions instead of 30+ cuts most of the tokens of every call."""
        specs = self.executor.registry.specs()
        if not wanted:
            return specs
        keep = set(wanted) | ALWAYS_TOOLS
        return [s for s in specs if s["function"]["name"] in keep]

    async def _act(self, req: TaskRequest, messages: list[Message], steps: list[StepRecord],
                   tools: list[dict[str, Any]]) -> tuple[str, str]:
        """Run tool calls until the model gives a final answer. Returns (final_answer, stop_reason)."""
        for _ in range(self.s.agent.max_steps):
            if self.governor and self.governor.health == Health.CRITICAL:
                reasons = ", ".join(self.governor.assessment.reasons)
                return "", f"Paused to protect your laptop ({reasons}). Ask me again once it cools down."
            resp = await self.router.complete(messages, tools=tools, task="chat")
            if not resp.tool_calls:
                messages.append({"role": "assistant", "content": resp.content})
                return resp.content, ""
            messages.append({"role": "assistant", "content": resp.content or "", "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                for c in resp.tool_calls]})
            for call in resp.tool_calls:
                await self.events.publish("task.step", task_id=req.id, tool=call.name, args=call.arguments)
                result = await self.executor.execute(call.name, call.arguments, task_id=req.id)
                steps.append(StepRecord(call.name, call.arguments, result.ok, result.output[:2000], result.evidence))
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                 "content": result.for_llm()})
        return "I ran out of steps for this attempt.", ""

    async def _learn(self, req: TaskRequest, result: TaskResult) -> None:
        """Self-improvement after a task: memory facts + skills. Never raises."""
        if result.status == "cancelled":
            return
        try:
            await self._learn_skill(req, result)
            if self.memory and self.s.memory.enabled:
                if result.steps:
                    self.memory.add(f"{req.text[:200]} -> {result.status}", kind="episode", source="agent")
                if self.s.memory.auto_extract:
                    for fact in await extract_facts(self.router, req.text, result.answer):
                        self.memory.add(fact, kind="profile", source="auto")
                        await self.events.publish("memory.learned", fact=fact)
        except Exception as e:
            log.error("learning.failed", task=req.id, error=str(e))

    async def _learn_skill(self, req: TaskRequest, result: TaskResult) -> None:
        if not self.skills or not self.s.memory.learn_skills:
            return
        verified = result.status == "done" and result.verification is not None and result.verification.passed
        if result.skill_id is not None:  # a learned skill was used: its track record changes
            change = self.skills.record(result.skill_id, success=verified)
        elif verified:
            draft = await distill(self.router, req.text, result.steps)
            if draft is None:
                return
            change = self.skills.learn(draft.name, draft.when_to_use, draft.procedure, draft.tools)
        else:
            return
        self.audit.record("skill", task=req.id, skill=change.skill.id, change=change.change)
        await self.events.publish("skill.changed", change=change.change, skill=change.skill.to_dict())
