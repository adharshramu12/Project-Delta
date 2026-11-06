"""The Healer: runs the recovery playbook for a failed step, inside the agent loop (Orchestrator._act).

    failed step -> classify (taxonomy) -> caps left? -> playbook actions (deterministic, no AI)
                -> retry once if safe -> still failing: strategy ladder (step down a rung) -> hint or stop

Safety, in code:
- Every action and every retry is an ordinary ToolExecutor.execute call: policy, approvals, Remote Guard,
  ExfilGuard, taint and the resume repeat check apply exactly as for the model's own calls.
- Refusals (BLOCKED, REFUSED, the user's "no", the user taking over the mouse) are never recovered.
- A step that changes something (send, delete, purchase, submit, write, shell) is never retried, unless the
  failure proves it did not happen (the tool found no target and returned before acting: PRE_ACTION). Shell,
  code, messaging and file tools are never retried at all.
- A strategy step-down runs only actions that read or act without changing anything (risk read/act).
- Benign popups are closed only when the policy allows that click without asking, only by a reject /
  not-now / close button (never "accept"), and never when payment, password, permission or security words
  are involved. Captchas go to the existing captcha rules; sign-in walls stop and ask the user.
- Attempts are capped per step (selfheal.max_per_step) and per task (selfheal.max_per_task).

Each recovery adds one plain-English line to the run timeline (event kind "recovery").
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from delta.config import SelfHealConfig
from delta.core.state import StepRecord
from delta.engine.recorder import ALWAYS_SIDE_EFFECT, fingerprint, risk_of
from delta.selfheal import strategy
from delta.selfheal.playbooks import PRE_ACTION, Playbook, playbook_for
from delta.selfheal.taxonomy import Failure, classify, is_refusal
from delta.system.logging import log
from delta.tools.base import ToolResult

F = Failure
SAFE_RISKS = {"read", "act"}
READ_BACK = {"web": "browser_read", "desktop": "read_window"}

# A harmless banner: cookie notices, newsletter and "get the app" prompts, notification nags.
BENIGN_CONTEXT = re.compile(r"cookie|consent|newsletter|get the app|open in (the )?app|app banner|"
                            r"notifications? (prompt|banner)|turn on notifications|stay in the loop", re.I)
# Never touched by an automatic dismiss.
DANGER = re.compile(r"pay|payment|card|checkout|purchase|buy|order|password|passcode|permission|allow access|"
                    r"security|verify|verification|two.factor|2fa|otp|sign ?in|log ?in|account|delete|remove|"
                    r"subscribe|subscription|install|admin|camera|microphone|location|captcha", re.I)
# Buttons that close a banner without agreeing to anything, best first. Never "accept" or "agree".
BENIGN_BUTTONS: list[re.Pattern[str]] = [re.compile(p, re.I) for p in (
    r"^reject( all)?( cookies)?$", r"^decline( all)?$",
    r"^(use |allow )?(only )?(necessary|essential)( cookies)?( only)?$",
    r"^continue without accepting$", r"^not now$", r"^no,? thanks$", r"^maybe later$", r"^dismiss$",
    r"^close( banner| dialog)?$", r"^(x|×|✕)$", r"^got it$",
)]
_ELEMENT = re.compile(r'^\s*(?:[+~] )?\[(\d+)\] (button|link) "([^"]{1,60})"', re.M)
_PASSWORD_FIELD = re.compile(r'^\s*(?:[+~] )?\[\d+\] textbox "[^"]*" \(password\)', re.M)
_LOGIN_URL = re.compile(r"(/|\b)(login|log-in|signin|sign-in|sign_in|servicelogin|auth/|oauth|accounts/)", re.I)
_URLISH = re.compile(r"\S*[/\\]\S*|\S+\.(html?|php|aspx?)\b")  # addresses: "login.html" is not a request
_ASKS_SIGN_IN = re.compile(r"\b(sign|log)\s?(in|on)\b|\blogin\b", re.I)


@dataclass
class Recovery:
    result: ToolResult
    lines: list[str] = field(default_factory=list)
    hint: str = ""
    stop: str = ""          # "NEED_USER: ..." text: the task stops here and asks or reports
    failure: Failure = F.UNKNOWN
    recovered: bool = False

    def note(self) -> str:
        """What the model reads after the tool result."""
        parts = [f"SELF-HEAL: {line}" for line in self.lines if line]
        if self.hint:
            parts.append(f"HINT: {self.hint}")
        return "\n".join(parts)


def retry_safe(name: str, risk: str, failure: Failure) -> bool:
    """May this failed step be tried again automatically? (The retry still passes policy and approvals.)"""
    if name in ALWAYS_SIDE_EFFECT:
        return False
    if risk in SAFE_RISKS:
        return True
    return failure in PRE_ACTION  # the tool did nothing, provably: it found no target and returned


class Healer:
    def __init__(self, cfg: SelfHealConfig, executor: Any, events: Any = None,
                 can_see: Callable[[], bool] = lambda: False, settle_s: float = 1.5,
                 can_ocr: Callable[[], bool] = lambda: False, can_mark: Callable[[], bool] = lambda: False) -> None:
        self.cfg = cfg
        self.executor = executor
        self.events = events
        self.can_see = can_see
        self.can_ocr = can_ocr    # Windows OCR works here: the OCR rung (click_text) is used
        self.can_mark = can_mark  # set-of-marks is available: the vision rung suggests look_with_marks
        self.settle_s = settle_s
        self.engine: Any = None  # TaskEngine (event log), set by bootstrap / the eval runner
        self._task: dict[str, int] = {}
        self._step: dict[tuple[str, str], int] = {}
        self._fails: dict[tuple[str, str, str], int] = {}
        self._rung: dict[tuple[str, str, str], str] = {}
        self._pending: dict[tuple[str, str], tuple[str, str]] = {}  # (task, family) -> (rung, target)
        self._asked_sign_in: set[str] = set()

    # --- bookkeeping ----------------------------------------------------------------------------
    def forget(self, task_id: str) -> None:
        self._task.pop(task_id, None)
        self._asked_sign_in.discard(task_id)
        for d in (self._step, self._pending):
            for k2 in [k for k in d if k[0] == task_id]:
                d.pop(k2, None)
        for d3 in (self._fails, self._rung):
            for k3 in [k for k in d3 if k[0] == task_id]:
                d3.pop(k3, None)

    def attempts(self, task_id: str) -> int:
        return self._task.get(task_id, 0)

    def take(self, task_id: str, step_key: str = "") -> bool:
        """Use one recovery attempt; False when the step's or the task's cap is used up."""
        if self._task.get(task_id, 0) >= self.cfg.max_per_task:
            return False
        if step_key and self._step.get((task_id, step_key), 0) >= self.cfg.max_per_step:
            return False
        self._task[task_id] = self._task.get(task_id, 0) + 1
        if step_key:
            self._step[(task_id, step_key)] = self._step.get((task_id, step_key), 0) + 1
        return True

    async def record(self, task_id: str, failure: Failure, line: str, ok: bool, tool: str = "",
                     **data: Any) -> None:
        """One line in the run timeline (event log) and on the live dashboard."""
        if not line:
            return
        log.info("selfheal.recovery", task=task_id, failure=str(failure), ok=ok, line=line)
        if self.engine is not None:
            self.engine.recovery(task_id, str(failure), line, ok, tool, data)
        if self.events is not None:
            try:
                await self.events.publish("task.recovery", task_id=task_id, failure=str(failure), line=line, ok=ok)
            except Exception:
                log.exception("selfheal.publish_failed")

    def _names(self) -> set[str]:
        return set(self.executor.registry.names())

    async def _exec(self, task_id: str, tool: str, args: dict[str, Any], steps: list[StepRecord],
                    failure: Failure) -> ToolResult | None:
        if tool not in self._names():
            return None
        if self.events is not None:
            try:
                await self.events.publish("task.step", task_id=task_id, tool=tool, args={**args, "auto": True})
            except Exception:
                log.exception("selfheal.publish_failed")
        r: ToolResult = await self.executor.execute(tool, args, task_id=task_id)
        steps.append(StepRecord(tool, dict(args), r.ok, r.output[:2000], {**r.evidence, "recovery": str(failure)}))
        return r

    def _label(self, name: str, args: dict[str, Any]) -> str:
        """The element's name: for a numbered element, the name the agent was shown (still in the tool's
        ref table, captured before any re-read renumbers the page)."""
        ref = args.get("ref")
        if ref is not None:
            tool = self.executor.registry.get(name)
            refs: dict[Any, Any] = getattr(getattr(tool, "mgr", None), "refs", None) or {}
            seen_name = str(getattr(refs.get(ref), "name", "") or "")
            if seen_name:
                return seen_name
        return str(args.get("text") or args.get("field") or args.get("name") or "").strip()

    # --- after a step that worked ---------------------------------------------------------------
    async def after_ok(self, task_id: str, request: str, name: str, args: dict[str, Any],
                       result: ToolResult) -> Recovery | None:
        """A sign-in wall reached by a step that "worked", and which strategy rung finally worked."""
        if not self.cfg.enabled:
            return None
        tool = self.executor.registry.get(name)
        found = strategy.rung_of(name, tool)
        if found is not None and (pending := self._pending.pop((task_id, found[0]), None)) and pending[0] == found[1]:
            await self.record(task_id, F.UNKNOWN, f"{strategy.RUNG_WORDS[found[1]].capitalize()} worked for this step.",
                              True, name, rung=found[1], rung_worked=True)
        if name.startswith("browser_") and task_id not in self._asked_sign_in and (
                wall := self._sign_in_wall(request, result, tool)):
            self._asked_sign_in.add(task_id)
            await self.record(task_id, F.SIGN_IN_WALL, playbook_for(F.SIGN_IN_WALL).line_tried, False, name)
            return Recovery(result, [playbook_for(F.SIGN_IN_WALL).line_tried], stop=wall, failure=F.SIGN_IN_WALL)
        return None

    def _sign_in_wall(self, request: str, result: ToolResult, tool: Any) -> str:
        if _ASKS_SIGN_IN.search(_URLISH.sub(" ", request or "")):
            return ""  # the user asked to sign in: the model handles it (and never types the password)
        url = str(result.evidence.get("url") or "")
        if not (_PASSWORD_FIELD.search(result.output or "") and _LOGIN_URL.search(url)):
            return ""
        return self._sign_in_question(url, tool)

    @staticmethod
    def _sign_in_question(url: str, tool: Any) -> str:
        host = (urlparse(url).hostname or "this site") if url else "this site"
        own = str(getattr(type(tool), "__module__", "")).endswith(".browser")
        helper = (" Delta's own browser profile keeps its own sign-ins: run scripts\\setup_chrome_profile.ps1 once "
                  "to sign in to your sites there." if own else "")
        return (f"NEED_USER: {host} wants you to sign in before I can go on. I never type passwords. Please sign in "
                f"in the browser window, then tell me to carry on.{helper}")

    # --- after a step that failed ---------------------------------------------------------------
    async def recover(self, task_id: str, name: str, args: dict[str, Any], result: ToolResult,
                      steps: list[StepRecord], request: str = "") -> Recovery:
        if not self.cfg.enabled or is_refusal(result.output, result.evidence):
            return Recovery(result)
        failure = classify(name, result.output, result.evidence)
        pb = playbook_for(failure)
        tool = self.executor.registry.get(name)
        if failure == F.UNKNOWN:
            return Recovery(result, failure=failure)
        if pb.stop:
            stop = self._stop_text(failure, name, result, tool)
            await self.record(task_id, failure, pb.line_tried, False, name)
            return Recovery(result, [pb.line_tried], stop=stop, failure=failure)
        if failure == F.CAPTCHA:
            await self.record(task_id, failure, pb.line_tried, False, name)
            return Recovery(result, [pb.line_tried], hint=pb.hint, failure=failure)
        if not self.take(task_id, fingerprint(name, args)):
            return Recovery(result, hint=pb.hint + " (Automatic recovery for this step is used up.)", failure=failure)
        risk = risk_of(tool, args)
        safe = retry_safe(name, risk, failure)
        label = self._label(name, args)
        rec = await self._play(task_id, name, args, result, steps, pb, failure, safe, label)
        if rec.recovered or rec.stop:
            return rec
        down = await self._step_down(task_id, name, args, rec.result, steps, failure, label)
        if down is not None:
            down.lines = rec.lines + down.lines
            return down
        return rec

    def _stop_text(self, failure: Failure, name: str, result: ToolResult, tool: Any) -> str:
        if failure == F.SIGN_IN_WALL:
            return self._sign_in_question(str(result.evidence.get("url") or ""), tool)
        detail = (result.output or "").strip().splitlines()[0][:200] if result.output else ""
        return (f"NEED_USER: Windows did not allow {name.replace('_', ' ')} ({detail}). I stopped instead of trying "
                "another way. If it should be allowed, change the permission or run it yourself, then tell me to "
                "carry on.")

    async def _play(self, task_id: str, name: str, args: dict[str, Any], result: ToolResult,
                    steps: list[StepRecord], pb: Playbook, failure: Failure, safe: bool, label: str) -> Recovery:
        new_args = dict(args)
        can_retry = pb.retry and safe
        done: list[str] = []
        dismissed = ""
        found = strategy.rung_of(name, self.executor.registry.get(name))
        family = found[0] if found else ("web" if name.startswith("browser_") else "desktop")
        for action in pb.actions:
            if action == "settle":
                await asyncio.sleep(self.settle_s * (2 if failure in (F.APP_NOT_RESPONDING, F.FILE_LOCKED) else 1))
            elif action == "resnapshot":
                reader = READ_BACK.get(family or ("web" if name.startswith("browser_") else "desktop"))
                if reader and reader != name and reader in self._names():
                    read_args: dict[str, Any] = {}
                    if reader == "read_window" and args.get("window"):
                        read_args["window"] = args["window"]
                    await self._exec(task_id, reader, read_args, steps, failure)
            elif action == "relocate":
                if args.get("ref") is not None:
                    if not label:
                        can_retry = False
                    else:
                        new_args.pop("ref", None)
                        new_args["field" if name == "browser_fill" else "text"] = label
            elif action == "dismiss_popup":
                dismissed = await self._dismiss(task_id, result.output, steps, failure)
                can_retry = can_retry and bool(dismissed)
            elif action == "reopen":
                can_retry = can_retry and await self._reopen(task_id, result, steps, failure)
            done.append(action)
        if can_retry:
            r2 = await self._exec(task_id, name, new_args, steps, failure)
            if r2 is not None and r2.ok:
                line = pb.line_ok
                if dismissed:
                    line = f"Closed a banner that was in the way ('{dismissed}') and tried again."
                    if "cookie" in result.output.lower() or "cookie" in dismissed.lower():
                        line = f"Closed a cookie banner ('{dismissed}') and tried again."
                await self.record(task_id, failure, line, True, name, actions=done, retried=True)
                return Recovery(r2, [line], failure=failure, recovered=True)
            if r2 is not None:
                result = r2
        line = pb.line_tried
        if pb.retry and not safe:
            line += " Did not retry it on my own, because that step changes something and could happen twice."
        await self.record(task_id, failure, line, False, name, actions=done, retried=can_retry)
        return Recovery(result, [line], hint=pb.hint, failure=failure)

    async def _dismiss(self, task_id: str, output: str, steps: list[StepRecord], failure: Failure) -> str:
        """Close a harmless banner by a reject / not-now / close button. Returns the button's name, or ""."""
        if not self.cfg.dismiss_benign_popups or not {"browser_read", "browser_click"} <= self._names():
            return ""
        if DANGER.search(output or ""):
            return ""
        page = await self._exec(task_id, "browser_read", {}, steps, failure)
        if page is None or not page.ok or not BENIGN_CONTEXT.search(f"{output}\n{page.output}"):
            return ""
        buttons = [(int(m.group(1)), m.group(3).strip()) for m in _ELEMENT.finditer(page.output)]
        choice = None
        for pattern in BENIGN_BUTTONS:
            choice = next(((ref, name) for ref, name in buttons if pattern.match(name) and not DANGER.search(name)),
                          None)
            if choice:
                break
        if choice is None:
            return ""
        click_args = {"ref": choice[0]}
        if not self._allowed_without_asking("browser_click", click_args):
            return ""
        r = await self._exec(task_id, "browser_click", click_args, steps, failure)
        return choice[1] if r is not None and r.ok else ""

    def _allowed_without_asking(self, name: str, args: dict[str, Any]) -> bool:
        from delta.safety.policy import Verdict

        tool = self.executor.registry.get(name)
        if tool is None:
            return False
        try:
            decision = self.executor.policy.evaluate(tool, tool.Args.model_validate(args))
        except Exception:
            return False
        return bool(decision.verdict == Verdict.ALLOW and str(decision.risk) in SAFE_RISKS)

    async def _reopen(self, task_id: str, result: ToolResult, steps: list[StepRecord], failure: Failure) -> bool:
        """After the watchdog restarted Delta's own browser: open the page the task was on again."""
        if result.evidence.get("watchdog") != "browser_dead" or "restarted the Delta browser" not in result.output:
            return False
        url = next((str(s.evidence.get("url")) for s in reversed(steps)
                    if s.ok and str(s.evidence.get("url") or "").startswith(("http://", "https://"))), "")
        if not url:
            return True  # nothing to go back to: the retry starts from the fresh browser
        r = await self._exec(task_id, "browser_open", {"url": url}, steps, failure)
        return bool(r is not None and r.ok)

    # --- strategy ladder ------------------------------------------------------------------------
    async def _step_down(self, task_id: str, name: str, args: dict[str, Any], result: ToolResult,
                         steps: list[StepRecord], failure: Failure, label: str) -> Recovery | None:
        if failure not in (F.ELEMENT_NOT_FOUND, F.STALE_ELEMENT, F.POPUP_OR_DIALOG, F.TIMEOUT):
            return None
        found = strategy.rung_of(name, self.executor.registry.get(name))
        if found is None:
            return None
        family, current = found
        target = strategy.target_of(name, args, label)
        key = (task_id, family, target)
        self._fails[key] = self._fails.get(key, 0) + 1
        if self._fails[key] < self.cfg.strategy_after:
            return None
        self._fails[key] = 0
        rung = self._rung.get(key, current)
        tried = [current]
        while True:
            nxt = strategy.next_rung(family, rung, self.can_see(), self.can_ocr())
            if nxt is None or nxt == strategy.ASK:
                self._rung[key] = strategy.ASK
                words = ", ".join(strategy.RUNG_WORDS[r] for r in dict.fromkeys(tried))
                what = f"'{label}'" if label else "that"
                line = f"Tried {words} for {what} without luck; asked you."
                await self.record(task_id, failure, line, False, name, rung=strategy.ASK)
                return Recovery(result, [line], failure=failure, stop=(
                    f"NEED_USER: I could not {name.replace('browser_', '').replace('_', ' ')} {what}: I tried "
                    f"{words}. Could you do that step yourself, or tell me another way?"))
            self._rung[key] = nxt
            if nxt == strategy.VISION:
                self._pending[(task_id, family)] = (nxt, target)
                line = f"Stepping down to the screen picture for '{label or name}'."
                await self.record(task_id, failure, line, False, name, rung=nxt)
                return Recovery(result, [line], hint=strategy.vision_hint(label or "the control", self.can_mark()),
                                failure=failure)
            translate = strategy.STEP_DOWN.get((name, nxt))
            move = translate(args, {"label": label, "title": self._title(steps)}) if translate else None
            if move is not None and move.tool in self._names() and self.take(task_id):
                new_tool = self.executor.registry.get(move.tool)
                if str(risk_of(new_tool, move.args)) in SAFE_RISKS:
                    tried.append(nxt)
                    r = await self._exec(task_id, move.tool, move.args, steps, failure)
                    if r is not None and r.ok:
                        line = (f"Could not do it through {strategy.RUNG_WORDS[current]}; "
                                f"used {strategy.RUNG_WORDS[nxt]} instead.")
                        await self.record(task_id, failure, line, True, move.tool, rung=nxt, rung_worked=True)
                        return Recovery(r, [line], failure=failure, recovered=True)
            rung = nxt

    @staticmethod
    def _title(steps: list[StepRecord]) -> str:
        for s in reversed(steps):
            if s.evidence.get("title"):
                return str(s.evidence["title"])
        return ""

