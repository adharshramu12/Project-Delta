"""Saved-login requests as a fixed tool flow, with no AI calls: "log in to my college ERP", "try again", and the
characters of a captcha the user was sent.

A plain sign-in needs no planning: which saved login is meant can be read from the request (LoginStore.resolve finds
"Example University ERP" -> the login "euerp" for https://newerp.example-university.edu), and sign_in itself opens
the page, fills the form, handles a simple captcha and checks that the sign-in worked. Running it through the planner
and the agent loop cost a real user 61.7k tokens and 18 AI calls for one failed login; here it costs none.

What it handles (anything else goes to the planner exactly as before):

- A sign-in request that names one saved login and nothing else ("sign in to my EU ERP portal"). A request that
  asks for more ("log in to my ERP and check my timetable") or that fits several logins or none is not taken.
- "try again" / "done, try again now" in the same chat within RETRY_WINDOW_S after such a sign-in did not finish:
  the same sign-in runs again, without re-planning and without the earlier reply steering a model.
- The characters of a captcha Delta sent to this chat (LoginService parked the filled form): typed into the same
  form, which is submitted once and checked. "cancel" drops it. Never logged or audited (Orchestrator.run shows
  "(captcha answer)" instead of the text).

sign_in goes through the ToolExecutor as from the agent: the same policy, approvals, PIN rules, Remote Guard and
audit. Results are fast (nothing is learned from them, no verifier call): the sign-in checks itself.
"""

from __future__ import annotations

import base64
import re
import time
from typing import Any

from delta.core.state import StepRecord, TaskRequest, TaskResult, Verification

POLITE = r"^(?:please\s+|pls\s+|delta[,\s]+|hey\s+delta[,\s]+|can\s+you\s+|could\s+you\s+|now\s+)*"
SIGN_IN = re.compile(POLITE + r"(?:log\s*-?\s*in|login|sign\s*-?\s*in|signin)"
                     r"(?:\s*to\s+|\s+into\s+|\s+on\s+|\s+at\s+|\s+)(?P<what>.+?)"
                     r"(?:\s+(?:now|please|pls|for\s+me))*\s*[.!]*$", re.I)
RETRY = re.compile(r"^(?:(?:ok(?:ay)?|done|now|yes|i\s+did|i\s+saved\s+it|saved)[,.!]?\s+)*(?:please\s+)?"
                   r"(?:try\s+(?:it\s+)?again|retry|again|do\s+it\s+again|try\s+now|try\s+once\s+more|one\s+more\s+time|"
                   r"log\s*in\s+again|sign\s*in\s+again)(?:\s+(?:now|please|pls))*\s*[.!]*$", re.I)
CANCEL = re.compile(r"^(?:cancel|stop|leave\s+it|never\s*mind|no)\s*[.!]*$", re.I)
ANSWER = re.compile(r"^[A-Za-z0-9]{3,10}$")
RETRY_WINDOW_S = 30 * 60
TOOL = "sign_in"


def captcha_text(text: str) -> str | None:
    """'K7Q2' or 'k 7 q 2' -> the characters; None when it does not look like a captcha answer."""
    t = (text or "").strip().strip("\"'`.")
    if len(t) > 16:
        return None
    joined = re.sub(r"\s+", "", t)
    return joined if ANSWER.match(joined) else None


