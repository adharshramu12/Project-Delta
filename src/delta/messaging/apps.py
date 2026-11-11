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


def _scope(w: Any) -> Any:
    """Where to look for controls: the app's web page when it has one, else the window.

    WhatsApp for Windows is now a web view. Walking its whole window visits the same page about 80 times (the
    wrappers around it repeat), about 15,000 controls and 17 seconds per look, far longer than any wait here.
    Asking Windows for the page itself takes a few hundredths of a second, and the page has about 200 controls.
    Apps with no web page (Telegram) are searched as before."""
    try:
        from pywinauto.controls.uiawrapper import UIAWrapper
        from pywinauto.uia_defines import IUIA
        from pywinauto.uia_element_info import UIAElementInfo

        ui = IUIA()
        is_page = ui.iuia.CreatePropertyCondition(ui.UIA_dll.UIA_ControlTypePropertyId,
                                                  ui.UIA_dll.UIA_DocumentControlTypeId)
        found = w.element_info.element.FindFirst(ui.UIA_dll.TreeScope_Descendants, is_page)
        return UIAWrapper(UIAElementInfo(found)) if found else w
    except Exception:
        return w


def _controls(w: Any, control_type: str | None = None) -> list[Any]:
    """Every control in the app's page (or only those of one type). A list that is redrawing can lose a row in
    the middle of the walk, and the walk then fails: look again, a few times, before giving up."""
    last: Exception | None = None
    for _ in range(4):
        try:
            scope = _scope(w)
            return list(scope.descendants(control_type=control_type) if control_type else scope.descendants())
        except Exception as e:  # pywinauto raises KeyError, COMError and others for a control that is gone
            last = e
            time.sleep(0.4)
    raise MessagingError(f"Could not read the app's window ({type(last).__name__}). Nothing was typed or sent.")


# Country calling codes for a number written without one, by the PC's region (Windows Settings > Region).
CALLING_CODES = {"IN": "91", "US": "1", "CA": "1", "GB": "44", "AE": "971", "AU": "61", "SG": "65", "DE": "49",
                 "FR": "33", "PK": "92", "BD": "880", "LK": "94", "NP": "977", "SA": "966", "MY": "60", "ID": "62",
                 "NG": "234", "ZA": "27", "BR": "55", "QA": "974", "KW": "965", "OM": "968", "IT": "39", "ES": "34"}
_PHONE = re.compile(r"^\+?[\d\s().-]{7,20}$")


def region_code() -> str:
    """The PC's region as two letters ('IN'), or '' when Windows does not say."""
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(8)
        return buf.value.upper() if ctypes.windll.kernel32.GetUserDefaultGeoName(buf, 8) else ""
    except Exception:
        return ""


def phone_number(person: str, region: str | None = None) -> str | None:
    """'+91 98765 43210' / '9876543210' -> '919876543210' (digits with the country code), or None when `person` is
    a name. A number with no country code gets the PC's region's code; MessagingError when that is unknown."""
    text = person.strip()
    if not _PHONE.match(text):
        return None
    digits = re.sub(r"\D", "", text)
    if not 7 <= len(digits) <= 15:
        return None
    if text.startswith("+"):
        return digits
    if digits.startswith("00"):
        return digits[2:]
    if digits.startswith("0"):  # a local number written with its leading 0 (098765 43210)
        digits = digits.lstrip("0")
    elif len(digits) > 10:  # already has its country code (919876543210)
        return digits
    code = CALLING_CODES.get(region_code() if region is None else region, "")
    if not code:
        raise MessagingError(f"'{person}' has no country code. Give the number with it, like +91 98765 43210.")
    return code + digits


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
    def send_text(self, text: str, chat: str = "") -> bool:
        """Type and send into the open chat; True if the message was seen afterwards. `chat` is the chat it must
        go to (default: the one open_chat opened last); a driver that can read the open chat's name checks it."""

    def read(self, count: int = 15) -> list[str]:
        raise MessagingError(f"Reading {self.label} chats is not supported yet.")

    def send(self, person: str, text: str) -> Sent:
        chat = self.open_chat(person)
        return Sent(chat, self.send_text(text, chat))


