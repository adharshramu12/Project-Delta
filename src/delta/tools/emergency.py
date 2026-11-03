"""Emergency help: when the user is unwell or in trouble, Delta can alert the people they chose.

The user saves emergency contacts once (Settings → Emergency contacts); they are stored encrypted with
DPAPI. In a crisis Delta writes a short message in the user's name and sends it on WhatsApp to those
contacts, and can start a WhatsApp voice call. It ALWAYS asks first, whatever the autonomy level: a
false alarm to someone's family is a real harm too, and the user must know what was said.

Delta is not an emergency service. For anything life-threatening the user is told to call 112.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pydantic import BaseModel, Field

from delta.system.vault import seal, unseal
from delta.tools.base import Risk, Tool, ToolResult

MAX_CONTACTS = 5


class Contact(BaseModel):
    name: str = Field(min_length=1, max_length=60, description="How the user calls them, e.g. 'Amma'")
    whatsapp: str = Field(min_length=1, max_length=60,
                          description="The name as saved in WhatsApp (or the phone number)")
    relation: str = Field(default="", max_length=40)


class EmergencyContacts:
    """The user's emergency contacts, kept in the data folder and encrypted at rest."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "emergency.json"

    def all(self) -> list[Contact]:
        try:
            raw = json.loads(unseal(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return []
        return [Contact(**c) for c in raw if isinstance(c, dict)][:MAX_CONTACTS]

    def save(self, contacts: list[Contact]) -> None:
        if len(contacts) > MAX_CONTACTS:
            raise ValueError(f"At most {MAX_CONTACTS} emergency contacts.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(seal(json.dumps([c.model_dump() for c in contacts])), encoding="utf-8")


class AlertArgs(BaseModel):
    message: str = Field(min_length=5, max_length=600,
                         description="What is happening, written as the user, short and clear, e.g. "
                                     "'I'm feeling very dizzy and alone at home. Please call me or come.'")
    contacts: list[str] = Field(default_factory=list,
                                description="Names of the emergency contacts to alert; empty = all of them")
    call: bool = Field(default=False, description="Also start a WhatsApp voice call to the first contact")


class AlertEmergencyContacts(Tool[AlertArgs]):
    name = "alert_emergency_contacts"
    description = ("EMERGENCY ONLY: send the user's message on WhatsApp to their saved emergency contacts, and "
                   "optionally start a WhatsApp voice call. Always asks the user first. Not a replacement for 112.")
    risk = Risk.EXTERNAL
    always_ask = True  # even in autopilot: see the module docstring
    Args = AlertArgs

    def __init__(self, store: EmergencyContacts) -> None:
        self.store = store

    def _chosen(self, args: AlertArgs) -> list[Contact]:
        saved = self.store.all()
        if not args.contacts:
            return saved
        wanted = {n.strip().lower() for n in args.contacts}
        return [c for c in saved if c.name.lower() in wanted or c.whatsapp.lower() in wanted]

    def summary(self, args: AlertArgs) -> str:
        who = ", ".join(c.name for c in self._chosen(args)) or "nobody (no matching emergency contact)"
        call = f" and CALL {self._chosen(args)[0].name}" if args.call and self._chosen(args) else ""
        return f"🆘 send on WhatsApp to {who}{call}: “{args.message}”"

    async def run(self, args: AlertArgs) -> ToolResult:
        from delta.messaging.apps import MessagingError, driver

        people = self._chosen(args)
        if not people:
            return ToolResult(False, "No emergency contacts are saved (or none match). Tell the user to add them in "
                                     "Delta → Settings → Emergency contacts, and to call 112 if it is serious.")
        wa = driver("whatsapp")
        sent, failed = [], []
        for c in people:
            try:
                await asyncio.to_thread(wa.send, c.whatsapp, args.message + "\n(sent by Delta, my assistant)")
                sent.append(c.name)
            except MessagingError as e:
                failed.append(f"{c.name}: {e}")
        called = ""
        if args.call:
            try:
                await asyncio.to_thread(wa.call, people[0].whatsapp)  # type: ignore[attr-defined]
                called = people[0].name
            except MessagingError as e:
                failed.append(f"call {people[0].name}: {e}")
        out = (f"Sent to {', '.join(sent)}." if sent else "Nothing was sent.") + (
            f" Calling {called} on WhatsApp." if called else "") + (f" Problems: {'; '.join(failed)}" if failed else "")
        return ToolResult(bool(sent or called), out, {"sent": sent, "called": called, "failed": failed,
                                                      "verified": bool(sent) and not failed})
