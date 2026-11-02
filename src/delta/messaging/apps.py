"""Drivers for the WhatsApp, Telegram and Slack desktop apps.

Each driver opens the app, finds the person through the app's own search, CONFIRMS the right chat
is open before typing anything, then types (Unicode, so emoji work) and sends. They work through
Windows accessibility names and the keyboard: no screenshots, no AI, no mouse aiming.

How each app is confirmed (from inspecting the real apps):
- WhatsApp: named search box "Search or start a new chat"; results are items whose name starts with
  the contact name; an open chat has a message box.
- Telegram: named "Search" box; results are list items; an open chat has a message box.
- Slack: exposes almost no accessibility tree, but its window title names the open conversation
  ("Slack (DM) - Ravi - Slack"), which is checked after the quick switcher (Ctrl+K).

All methods block (UI Automation is synchronous); call them from a worker thread.
"""

from __future__ import annotations

import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

APPS = ("whatsapp", "telegram", "slack")

# Where a chat-list entry's contact name ends: a date, a time, or a day word.
_NAME_END = re.compile(r"\s+(?:\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{1,2}:\d{2}(?:\s*[ap]m)?|yesterday|today|"
                       r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)


class MessagingError(RuntimeError):
    """The app could not do what was asked; the message says what the user should know."""


class ContactNotFoundError(MessagingError):
    pass


class AmbiguousContactError(MessagingError):
    def __init__(self, query: str, options: list[str]) -> None:
        super().__init__(f"Several chats match '{query}': {', '.join(options)}. Which one?")
        self.options = options


@dataclass
class Sent:
    to: str
    confirmed: bool  # the message was seen in the chat afterwards


def contact_label(item_name: str) -> str:
    """'View status Best Friend. Yougitha 20/10/2026 Chey ra' -> 'Best Friend. Yougitha'."""
    name = re.sub(r"^view status\s+", "", item_name.strip(), flags=re.I)
    return _NAME_END.split(name, maxsplit=1)[0].strip()


def choose_contact(query: str, item_names: list[str]) -> str:
    """The one chat that matches `query`, or a clear error (none / several)."""
    labels = list(dict.fromkeys(contact_label(n) for n in item_names if n.strip()))
    q = query.casefold().strip()
    exact = [lab for lab in labels if lab.casefold() == q]
    if len(exact) == 1:
        return exact[0]
    words = q.split()
    hits = [lab for lab in labels if all(w in lab.casefold() for w in words)]
    starts = [lab for lab in hits if lab.casefold().startswith(q)]
    pick = starts if len(starts) == 1 else hits
    if len(pick) == 1:
        return pick[0]
    if not pick:
        raise ContactNotFoundError(f"No chat found for '{query}'.")
    raise AmbiguousContactError(query, pick[:5])


def _uia() -> Any:
    import pythoncom
    from pywinauto import Desktop

    pythoncom.CoInitialize()
    return Desktop(backend="uia")


def _type(text: str) -> None:
    """Type text as Unicode (emoji, Telugu, Hindi all work); new lines as Shift+Enter, so a
    multi-line message is not sent half-way."""
    from pywinauto.keyboard import send_keys

    from delta.tools.desktop import _send_unicode

    for i, line in enumerate(text.split("\n")):
        if i:
            send_keys("+{ENTER}")
        if line:
            _send_unicode(line)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _value(element: Any) -> str:
    try:
        return str(element.iface_value.CurrentValue or "")
    except Exception:
        try:
            return str(element.window_text() or "")
        except Exception:
            return ""


def _press(keys: str) -> None:
    from pywinauto.keyboard import send_keys

    send_keys(keys)


class AppDriver(ABC):
    name: str = ""
    label: str = ""

    def window(self, wait_s: float = 0) -> Any:
        end = time.monotonic() + wait_s
        while True:
            for w in _uia().windows():
                if self._is_mine(w.window_text()):
                    return w
            if time.monotonic() >= end:
                return None
            time.sleep(0.7)

    @abstractmethod
    def _is_mine(self, title: str) -> bool: ...

    @abstractmethod
    def _launch(self) -> None: ...

    def open_app(self) -> Any:
        w = self.window()
        if w is None:
            self._launch()
            w = self.window(wait_s=25)
        if w is None:
            raise MessagingError(f"{self.label} did not open. Is it installed and signed in?")
        w.set_focus()
        time.sleep(0.4)
        return w

    @abstractmethod
    def open_chat(self, person: str) -> str:
        """Open `person`'s chat and return the confirmed chat name."""

    @abstractmethod
    def send_text(self, text: str) -> bool:
        """Type and send into the open chat; True if the message was seen afterwards."""

    def read(self, count: int = 15) -> list[str]:
        raise MessagingError(f"Reading {self.label} chats is not supported yet.")

    def send(self, person: str, text: str) -> Sent:
        chat = self.open_chat(person)
        return Sent(chat, self.send_text(text))