class _SearchListDriver(AppDriver):
    """WhatsApp and Telegram: a named search box, results as list items, a message box."""

    search_names: tuple[str, ...] = ()
    compose_names: tuple[str, ...] = ()  # the message box, found ONLY by its own name
    result_types: tuple[str, ...] = ("DataItem", "ListItem")
    opened: str = ""  # the chat open_chat confirmed last
    LOAD_WAIT_S = 40  # a cold start shows a loading screen first

    def box_chat(self, compose: Any) -> str:
        """The chat the message box says it writes to ('Type a message to Ravi' -> 'Ravi'), or '' when the app's
        box does not say."""
        return ""

    def check_chat(self, w: Any, chat: str) -> Any:
        """The message box, only if the open chat is `chat`. Raises MessagingError otherwise, before a single key
        is typed. A message to the wrong person cannot be taken back, so "probably right" is never enough: where
        the app names the open chat, the name must match."""
        _, compose = self._edits(w)
        if compose is None:
            raise MessagingError(f"No open chat in {self.label} to type into.")
        if not chat:
            return compose
        named = self.box_chat(compose)
        if named and _norm(named) != _norm(chat):
            raise MessagingError(f"{self.label} has the chat '{named}' open, not '{chat}'. Nothing was typed or sent.")
        return compose

    def _edits(self, w: Any) -> tuple[Any | None, Any | None]:
        """(search box, message box). The message box is matched by name only: once text is typed,
        WhatsApp's search box loses its name, and typing a message into it must never happen."""
        search = compose = unnamed = None
        for e in _controls(w, "Edit"):
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
        end = time.monotonic() + self.LOAD_WAIT_S
        while search is None and time.monotonic() < end:  # just started: still loading its chats
            time.sleep(1.0)
            search, _ = self._edits(w)
        if search is None:
            raise MessagingError(f"Could not find {self.label}'s search box after {self.LOAD_WAIT_S} seconds. Is "
                                 f"{self.label} signed in and showing your chats?")
        search.click_input()  # a click: Windows may refuse a plain focus request
        time.sleep(0.2)
        _press("^a{BACKSPACE}")
        _type(person)
        self.opened = ""
        time.sleep(1.5)
        # The list redraws while results arrive, and a row read a moment ago can be showing another chat by the
        # time it is clicked. So: wait until two looks in a row give the same list, then pick from that list.
        end = time.monotonic() + 8
        before: list[str] | None = None
        while True:
            items = [e for e in _controls(w) if e.element_info.control_type in self.result_types]
            names = [e.element_info.name or "" for e in items]
            if names == before or time.monotonic() >= end:
                break
            before = names
            time.sleep(0.6)
        chat = choose_contact(person, names)
        target = next(e for e in items if contact_label(e.element_info.name or "") == chat)
        if contact_label(target.element_info.name or "") != chat:  # read again at the last moment
            raise MessagingError(f"{self.label}'s list changed while opening '{chat}'. Nothing was typed or sent.")
        target.click_input()
        end = time.monotonic() + 8
        problem = f"Opened '{chat}' in {self.label} but no message box appeared."
        while True:  # the chat is open only when the message box is there AND, where the app says, names this chat
            try:
                self.check_chat(w, chat)
                self.opened = chat
                return chat
            except MessagingError as e:
                problem = str(e) if "open, not" in str(e) or "Could not confirm" in str(e) else problem
            if time.monotonic() >= end:
                raise MessagingError(problem)
            time.sleep(0.4)

    def _wait_for_message_box(self, w: Any, seconds: float) -> bool:
        """An open chat has a message box. At least one look, however long a look takes."""
        end = time.monotonic() + seconds
        while True:
            if self._edits(w)[1] is not None:
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.4)

    def send_text(self, text: str, chat: str = "") -> bool:
        """Type into the chat's message box and send. Enter is pressed ONLY after the text is
        confirmed inside that box: if Windows kept focus elsewhere, the keys would land in another
        app, and Enter there could do anything. Sent = the box empties, as both apps do on send.
        The open chat is checked against `chat` before typing and again before Enter."""
        w = self.open_app()
        chat = chat or self.opened
        compose = self.check_chat(w, chat)
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
        try:
            self.check_chat(w, chat)  # still the same chat, now that the text is in the box
        except MessagingError:
            if self._has_focus(compose):
                _press("^a{BACKSPACE}")
            raise
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
                for e in _controls(w, "ListItem")]
        return [r for r in rows if r][-count:]


