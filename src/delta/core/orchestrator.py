"""The agent loop: plan -> act -> observe -> verify -> (reflect + retry) -> report."""

from __future__ import annotations

import asyncio
import contextlib
import json
import platform
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from delta.config import Settings, known_folder, save_dir
from delta.core.clarify import ask_first
from delta.core.events import EventBus
from delta.core.intents import match_intent
from delta.core.planner import make_plan
from delta.core.proof import ProofRecorder
from delta.core.state import Plan, StepRecord, TaskRequest, TaskResult, Verification
from delta.core.verifier import verify
from delta.learning.corrections import learn_corrections, standing_instructions
from delta.learning.store import LearningStore
from delta.llm.capabilities import abilities_text, brain_for
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
- Websites: use the browser freely (browser_search, browser_open, browser_read, browser_click,
  browser_fill with submit=true for search boxes, browser_scroll, browser_back). It is Delta's own
  Chrome/Edge profile. browser_read after each step to see the page; on long pages pass a focus
  (browser_read focus="born") to get just the relevant lines.
  To search INSIDE a site (YouTube, Amazon, Wikipedia…), type into its box with
  browser_fill field="Search" value="…" submit=true. Clicking a Search button does not type
  anything. Opening the site's search URL directly also works, e.g.
  https://www.youtube.com/results?search_query=… . If the same action fails twice, try another way.
  Clicking Send/Buy/Delete, submitting forms and anything with passwords or payments asks the
  user first.
- If a desktop tool says STOPPED, the user took over: stop and report.
- Songs/videos: to prove something is playing, call media_status (Windows' own Playing/Paused,
  title and position: exact, instant, free). A screenshot cannot show playback. If an ad
  plays first, click its Skip button when shown, then check media_status again.
- Never ask for or handle passwords, card numbers or API keys.
- Signed in to a site with ANY account counts as signed in: carry on and name the account in your
  final summary. Only stop for sign-in if the page asks to log in, or the user named an account.
- KEEP GOING until every success criterion is met. Every reply WITHOUT a tool call must start with:
    DONE: <short, friendly summary for the user>  (all criteria met, you have seen the evidence)
    NEED_USER: <the one question or blocker>    (only if you truly cannot continue: not signed
                                                in, a choice only the user can make, missing info)
  Any other reply is treated as a progress note and you will be asked to continue.

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
                    "sign in to Claude Code or Codex to use your existing plan, "
                    "or start Ollama / LM Studio to run a model on this PC.")
    return f"I couldn't reach an AI model to finish this. Details: {e}"


# Free tiers cap a single request at ~7-8k tokens; ~4 chars per token, leaving room for tools.
CONTEXT_BUDGET_CHARS = 18000
OLD_RESULT_CHARS = 500


def compact_history(messages: list[Message], budget: int, keep_last: int = 2) -> None:
    """Keep long multi-step tasks within the model's request limit.

    The newest tool results stay in full (the model is acting on them); older ones are cut to a
    short head, since their key facts are already in the model's own later messages. If still over
    budget, the newest results are trimmed too. Edits `messages` in place; never drops a message,
    so tool-call ids stay paired."""
    shots = [i for i, m in enumerate(messages) if isinstance(m.get("content"), list)]
    for i in shots[:-1]:  # only the newest screenshot is worth its ~1k tokens
        messages[i]["content"] = "[earlier screenshot removed to save tokens]"
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for i in tool_idx[:-keep_last] if keep_last else tool_idx:
        content = str(messages[i].get("content", ""))
        if len(content) > OLD_RESULT_CHARS:
            messages[i]["content"] = content[:OLD_RESULT_CHARS] + "\n…[older result trimmed to save space]"

    def size() -> int:
        return sum(len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or "")) for m in messages)

    over = size() - budget
    for i in reversed(tool_idx[-keep_last:] if keep_last else []):
        if over <= 0:
            break
        content = str(messages[i].get("content", ""))
        keep = max(OLD_RESULT_CHARS, len(content) - over - 100)
        if keep < len(content):
            messages[i]["content"] = content[:keep] + "\n…[trimmed to fit the model's limit]"
            over = size() - budget


