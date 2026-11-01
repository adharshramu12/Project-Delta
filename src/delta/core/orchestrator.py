"""The agent loop: plan -> act -> observe -> verify -> (reflect + retry) -> report."""

from __future__ import annotations

import json
import platform
from datetime import datetime

from delta.config import Settings, known_folder, save_dir
from delta.core.events import EventBus
from delta.core.planner import make_plan
from delta.core.state import Plan, StepRecord, TaskRequest, TaskResult, Verification
from delta.core.verifier import verify
from delta.llm.router import ModelRouter
from delta.llm.schemas import LLMError, Message
from delta.memory.extractor import extract_facts
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

What you know about the user:
{memory}"""


class Orchestrator:
    def __init__(self, settings: Settings, router: ModelRouter, executor: ToolExecutor,
                 memory: MemoryStore | None, governor: Governor | None, events: EventBus,
                 audit: AuditLog) -> None:
        self.s = settings
        self.router = router
        self.executor = executor
        self.memory = memory
        self.governor = governor
        self.events = events
        self.audit = audit

    async def run(self, req: TaskRequest) -> TaskResult:
        self.audit.record("task.start", task=req.id, channel=req.channel, text=req.text)
        await self.events.publish("task.started", task_id=req.id, text=req.text)
        try:
            result = await self._run(req)
        except LLMError as e:
            result = TaskResult(req.id, "failed", f"I couldn't reach any AI model: {e}")
        self.audit.record("task.end", task=req.id, status=result.status, attempts=result.attempts)
        await self.events.publish("task.finished", task_id=req.id, status=result.status, answer=result.answer)
        await self._remember(req, result)
        return result

    async def _run(self, req: TaskRequest) -> TaskResult:
        memory_ctx = self.memory.context_for(req.text, self.s.memory.recall_k) if self.memory else ""
        plan = await make_plan(self.router, req.text, self.executor.registry.names(), memory_ctx,
                               self.s.agent.name)
        await self.events.publish("task.plan", task_id=req.id, plan=plan.__dict__)
        if not plan.needs_tools:
            return TaskResult(req.id, "done", plan.answer, provider=self.router.last_provider or "")

        messages = self._initial_messages(req, plan, memory_ctx)
        steps: list[StepRecord] = []
        verification: Verification | None = None
        final = ""
        for attempt in range(1, self.s.agent.max_attempts + 1):
            final, stopped = await self._act(req, messages, steps)
            if stopped:
                return TaskResult(req.id, "cancelled", stopped, attempt, steps)
            verification = await verify(self.router, req.text, plan, steps, final)
            await self.events.publish("task.verify", task_id=req.id, attempt=attempt,
                                      passed=verification.passed, reason=verification.reason)
            self.audit.record("verify", task=req.id, attempt=attempt, passed=verification.passed,
                              reason=verification.reason)
            if verification.passed:
                return TaskResult(req.id, "done", final, attempt, steps, verification,
                                  self.router.last_provider or "")
            messages.append({"role": "user", "content": (
                f"Verification FAILED: {verification.reason}. Unmet: {verification.missing}. "
                "Inspect the current state with tools, fix what is missing, then summarise again.")})
        answer = (f"{final}\n\n⚠ I could not verify this task is complete after "
                  f"{self.s.agent.max_attempts} attempts: {verification.reason if verification else ''}")
        return TaskResult(req.id, "failed", answer.strip(), self.s.agent.max_attempts, steps, verification,
                          self.router.last_provider or "")

    def _initial_messages(self, req: TaskRequest, plan: Plan, memory_ctx: str) -> list[Message]:
        from pathlib import Path

        system = ACTOR_PROMPT.format(
            name=self.s.agent.name, owner=self.s.agent.owner_name or "the user",
            os=f"{platform.system()} {platform.release()}", now=datetime.now().strftime("%A %d %B %Y %H:%M"),
            home=Path.home(), desktop=known_folder("Desktop"), documents=known_folder("Personal"),
            save_dir=save_dir(self.s.files),
            goal=plan.goal,
            steps="\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan.steps)) or "(decide as you go)",
            criteria="\n".join(f"- {c}" for c in plan.success_criteria), memory=memory_ctx or "(nothing yet)")
        return [{"role": "system", "content": system}, {"role": "user", "content": req.text}]

    async def _act(self, req: TaskRequest, messages: list[Message], steps: list[StepRecord]) -> tuple[str, str]:
        """Run tool calls until the model gives a final answer. Returns (final_answer, stop_reason)."""
        tools = self.executor.registry.specs()
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

    async def _remember(self, req: TaskRequest, result: TaskResult) -> None:
        if not self.memory or not self.s.memory.enabled or result.status == "cancelled":
            return
        if result.steps:
            self.memory.add(f"{req.text[:200]} -> {result.status}", kind="episode", source="agent")
        if self.s.memory.auto_extract:
            for fact in await extract_facts(self.router, req.text, result.answer):
                self.memory.add(fact, kind="profile", source="auto")
                log.info("memory.learned", fact=fact)