class WhatsAppDriver(_SearchListDriver):
    name, label = "whatsapp", "WhatsApp"
    search_names = ("Search or start a new chat", "Search", "Ask Meta AI or Search")
    compose_names = ("Type a message", "Type to ")
    BOX_CHAT = re.compile(r"^(?:Type a message to|Type to)\s+(.+)$", re.S)

    def box_chat(self, compose: Any) -> str:
        m = self.BOX_CHAT.match((compose.element_info.name or "").strip())
        return m.group(1).strip() if m else ""

    def check_chat(self, w: Any, chat: str) -> Any:
        """WhatsApp's message box names its chat ('Type a message to Ravi'). A box that names nobody (an older
        WhatsApp) is accepted only when the chat's own heading shows the name."""
        compose = super().check_chat(w, chat)
        if chat and not self.box_chat(compose):
            heads = (e.element_info.name or "" for e in _controls(w, "Text"))
            if not any(_norm(h) == _norm(chat) for h in heads):
                raise MessagingError(f"Could not confirm that '{chat}' is the open WhatsApp chat. Nothing was typed "
                                     "or sent.")
        return compose
    TITLE = re.compile(r"^(?:\(\d+\+?\)\s*)?WhatsApp(?: Beta)?$")  # "(3) WhatsApp" with unread chats

    def _is_mine(self, title: str) -> bool:
        return bool(self.TITLE.match(title.strip()))

    def _launch(self) -> None:
        os.startfile("whatsapp:")

    def open_chat(self, person: str) -> str:
        """A name goes through WhatsApp's search. A phone number opens its chat directly with WhatsApp's own
        link, which also works for a number that is not in the contacts (search finds only saved people)."""
        number = phone_number(person)
        if number is None:
            return super().open_chat(person)
        self.opened = ""
        was = ""  # the chat that is open now, so a chat left open from before is never mistaken for the new one
        if (w := self.window()) is not None and (box := self._edits(w)[1]) is not None:
            was = self.box_chat(box)
        os.startfile(f"whatsapp://send?phone={number}")
        w = self.window(wait_s=25)
        if w is None:
            raise MessagingError("WhatsApp did not open. Is it installed and signed in?")
        w.set_focus()
        end = time.monotonic() + 12
        while True:
            box = self._edits(w)[1]
            named = self.box_chat(box) if box is not None else ""
            # The box names the number itself (not in the contacts) or the contact's name (then it must be a chat
            # that was not open before the link). Anything else is not proof, and nothing is typed without proof.
            if named and (re.sub(r"\D", "", named).endswith(number[-7:]) or _norm(named) != _norm(was)):
                self.opened = named
                return named
            if time.monotonic() >= end:
                break
            time.sleep(0.4)
        raise MessagingError(f"Could not confirm that WhatsApp opened the chat for +{number}. The number may not be "
                             "on WhatsApp, or it needs its country code (like +91 98765 43210). If the person is "
                             "in your contacts, give their name instead. Nothing was typed or sent.")

    CALL_BUTTON = re.compile(r"^(voice|audio) call$", re.I)

    def call(self, person: str) -> str:
        """Open the person's chat (confirmed, as for sending) and press WhatsApp's voice-call button."""
        chat = self.open_chat(person)
        w = self.open_app()
        buttons = (e for e in _controls(w) if e.element_info.control_type == "Button")
        button = next((e for e in buttons if self.CALL_BUTTON.match(e.element_info.name or "")), None)
        if button is None:
            raise MessagingError(f"Opened {chat}'s chat but found no voice-call button; the message was still sent.")
        try:
            button.invoke()
        except Exception:
            button.click_input()
        return chat


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

    def send_text(self, text: str, chat: str = "") -> bool:
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
