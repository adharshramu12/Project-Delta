"""Messaging tools: send_message, read_messages, chat_for_me.

send_message is EXTERNAL risk: the approval shows the app, the person and the exact text.
chat_for_me is approved once with its limits (who, how long, mood, goal); by default each reply
still asks, and anything sensitive (money, OTPs, passwords, emergencies, "are you a bot?") is never
answered by Delta: it pauses and hands the chat back to the user.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import AliasChoices, BaseModel, Field

from delta.messaging.apps import AmbiguousContactError, MessagingError, driver
from delta.messaging.style import MOODS, STYLE_GUIDE, humanize
from delta.tools.base import Risk, Tool, ToolResult

APP_FIELD = Field(description="whatsapp, telegram or slack", validation_alias=AliasChoices("app", "platform"))
PERSON_FIELD = Field(description="The contact or chat name as it appears in the app",
                     validation_alias=AliasChoices("to", "person", "contact", "recipient", "name"))

# Never answered by Delta: the user decides these themselves.
SENSITIVE = re.compile(
    r"\b(otp|one[- ]time password|password|passcode|pin|cvv|upi|gpay|phonepe|paytm|bank|account (?:no|number)|"
    r"ifsc|transfer|send (?:me )?money|pay(?:ment)?|loan|lend|borrow|rs\.?\s?\d|₹|aadhaar|aadhar|pan card|"
    r"address|location|emergency|hospital|accident|police|are you (?:a )?bot|is this (?:an? )?(?:ai|bot)|"
    r"(?:are|r) (?:you|u) (?:an? )?ai)\b", re.I)


class SendArgs(BaseModel):
    app: str = APP_FIELD
    to: str = PERSON_FIELD
    text: str = Field(min_length=1, max_length=2000, description="The message, written the way the user texts",
                      validation_alias=AliasChoices("text", "message", "msg", "body"))


class SendMessage(Tool[SendArgs]):
    name = "send_message"
    description = ("Send a message on WhatsApp, Telegram or Slack as the user: opens the app, finds the person, "
                   "confirms the chat, types and sends (emoji supported). Write the text the way the user texts: "
                   "short, natural, no AI phrases or long dashes.")
    risk = Risk.EXTERNAL
    Args = SendArgs

    def summary(self, args: SendArgs) -> str:
        return f"send on {args.app} to {args.to}: “{humanize(args.text)}”"

    async def run(self, args: SendArgs) -> ToolResult:
        text = humanize(args.text)
        if not text:
            return ToolResult(False, "The message is empty after clean-up; write it again.")
        try:
            d = driver(args.app)
            sent = await asyncio.to_thread(d.send, args.to, text)
        except AmbiguousContactError as e:
            return ToolResult(False, f"{e} Ask the user which one, then send again with that exact name.",
                              {"options": e.options})
        except MessagingError as e:
            return ToolResult(False, str(e))
        note = "" if sent.confirmed else (" (Slack does not let me read the chat back, so delivery is not "
                                          "double-checked)" if d.name == "slack" else " (not seen in the chat yet)")
        return ToolResult(True, f"Sent on {d.label} to {sent.to}: “{text}”{note}.",
                          {"app": d.name, "to": sent.to, "text": text, "confirmed": sent.confirmed,
                           "verified": sent.confirmed or d.name == "slack"})


class ReadArgs(BaseModel):
    app: str = APP_FIELD
    person: str = PERSON_FIELD
    count: int = Field(default=15, ge=1, le=50)


class ReadMessages(Tool[ReadArgs]):
    name = "read_messages"
    description = "Read the latest messages in a WhatsApp or Telegram chat (to reply in context or summarise)."
    risk = Risk.ACT
    Args = ReadArgs

    async def run(self, args: ReadArgs) -> ToolResult:
        try:
            d = driver(args.app)
            chat = await asyncio.to_thread(d.open_chat, args.person)
            rows = await asyncio.to_thread(d.read, args.count)
        except AmbiguousContactError as e:
            return ToolResult(False, f"{e} Ask the user which one.", {"options": e.options})
        except MessagingError as e:
            return ToolResult(False, str(e))
        return ToolResult(True, f"Last {len(rows)} messages with {chat} on {d.label}:\n" + "\n".join(rows),
                          {"chat": chat, "count": len(rows), "verified": True})


# --- delegated chat ------------------------------------------------------------------------------

REPLY_PROMPT = STYLE_GUIDE + """