DONE, NEED_USER = "DONE:", "NEED_USER:"
MAX_NUDGES = 3  # small models may never use the markers: accept their answer after this many
CONTINUE_NUDGE = ("Not finished yet: that reply had no tool call and did not start with DONE: or NEED_USER:. "
                  "Continue with the next unmet success criterion now (call a tool). Reply DONE: only when "
                  "every criterion is met, or NEED_USER: if only the user can unblock you.")

LOOKING = {"read_window", "list_windows", "path_info", "look_at_screen", "media_status"}


def stuck_in_loop(steps: list[StepRecord], repeats: int = 3) -> bool:
    """The same action with the same arguments `repeats` times in a row (reads in between ignored,
    since checking the page after each try is normal)."""
    acts = [(s.tool, json.dumps(s.args, sort_keys=True)) for s in steps
            if not s.tool.endswith("_read") and s.tool not in LOOKING]
    return len(acts) >= repeats and len(set(acts[-repeats:])) == 1


# Actions that change what is on screen. After each one the agent sees the result (AFTER: ...),
# like a person glancing at the screen after clicking, instead of acting blind.
DESKTOP_ACTIONS = {"click_element", "click_at", "do_actions", "type_text", "press_keys", "set_field", "scroll",
                   "focus_window", "close_window", "open_app", "open_target"}
BROWSER_ACTIONS = {"browser_open", "browser_search", "browser_click", "browser_fill", "browser_press",
                   "browser_scroll", "browser_back"}
TEXT_ACTIONS = {"type_text", "set_field", "press_keys"}
PIXEL_ACTIONS = {"click_at", "scroll", "do_actions"}  # aimed by coordinates: only a screenshot shows what happened
OBSERVE_CHARS = 1500
OBSERVE_WIDTH = 1024  # step screenshots: enough to read buttons, about 1k image tokens
FINAL_WIDTH = 1280    # the proof screenshot the verifier judges

def proof_target(steps: list[StepRecord]) -> str | None:
    """Title of the window the task last worked in: "" = whole screen, None = no screen work."""
    for s in reversed(steps):
        if s.tool in BROWSER_ACTIONS or s.tool == "browser_read":
            return str(s.evidence.get("title") or s.evidence.get("browser") or "")
        if s.tool in DESKTOP_ACTIONS or s.tool in {"read_window", "look_at_screen"}:
            return str(s.args.get("window") or s.evidence.get("window") or s.evidence.get("foreground_window") or "")
    return None


# Read-only helpers every task may need for checking its own work.
ALWAYS_TOOLS = {"path_info", "read_file", "list_dir", "web_search", "recall",
                "list_windows", "read_window", "focus_window", "look_at_screen", "browser_read",
                "media_status", "do_actions", "scroll"}