class _SearchListDriver(AppDriver):
    """WhatsApp and Telegram: a named search box, results as list items, a message box."""

    search_names: tuple[str, ...] = ()
    compose_names: tuple[str, ...] = ()  # the message box, found ONLY by its own name
    result_types: tuple[str, ...] = ("DataItem", "ListItem")

    def _edits(self, w: Any) -> tuple[Any | None, Any | None]:
        """(search box, message box). The message box is matched by name only: once text is typed,
        WhatsApp's search box loses its name, and typing a message into it must never happen."""
        search = compose = unnamed = None
        for e in w.descendants(control_type="Edit"):
            n = (e.element_info.name or "").strip()
            if compose is None and any(n.startswith(c) for c in self.compose_names) and e.is_visible():
                compose = e
            elif search is None and any(n.startswith(s) for s in self.search_names):
                search = e
            elif unnamed is None and not n and e.is_visible():
                unnamed = e  # WhatsApp's search box once it holds text
        return search or unnamed, compose

    def open_chat(self, person: str) -> str:
        w = self.open_app()
        search, _ = self._edits(w)
        if search is None:
            raise MessagingError(f"Could not find {self.label}'s search box.")
        search.click_input()  # a click: Windows may refuse a plain focus request
        time.sleep(0.2)
        _press("^a{BACKSPACE}")
        _type(person)
        time.sleep(2.2)
        items = [e for e in w.descendants() if e.element_info.control_type in self.result_types]
        chat = choose_contact(person, [e.element_info.name or "" for e in items])
        target = next(e for e in items if contact_label(e.element_info.name or "") == chat)
        try:
            target.invoke()
        except Exception:
            target.click_input()
        end = time.monotonic() + 5
        while time.monotonic() < end:  # an open chat has a message box
            if self._edits(w)[1] is not None:
                return chat
            time.sleep(0.5)
        raise MessagingError(f"Opened '{chat}' in {self.label} but no message box appeared.")

    def send_text(self, text: str) -> bool:
        """Type into the chat's message box and send. Enter is pressed ONLY after the text is
        confirmed inside that box: if Windows kept focus elsewhere, the keys would land in another
        app, and Enter there could do anything. Sent = the box empties, as both apps do on send."""
        w = self.open_app()
        _, compose = self._edits(w)
        if compose is None:
            raise MessagingError(f"No open chat in {self.label} to type into.")
        compose.click_input()  # a real click: focus requests can be refused by Windows, clicks are not
        time.sleep(0.3)
        _press("^a{BACKSPACE}")
        _type(text)
        time.sleep(0.4)
        snippet = _norm(text)[:40]
        if snippet not in _norm(_value(compose)):
            if self._has_focus(compose):  # clear a half-typed draft, but never keys into another app
                _press("^a{BACKSPACE}")
            raise MessagingError(f"The text did not reach {self.label}'s message box (another window kept the "
                                 "keyboard), so nothing was sent. Try again with the app in front.")
        _press("{ENTER}")
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if snippet not in _norm(_value(compose)):
                return True
            time.sleep(0.4)
        return False

    @staticmethod
    def _has_focus(element: Any) -> bool:
        try:
            return bool(element.has_keyboard_focus())
        except Exception:
            return False

    def read(self, count: int = 15) -> list[str]:
        w = self.open_app()
        rows = [re.sub(r"\s+", " ", e.element_info.name or "").strip()
                for e in w.descendants(control_type="ListItem")]
        return [r for r in rows if r][-count:]


class WhatsAppDriver(_SearchListDriver):
    name, label = "whatsapp", "WhatsApp"
    search_names = ("Search or start a new chat", "Search")
    compose_names = ("Type a message",)

    def _is_mine(self, title: str) -> bool:
        return title.strip() == "WhatsApp"

    def _launch(self) -> None:
        os.startfile("whatsapp:")


class TelegramDriver(_SearchListDriver):
    name, label = "telegram", "Telegram"
    search_names = ("Search",)
    compose_names = ("Write a message", "Message", "Type a message")

    def _is_mine(self, title: str) -> bool:
        return title.startswith("Telegram")

    def _launch(self) -> None:
        exe = os.path.expandvars(r"%APPDATA%\Telegram Desktop\Telegram.exe")
        os.startfile(exe if os.path.exists(exe) else "tg:")


class SlackDriver(AppDriver):
    name, label = "slack", "Slack"

    def _is_mine(self, title: str) -> bool:
        return title.endswith("- Slack") or title == "Slack"

    def _launch(self) -> None:
        os.startfile("slack:")

    def open_chat(self, person: str) -> str:
        self.open_app()
        _press("^k")
        time.sleep(0.8)
        _type(person)
        time.sleep(1.8)
        _press("{ENTER}")
        end = time.monotonic() + 6
        want = person.casefold()
        while time.monotonic() < end:  # 'Slack (DM) - Ravi Kumar - Slack'
            w = self.window()
            title = w.window_text() if w else ""
            middle = title.rsplit(" - Slack", 1)[0].split(" - ", 1)[-1]
            if want in middle.casefold():
                return middle
            time.sleep(0.6)
        raise MessagingError(f"Slack did not open a conversation with '{person}'. Check the name.")

    def send_text(self, text: str) -> bool:
        self.open_app()  # the message box has focus after switching conversations
        _type(text)
        _press("{ENTER}")
        return False  # Slack shows its messages to nothing we can read: sent, but not confirmed


DRIVERS: dict[str, type[AppDriver]] = {"whatsapp": WhatsAppDriver, "telegram": TelegramDriver, "slack": SlackDriver}


def driver(app: str) -> AppDriver:
    key = app.strip().casefold().replace(" ", "")
    if key not in DRIVERS:
        raise MessagingError(f"'{app}' is not supported: use WhatsApp, Telegram or Slack.")
    return DRIVERS[key]()