class LoginFlow:
    def __init__(self, service: Any, executor: Any, events: Any = None) -> None:
        self.service = service
        self.executor = executor
        self.events = events
        self._recent: dict[str, tuple[str, float]] = {}  # chat -> (login id, when) of a sign-in that did not finish
        self.clock = time.monotonic

    @staticmethod
    def key(req: TaskRequest) -> str:
        return req.chat_id or f"{req.channel}:{req.user}"

    # --- what it takes ----------------------------------------------------------------------------------
    def is_captcha_answer(self, req: TaskRequest) -> bool:
        return self.service.waiting(self.key(req)) is not None and captcha_text(req.text) is not None

    def _available(self) -> bool:
        tool = self.executor.registry.get(TOOL)
        return tool is not None and bool(tool.available())

    def wanted_login(self, text: str) -> Any | None:
        """The one saved login a plain sign-in request means, else None (the planner takes the request)."""
        t = " ".join((text or "").split())
        if len(t) > 120 or "\n" in (text or ""):
            return None
        m = SIGN_IN.match(t)
        if m is None:
            return None
        login, _why = self.service.store.resolve(m["what"])
        return login

    # --- the flow ---------------------------------------------------------------------------------------
    async def handle(self, req: TaskRequest) -> TaskResult | None:
        key = self.key(req)
        text = (req.text or "").strip()
        if self.service.waiting(key) is not None:
            if CANCEL.match(text):
                parked = self.service.cancel(key)
                login = self.service.store.get(parked.login_id) if parked else None
                return self._result(req, "done", f"OK, I stopped signing in{' to ' + login.name if login else ''}.")
            if (answer := captcha_text(text)) is not None:
                return await self._answer(req, key, answer)
        login = None
        if RETRY.match(text) and (recent := self._recent.get(key)) and self.clock() - recent[1] < RETRY_WINDOW_S:
            login = self.service.store.get(recent[0])
        if login is None:
            login = self.wanted_login(text)
        if login is None or not self._available():
            return None
        return await self._sign_in(req, key, login)

    async def _sign_in(self, req: TaskRequest, key: str, login: Any) -> TaskResult:
        args = {"site": login.name}
        await self._publish("task.plan", task_id=req.id, plan={
            "needs_tools": True, "goal": f"Sign in to {login.name}", "steps": [TOOL], "tools": [TOOL],
            "success_criteria": [f"signed in to {login.origin}"], "direct": {"tool": TOOL, "args": args},
            "fast_path": "saved login"})
        await self._publish("task.step", task_id=req.id, tool=TOOL, args=args)
        result = await self.executor.execute(TOOL, args, task_id=req.id)
        step = StepRecord(TOOL, args, result.ok, result.output[:2000], result.evidence)
        if result.evidence.get("user_denied"):
            return self._result(req, "cancelled", "OK, I didn't sign in.", [step])
        outcome = str(result.evidence.get("outcome") or "")
        return self._finish(req, key, login, result.ok, outcome, result.output, [step])

    async def _answer(self, req: TaskRequest, key: str, answer: str) -> TaskResult:
        parked = self.service.waiting(key)
        login = self.service.store.get(parked.login_id) if parked else None
        await self._publish("task.step", task_id=req.id, tool="sign_in_captcha", args={"login": login.name if login
                                                                                         else ""})
        result = await self.service.answer_captcha(key, answer, req.id)
        step = StepRecord("sign_in_captcha", {}, result.ok, result.message[:2000], {"outcome": result.outcome})
        if login is None:
            return self._result(req, "failed", result.message, [step])
        return self._finish(req, key, login, result.ok, result.outcome, result.message, [step])

    def _finish(self, req: TaskRequest, key: str, login: Any, ok: bool, outcome: str, output: str,
                steps: list[StepRecord]) -> TaskResult:
        message = output.split("\n\n", 1)[0].strip()
        if ok:
            self._recent.pop(key, None)
            check = Verification(True, f"sign_in checked it: {message[:200]}", [],
                                 [{"checkpoint": f"signed in to {login.origin}", "met": True, "seen": message[:300]}])
            where = re.search(r"(?:now at|moved on to) (\S+?)\)?$", message)
            text = f"Signed in to {login.name} ({login.origin})." + (f" The page is now {where[1]}" if where else "")
            if "already signed in" in message:
                text = f"You were already signed in to {login.name} ({login.origin})."
            done = self._result(req, "done", text, steps)
            done.verification = check
            return done
        self._recent[key] = (login.id, self.clock())
        while len(self._recent) > 64:
            self._recent.pop(next(iter(self._recent)))
        image = self.service.last_image.pop(key, None)
        if image is not None and self.service.waiting(key) is not None:
            asked = self._result(req, "needs_user", _plain(message), steps)
            asked.image = base64.b64encode(image).decode()
            return asked
        status = "needs_user" if outcome in ("needs_user", "not_allowed") else "failed"
        return self._result(req, status, f"I could not sign in to {login.name}: {_plain(message)}", steps)

    def _result(self, req: TaskRequest, status: str, answer: str, steps: list[StepRecord] | None = None) -> TaskResult:
        r = TaskResult(req.id, status, answer, 1, steps or [], fast=True)  # type: ignore[arg-type]
        r.provider = "delta"
        return r

    async def _publish(self, event: str, **data: Any) -> None:
        if self.events is not None:
            try:
                await self.events.publish(event, **data)
            except Exception:  # the timeline must never fail a sign-in
                return


def _plain(message: str) -> str:
    """A tool message for a person: without the markers meant for the AI."""
    for marker in ("CAPTCHA: ", "NEEDS HUMAN: ", "REFUSED: ", "STOPPED: "):
        message = message.replace(marker, "")
    return message.replace(" Do not try another way.", "").replace(" Do not retry; tell the user.", "").strip()