class Orchestrator:
    def __init__(self, settings: Settings, router: ModelRouter, executor: ToolExecutor,
                 memory: MemoryStore | None, governor: Governor | None, events: EventBus,
                 audit: AuditLog, skills: SkillStore | None = None,
                 learning: LearningStore | None = None, proof: ProofRecorder | None = None) -> None:
        self.s = settings
        self.router = router
        self.executor = executor
        self.memory = memory
        self.governor = governor
        self.events = events
        self.audit = audit
        self.skills = skills
        self.learning = learning
        self.proof = proof
        self._learning: set[asyncio.Task[None]] = set()

    async def drain(self) -> None:
        """Wait for background learning (used at shutdown and in tests)."""
        if self._learning:
            await asyncio.gather(*self._learning, return_exceptions=True)

    async def run(self, req: TaskRequest) -> TaskResult:
        self.audit.record("task.start", task=req.id, channel=req.channel, text=req.text)
        await self.events.publish("task.started", task_id=req.id, text=req.text)
        if self.proof:
            self.proof.start(req.id, req.text)
        self.router.reset_meter()
        try:
            result = await self._run(req)
        except LLMError as e:
            result = TaskResult(req.id, "failed", friendly_model_error(e))
        finally:
            self.executor.forget_task(req.id)
            await self.router.release()  # free long-lived CLI sessions (RAM) between tasks
        result.tokens = dict(self.router.meter)
        if self.proof:
            self.proof.finish(result)
        self.audit.record("task.end", task=req.id, status=result.status, attempts=result.attempts)
        await self.events.publish("task.finished", task_id=req.id, status=result.status, answer=result.answer)
        # Learning happens after the answer is delivered, so the user never waits for it.
        t = asyncio.create_task(self._learn(req, result))
        self._learning.add(t)
        t.add_done_callback(self._learning.discard)
        return result

    async def _run(self, req: TaskRequest) -> TaskResult:
        # Never guess what only the user can decide (e.g. a meeting's day/time): ask first, 0 tokens.
        if question := ask_first(req.text):
            return TaskResult(req.id, "needs_user", question.text, options=question.options,
                              asked_about=req.text)
        steps: list[StepRecord] = []
        # Instant commands ("pause the music", "start a Google Meet"): 0 tokens, no planner call.
        if intent := match_intent(req.text, set(self.executor.registry.names())):
            quick = Plan(True, goal=req.text, success_criteria=[intent.check],
                         direct={"tool": intent.tool, "args": intent.args})
            await self.events.publish("task.plan", task_id=req.id, plan=quick.__dict__)
            if done := await self._direct(req, quick, steps):
                return done
        memory_ctx = self.memory.context_for(req.text, self.s.memory.recall_k) if self.memory else ""
        if self.learning and (rules := standing_instructions(self.learning)):
            memory_ctx = (f"STANDING INSTRUCTIONS FROM THE USER (always follow these):\n{rules}\n\n"
                          + (memory_ctx or ""))
        known = self.skills.relevant(req.text) if self.skills else []
        environment = (f"Now: {datetime.now():%A %d %B %Y %H:%M} (local time). "
                       f"Save folder for new files: {save_dir(self.s.files)}. Desktop: {known_folder('Desktop')}. "
                       f"Documents: {known_folder('Personal')}. Home: {Path.home()}.")
        # Boot the acting model (Claude Code: ~6 s) while the plan is being made, not after.
        warm = asyncio.create_task(self.router.prewarm("chat"))
        plan = await make_plan(self.router, req.text, self.executor.registry.names(), memory_ctx,
                               self.s.agent.name, known, environment)
        with contextlib.suppress(Exception):
            await warm
        await self.events.publish("task.plan", task_id=req.id, plan=plan.__dict__)
        if not plan.needs_tools:
            return TaskResult(req.id, "done", plan.answer, provider=self.router.last_provider or "")
        if plan.direct and not steps:
            done = await self._direct(req, plan, steps)
            if done is not None:
                return done

        skill = next((s for s in known if s.id == plan.skill_id), None)
        messages = self._initial_messages(req, plan, memory_ctx, skill)
        if steps:  # the fast path ran but could not prove the result: continue from there
            messages.append({"role": "user", "content": (
                f"Already done: {steps[0].tool}({json.dumps(steps[0].args)}) -> {steps[0].output[:800]}\n"
                "Continue from here to finish the task.")})
        budget = self._step_budget()
        messages[0]["content"] += "\n\n" + abilities_text(self._brain(), self._can_see(), budget)
        verification: Verification | None = None
        final = ""
        # The screenshot check runs on its own (cheap) model: boot it while the agent works.
        warm_checker = asyncio.create_task(self.router.prewarm("vision"))
        self._learning.add(warm_checker)  # keep a reference; drained like learning at shutdown
        warm_checker.add_done_callback(self._learning.discard)
        for attempt in range(1, self.s.agent.max_attempts + 1):
            # Only the tools the plan needs (plus looking/checking helpers), on every attempt: the
            # same tool list keeps a Claude Code session and its prompt cache alive across retries,
            # where switching to all ~30 tools would restart it and re-send everything.
            tools = self._tool_specs(plan.tools)
            final, stopped = await self._act(req, messages, steps, tools, budget)
            if stopped:
                return TaskResult(req.id, "cancelled", stopped, attempt, steps)
            if final.startswith(NEED_USER):
                # A real blocker only the user can clear: ask, don't burn retries on it.
                return TaskResult(req.id, "needs_user", final.removeprefix(NEED_USER).strip(), attempt, steps,
                                  provider=self.router.last_provider or "")
            final = final.removeprefix(DONE).strip()
            await self._fresh_media_status(req.id, steps)
            shot, target = await self._final_screenshot(steps)
            verification = await verify(self.router, req.text, plan, steps, final, shot, target)
            await self.events.publish("task.verify", task_id=req.id, attempt=attempt,
                                      passed=verification.passed, reason=verification.reason,
                                      checks=verification.checks, screenshot=verification.screenshot)
            self.audit.record("verify", task=req.id, attempt=attempt, passed=verification.passed,
                              reason=verification.reason, checks=verification.checks, raw=verification.raw)
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

    async def _direct(self, req: TaskRequest, plan: Plan, steps: list[StepRecord]) -> TaskResult | None:
        """Fast path: the planner named one tool call that does the whole job. If the tool proves its
        own result (evidence["verified"], e.g. Windows says the song is PLAYING), that IS the
        verification: no agent loop, no screenshots, no checker call. Otherwise fall back to the
        normal loop, which starts with this step already done."""
        tool, args = plan.direct["tool"], plan.direct.get("args", {})  # type: ignore[index]
        await self.events.publish("task.step", task_id=req.id, tool=tool, args=args)
        result = await self.executor.execute(tool, args, task_id=req.id)
        steps.append(StepRecord(tool, args, result.ok, result.output[:2000], result.evidence))
        if result.evidence.get("user_denied"):
            return TaskResult(req.id, "cancelled", f"OK, I didn't do it. You declined: "
                              f"{result.evidence.get('denied_action', tool)}.", 1, steps)
        if not (result.ok and result.evidence.get("verified")):
            log.info("orchestrator.direct_fallback", task=req.id, tool=tool, ok=result.ok)
            return None
        checks = [{"checkpoint": c, "met": True, "seen": result.output[:300]} for c in plan.success_criteria]
        verification = Verification(True, f"{tool} proved it: {result.output[:200]}", [], checks, False)
        await self.events.publish("task.verify", task_id=req.id, attempt=1, passed=True,
                                  reason=verification.reason, checks=checks, screenshot=False)
        return TaskResult(req.id, "done", result.output, 1, steps, verification,
                          self.router.last_provider or "", fast=True)

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

    def _brain(self) -> str:
        """The provider that will most likely answer: the main AI, else the first on the route."""
        first = self.router.candidates("chat")
        return first[0].name if first else ""

    def _can_see(self) -> bool:
        return "look_at_screen" in self.executor.registry.names() and bool(self.router.candidates("vision"))

    def _step_budget(self) -> int:
        return max(6, round(self.s.agent.max_steps * brain_for(self._brain()).step_factor))

    def _brain_sees(self) -> bool:
        """The acting model takes images itself, so a screenshot can go straight into its
        conversation instead of costing a separate describe-the-screen call."""
        first = self.router.candidates("chat")
        return bool(first) and first[0].supports("vision") and self._can_see()

    async def _observe(self, tool: str, task_id: str, steps: list[StepRecord] | None = None
                       ) -> tuple[str, str, dict[str, Any], str | None] | None:
        """What the screen/page shows right after an action: (observer, text, args, screenshot)."""
        if not self.s.agent.auto_observe:
            return None
        names = set(self.executor.registry.names())
        if tool in PIXEL_ACTIONS and self._brain_sees():
            shot, target = await self._screenshot(proof_target(steps or []) or "", OBSERVE_WIDTH, "after " + tool)
            if shot:
                return "screenshot", f"(screenshot of {target} attached below)", {"window": target}, shot
        if tool in BROWSER_ACTIONS and "browser_read" in names:
            observer, args = "browser_read", {}
        elif tool in PIXEL_ACTIONS and self._can_see():
            observer, args = "look_at_screen", {"question": (
                "In 2-3 short sentences: what is on the screen now? Mention any dialog, error, "
                "selected item or text field with focus.")}
        elif tool in DESKTOP_ACTIONS and "read_window" in names:
            # Typed text sits deep in the control tree (e.g. Notepad's editor): read far enough to show it.
            observer, args = "read_window", {"depth": 10 if tool in TEXT_ACTIONS else 5}
        else:
            return None
        result = await self.executor.execute(observer, args, task_id=task_id)
        if not result.ok:
            return None
        text = result.output
        if len(text) > OBSERVE_CHARS:
            text = text[:OBSERVE_CHARS] + "\n…[more: call the read tool again with a focus]"
        return observer, text, args, None

    async def _fresh_media_status(self, task_id: str, steps: list[StepRecord]) -> None:
        """Playback tasks: what Windows says is playing right now goes into the evidence, so the
        check never relies on an old reading (e.g. taken while an ad was still running).
        Exact, ~0.6 s and no AI tokens."""
        if not any(s.tool in ("media_status", "media_control") for s in steps):
            return
        result = await self.executor.execute("media_status", {}, task_id=task_id)
        if result.ok:
            steps.append(StepRecord("media_status", {"auto": "before final check"}, True, result.output[:2000],
                                    result.evidence))

    async def _final_screenshot(self, steps: list[StepRecord]) -> tuple[str | None, str]:
        """A fresh picture of where the task happened (the app window or browser page it used),
        for the verifier and the saved proof. None when the task never touched the screen."""
        if not self.s.agent.auto_observe or not self._can_see():
            return None, ""
        target = proof_target(steps)
        if target is None:
            return None, ""
        return await self._screenshot(target, FINAL_WIDTH, "final")

    async def _screenshot(self, target: str, width: int, label: str) -> tuple[str | None, str]:
        """One window (even if covered) or, if not found, the whole screen; saved as proof."""
        try:
            from delta.tools import screen

            hwnd = await asyncio.to_thread(screen.find_window_handle, target) if target else None
            if hwnd:
                b64 = (await asyncio.to_thread(screen.capture_window_jpeg, hwnd, width))[0]
            else:
                target = "the whole screen"
                b64 = (await asyncio.to_thread(screen.capture_jpeg, width))[0]
        except Exception as e:  # no display, locked screen, missing extras: carry on without it
            log.info("orchestrator.screenshot_failed", error=str(e))
            return None, ""
        if self.proof:
            self.proof.save_image(b64, f"{label} {target}")
        return b64, target

    def _tool_specs(self, wanted: list[str]) -> list[dict[str, Any]]:
        """Specs for the planned tools plus a few cheap read-only helpers; all tools if none planned.
        Sending 6 tool descriptions instead of 30+ cuts most of the tokens of every call."""
        specs = self.executor.registry.specs()
        if not wanted:
            return specs
        keep = set(wanted) | ALWAYS_TOOLS
        return [s for s in specs if s["function"]["name"] in keep]

    async def _complete_with_recovery(self, messages: list[Message], tools: list[dict[str, Any]]) -> Any:
        """Model call that recovers from the two common, fixable failures of free/small models:
        a request over the size limit (trim harder) and a malformed tool call (ask again)."""
        for attempt in range(3):
            try:
                return await self.router.complete(messages, tools=tools, task="chat")
            except LLMError as e:
                kinds = {e.kind, *getattr(e, "kinds", [])}
                if attempt == 2 or not kinds & {"too_large", "bad_tool_call"}:
                    raise
                if "too_large" in kinds:
                    # The provider's real limit is lower than our estimate: cut at least 30%.
                    current = sum(len(str(m.get("content") or "")) for m in messages)
                    compact_history(messages, min(CONTEXT_BUDGET_CHARS // 2, int(current * 0.7)), keep_last=1)
                else:
                    log.info("orchestrator.retry_bad_tool_call", attempt=attempt + 1)
                    messages.append({"role": "user", "content": (
                        "Your last tool call was not valid JSON. Call the tool again with arguments as a "
                        "valid JSON object (double quotes, no comments, no trailing commas).")})
        raise AssertionError("unreachable")

    async def _act(self, req: TaskRequest, messages: list[Message], steps: list[StepRecord],
                   tools: list[dict[str, Any]], budget: int | None = None) -> tuple[str, str]:
        """Run tool calls until the model gives a final answer. Returns (final_answer, stop_reason)."""
        nudges = 0
        for _ in range(budget or self.s.agent.max_steps):
            if self.governor and self.governor.health == Health.CRITICAL:
                reasons = ", ".join(self.governor.assessment.reasons)
                return "", f"Paused to protect your laptop ({reasons}). Ask me again once it cools down."
            compact_history(messages, CONTEXT_BUDGET_CHARS)
            resp = await self._complete_with_recovery(messages, tools)
            if not resp.tool_calls:
                content = (resp.content or "").strip()
                if getattr(resp, "final", False) and not content.startswith((DONE, NEED_USER)):
                    content = f"{DONE} {content}"  # {"final": ...} is an explicit "I am finished"
                messages.append({"role": "assistant", "content": content})
                if content.startswith((DONE, NEED_USER)) or nudges >= MAX_NUDGES:
                    return content, ""
                # A progress note or "should I continue?" is not the end of the task.
                nudges += 1
                log.info("orchestrator.continue_nudge", task=req.id, nudge=nudges, said=content[:160])
                messages.append({"role": "user", "content": CONTINUE_NUDGE})
                continue
            messages.append({"role": "assistant", "content": resp.content or "", "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                for c in resp.tool_calls]})
            latest_shot: tuple[str, str] | None = None
            for call in resp.tool_calls:
                await self.events.publish("task.step", task_id=req.id, tool=call.name, args=call.arguments)
                result = await self.executor.execute(call.name, call.arguments, task_id=req.id)
                steps.append(StepRecord(call.name, call.arguments, result.ok, result.output[:2000], result.evidence))
                if result.evidence.get("user_denied"):
                    # A human "no" ends the task: no workarounds, no retries, no second question.
                    return "", (f"OK, I didn't do it. You declined: {result.evidence.get('denied_action', call.name)}. "
                                "Tell me if you want something different.")
                content = result.for_llm()
                if result.ok and (seen := await self._observe(call.name, req.id, steps)):
                    observer, text, obs_args, shot = seen
                    if shot:
                        latest_shot = (shot, obs_args.get("window", ""))
                    steps.append(StepRecord(observer, obs_args, True, text[:2000], {"auto_observe": True}))
                    await self.events.publish("task.step", task_id=req.id, tool=observer, args={"auto": True})
                    content += f"\n\nAFTER ({observer}):\n{text}"
                if stuck_in_loop(steps):
                    # Small models repeat a failing action; tell them plainly to change approach.
                    content += ("\n\nNOTE: you have now done exactly this 3 times in a row and it is not "
                                "working. Do something different (another tool, another field, a direct "
                                "URL), or tell the user what is blocking you.")
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content})
            if latest_shot:
                # The picture itself, for a model that can see: no extra vision call, no description.
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": f"AFTER screenshot of {latest_shot[1] or 'the screen'}:"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{latest_shot[0]}"}}]})
        return "I ran out of steps for this attempt.", ""

    async def _learn(self, req: TaskRequest, result: TaskResult) -> None:
        """Self-improvement after a task: memory facts + skills. Never raises."""
        if result.status in ("cancelled", "needs_user") or result.fast:
            return
        try:
            if self.learning:
                for text in await learn_corrections(self.router, self.learning, req.text):
                    self.audit.record("learned", task=req.id, kind="preference", content=text)
                    item = self.learning.find("preference", re.sub(r"\W+", " ", text.lower()).strip())
                    await self.events.publish("learning.added", item=item.to_dict() if item else {"content": text})
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
