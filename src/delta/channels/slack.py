"""Slack channel over Socket Mode (outbound WebSocket, no public URL). Direct messages only,
allow-listed member ids only. Approvals are DM'd with Approve/Deny buttons.

Remote Guard applies centrally: while remote control is locked, Container.submit refuses Slack
requests and ApprovalBroker turns a Slack button press into a "no". Slack cannot take the PIN, so actions
that need it are done from the PC (or from Telegram after /unlock)."""

from __future__ import annotations

from typing import Any

from delta.bootstrap import Container
from delta.channels.base import approval_text, chunks, is_allowed, result_text
from delta.core.state import TaskRequest
from delta.safety.approvals import ApprovalRequest
from delta.system.logging import log


class SlackChannel:
    name = "slack"

    def __init__(self, c: Container, bot_token: str, app_token: str, allowed: list[str]) -> None:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_bolt.async_app import AsyncApp

        self.c = c
        self.allowed = allowed
        self.app = AsyncApp(token=bot_token)
        self.handler = AsyncSocketModeHandler(self.app, app_token)
        self.app.event("message")(self._on_message)
        self.app.action("delta_approve")(self._on_action)
        self.app.action("delta_deny")(self._on_action)
        self.app.action("delta_approve_task")(self._on_action)

    async def start(self) -> None:
        self.c.approvals.add_presenter(self.name, self._present)
        await self.handler.connect_async()
        log.info("slack.started")

    async def stop(self) -> None:
        await self.handler.close_async()

    async def send_text(self, user: str, text: str) -> None:
        """A message Delta starts itself (a scheduled task's result, a reminder) in an allowed user's DM."""
        if not is_allowed(user, self.allowed):
            raise ValueError("not an allowed Slack user")
        dm = await self.app.client.conversations_open(users=user)
        for part in chunks(text):
            await self.app.client.chat_postMessage(channel=dm["channel"]["id"], text=part)

    async def _on_message(self, event: dict[str, Any], say: Any) -> None:
        if event.get("bot_id") or event.get("subtype") or event.get("channel_type") != "im":
            return
        user = event.get("user", "")
        if not is_allowed(user, self.allowed):
            log.warning("slack.rejected", user=user)
            return
        from delta.control import stop_command

        if (reply := stop_command(self.c, event.get("text", ""), by=f"slack:{user}")) is not None:
            await say(reply)  # "stop" / "stop all": never queued behind the task it should stop
            return
        res = await self.c.submit(TaskRequest(event.get("text", ""), channel=self.name, user=user))
        for part in chunks(result_text(res)):
            await say(part)

    async def _present(self, req: ApprovalRequest) -> None:
        buttons: list[dict[str, Any]] = [
            {"type": "button", "style": "primary", "action_id": "delta_approve", "value": req.id,
             "text": {"type": "plain_text", "text": "Approve"}}]
        if req.for_task:  # offered only for actions it may cover (see ToolExecutor.task_grant_key)
            buttons.append({"type": "button", "action_id": "delta_approve_task", "value": req.id,
                            "text": {"type": "plain_text", "text": "Allow for this task"}})
        buttons.append({"type": "button", "style": "danger", "action_id": "delta_deny", "value": req.id,
                        "text": {"type": "plain_text", "text": "Cancel" if req.can_edit else "Deny"}})
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "plain_text", "text": approval_text(req)}},
            {"type": "actions", "elements": buttons},
        ]
        for uid in self.allowed:
            dm = await self.app.client.conversations_open(users=uid)
            await self.app.client.chat_postMessage(channel=dm["channel"]["id"], text=approval_text(req),
                                                   blocks=blocks)

    async def _on_action(self, ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        user = body["user"]["id"]
        if not is_allowed(user, self.allowed):
            return
        action = body["actions"][0]
        approved = action["action_id"] in ("delta_approve", "delta_approve_task")
        # Remote Guard (lock, PIN to approve) is checked inside the broker, for every channel alike.
        settled, verdict = self.c.approvals.answer(action["value"], approved, by=f"slack:{user}",
                                                   for_task=action["action_id"] == "delta_approve_task")
        if not settled and verdict != "already answered or expired":
            await client.chat_postMessage(channel=body["channel"]["id"], text=verdict)  # buttons stay
            return
        await client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"],
                                 text=f"{body['message'].get('text', '')}\n→ {verdict}", blocks=[])