The user asked you to chat with {person} for them. Their instructions: {goal}
Reply to the latest message(s) from {person} in the chat below. If nothing new needs an answer, or
the conversation has reached the user's goal, reply with an empty string.
If the other person asks for anything sensitive (money, payments, OTPs, passwords, personal details,
addresses, plans or promises the user did not mention) or asks whether they are talking to a bot,
do NOT answer: set "handoff" to a one-line reason instead.

Reply with JSON only: {{"reply": "<the message, or empty>", "handoff": null or "<reason>"}}"""


@dataclass
class ChatSession:
    app: str
    person: str
    goal: str
    mood: str
    until: float
    approve_each: bool
    sent: list[str] = field(default_factory=list)
    replies: int = 0
    status: str = "running"
    task: asyncio.Task[None] | None = None


class ChatSessions:
    """Running delegated chats. Wired in bootstrap: needs the router (writing replies), the
    executor (approvals for each reply) and the event bus (telling the user what happened)."""

    POLL_S = 20.0
    MAX_REPLIES = 40

    def __init__(self, router: Any, events: Any, audit: Any) -> None:
        self.router, self.events, self.audit = router, events, audit
        self.executor: Any = None
        self.sessions: dict[tuple[str, str], ChatSession] = {}

    def active(self) -> list[ChatSession]:
        return [s for s in self.sessions.values() if s.status == "running"]

    def start(self, s: ChatSession) -> None:
        key = (s.app, s.person.casefold())
        if (old := self.sessions.get(key)) and old.task:
            old.task.cancel()
        self.sessions[key] = s
        s.task = asyncio.create_task(self._run(s), name=f"chat-{s.app}-{s.person}")

    def stop(self, app: str | None = None, person: str | None = None) -> list[ChatSession]:
        stopped = []
        for (a, p), s in self.sessions.items():
            if s.status == "running" and (not app or a == app) and (not person or p == person.casefold()):
                s.status = "stopped"
                if s.task:
                    s.task.cancel()
                stopped.append(s)
        return stopped

    async def _say(self, s: ChatSession, what: str, **data: Any) -> None:
        await self.events.publish("chat." + what, app=s.app, person=s.person, **data)

    async def _run(self, s: ChatSession) -> None:
        d = driver(s.app)
        try:
            await asyncio.to_thread(d.open_chat, s.person)
            seen = set(await asyncio.to_thread(d.read, 30))
            while s.status == "running" and time.monotonic() < s.until and s.replies < self.MAX_REPLIES:
                await asyncio.sleep(self.POLL_S)
                await asyncio.to_thread(d.open_chat, s.person)
                rows = await asyncio.to_thread(d.read, 30)
                new = [r for r in rows if r not in seen and not any(m[:30].casefold() in r.casefold() for m in s.sent)]
                seen.update(rows)
                if not new:
                    continue
                if any(SENSITIVE.search(r) for r in new):
                    await self._handoff(s, "they asked about something sensitive (money, OTP, personal details "
                                           "or whether this is a bot)", new[-1])
                    return
                await self._reply(s, rows)
        except asyncio.CancelledError:
            raise
        except MessagingError as e:
            s.status = "failed"
            await self._say(s, "ended", reason=str(e))
        finally:
            if s.status == "running":
                s.status = "finished"
                await self._say(s, "ended", reason="time limit reached", replies=s.replies)

    async def _reply(self, s: ChatSession, rows: list[str]) -> None:
        from delta.llm.providers import extract_json
        from delta.llm.schemas import LLMError

        prompt = REPLY_PROMPT.format(mood=s.mood, person=s.person, goal=s.goal or "keep the chat going naturally")
        transcript = "CHAT (oldest first):\n" + "\n".join(rows[-15:])
        try:
            resp = await self.router.complete([{"role": "system", "content": prompt},
                                               {"role": "user", "content": transcript}],
                                              task="verify", json_mode=True)
            data = extract_json(resp.content)
        except (LLMError, ValueError):
            return
        if data.get("handoff"):
            await self._handoff(s, str(data["handoff"]), rows[-1] if rows else "")
            return
        text = humanize(str(data.get("reply") or ""))
        if not text:
            return
        if s.approve_each and self.executor is not None:
            result = await self.executor.execute("send_message", {"app": s.app, "to": s.person, "text": text},
                                                 task_id=f"chat-{s.app}-{s.person}")
            if result.evidence.get("user_denied"):
                await self._say(s, "skipped", text=text)
                return
            ok = result.ok
        else:
            sent = await asyncio.to_thread(driver(s.app).send, s.person, text)
            ok = bool(sent)
            self.audit.record("chat.sent", app=s.app, to=s.person, text=text, auto=True)
        if ok:
            s.sent.append(text)
            s.replies += 1
            await self._say(s, "replied", text=text, replies=s.replies)

    async def _handoff(self, s: ChatSession, reason: str, message: str) -> None:
        s.status = "handed_off"
        self.audit.record("chat.handoff", app=s.app, person=s.person, reason=reason)
        await self._say(s, "handoff", reason=reason, message=message[:200])


class ChatArgs(BaseModel):
    app: str = Field(default="", description="whatsapp, telegram or slack",
                     validation_alias=AliasChoices("app", "platform"))
    person: str = Field(default="", description="The contact or chat name as it appears in the app",
                        validation_alias=AliasChoices("person", "to", "contact", "recipient", "name"))
    goal: str = Field(default="", description="What the user wants from this chat, in their words")
    mood: str = Field(default="friendly", description="|".join(MOODS))
    minutes: int = Field(default=30, ge=5, le=120)
    approve_each: bool = Field(default=True, description="Ask the user before each reply (default)")
    stop: bool = Field(default=False, description="Stop chatting with this person instead")


class ChatForMe(Tool[ChatArgs]):
    name = "chat_for_me"
    description = ("Reply to someone as the user for a while (WhatsApp/Telegram/Slack), in the user's own texting "
                   "style and the asked mood. Approved once with its limits; by default each reply also asks. "
                   "Hands the chat back for anything sensitive. stop=true ends it.")
    risk = Risk.EXTERNAL
    Args = ChatArgs

    def __init__(self, sessions: ChatSessions) -> None:
        self.sessions = sessions

    def risk_for(self, args: ChatArgs) -> Risk:
        return Risk.ACT if args.stop else Risk.EXTERNAL

    def summary(self, args: ChatArgs) -> str:
        if args.stop:
            return f"stop chatting with {args.person or 'everyone'}" + (f" on {args.app}" if args.app else "")
        each = "asking you before each reply" if args.approve_each else "sending replies WITHOUT asking each time"
        return (f"reply as you to {args.person} on {args.app} for {args.minutes} min, mood {args.mood}, {each}"
                + (f"; goal: {args.goal}" if args.goal else ""))

    async def run(self, args: ChatArgs) -> ToolResult:
        app = args.app.strip().casefold()
        if args.stop:
            stopped = self.sessions.stop(app or None, args.person or None)
            names = ", ".join(f"{s.person} ({s.app})" for s in stopped)
            return ToolResult(True, f"Stopped chatting with {names}." if stopped else "I wasn't chatting with anyone.",
                              {"verified": True, "stopped": len(stopped)})
        if not app or not args.person:
            return ToolResult(False, "Say which app (WhatsApp, Telegram or Slack) and who to chat with.")
        try:
            driver(app)
        except MessagingError as e:
            return ToolResult(False, str(e))
        mood = args.mood if args.mood in MOODS else "friendly"
        self.sessions.start(ChatSession(app, args.person, args.goal, mood, time.monotonic() + args.minutes * 60,
                                        args.approve_each))
        each = "I'll ask you before each reply" if args.approve_each else "I'll reply without asking each time"
        return ToolResult(True, f"Chatting with {args.person} on {app} as you for {args.minutes} min ({mood}). {each}, "
                                "and I'll stop and tell you if they ask about money, OTPs, passwords or personal "
                                "details. Say 'stop chatting' any time.",
                          {"verified": True, "session": f"{app}:{args.person}"})


def messaging_tools(sessions: ChatSessions) -> list[Tool]:
    import sys

    return [SendMessage(), ReadMessages(), ChatForMe(sessions)] if sys.platform == "win32" else []
