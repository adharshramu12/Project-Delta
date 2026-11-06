"""The phone menu: one list of commands for every chat channel, and old chats from the phone.

`COMMANDS` is the single source for Telegram's "/" menu (setMyCommands), the grouped /help reply and the typed
menu of text-only channels (WhatsApp: "menu" or "help"). A command is listed only when a channel really handles
it; tests check the Telegram list against the bot's registered handlers both ways.

`PhoneChats` gives a person on a channel their own conversations: the current one, a fresh one (/new), the
recent ones (/chats), reopening one (/open) and searching them (/find). Only that person's chats: never the PC's
own chats from the dashboard and never another paired user's. Like every phone command, nothing works while remote
control is locked (the channel checks that first); the PIN rules for what a task then does are unchanged.

`handle_text` is the same menu and chat commands for a text-only channel, where a command is just a typed word
("chats", "open 2", "find wifi code"). Delta has no WhatsApp control channel yet, so nothing calls it for
WhatsApp today: it is ready, tested, and formatted for WhatsApp (*bold*, no HTML) for when one is added.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from html import escape
from typing import Any, Literal

Style = Literal["telegram", "whatsapp"]

GROUPS = ("Chats", "Tasks", "Your PC", "Apps and sites", "AI and settings", "Security", "Help")


@dataclass(frozen=True)
class Command:
    name: str
    desc: str  # short, plain words (Telegram shows it next to the command)
    group: str
    usage: str = ""  # example arguments for /help ("2", "wifi code")
    text: bool = False  # also a typed command on text-only channels (handle_text)


COMMANDS: tuple[Command, ...] = (
    Command("new", "Start a fresh chat", "Chats", text=True),
    Command("chats", "Your recent chats", "Chats", text=True),
    Command("open", "Continue an old chat", "Chats", "2", text=True),
    Command("find", "Search your past chats", "Chats", "wifi code", text=True),
    Command("tasks", "Recent tasks, resume or discard", "Tasks"),
    Command("stop", "Stop the running task", "Tasks", text=True),
    Command("undo", "Put back files a task changed (PIN)", "Tasks"),
    Command("screen", "Screenshot of the PC (PIN)", "Your PC"),
    Command("camera", "Webcam photo (PIN)", "Your PC"),
    Command("windows", "Open windows, tap to close", "Your PC"),
    Command("volume", "PC volume", "Your PC", "30"),
    Command("brightness", "Screen brightness", "Your PC", "70"),
    Command("theme", "Windows light or dark mode", "Your PC", "dark"),
    Command("apps", "Connected apps like Notion, GitHub", "Apps and sites"),
    Command("connect", "Connect an app (PIN)", "Apps and sites", "notion"),
    Command("disconnect", "Remove a connected app (PIN)", "Apps and sites", "notion"),
    Command("logins", "Saved site logins, names only", "Apps and sites"),
    Command("watchers", "Sites I check for you", "Apps and sites"),
    Command("signin", "Open my browser on the PC to sign in (PIN)", "Apps and sites"),
    Command("ai", "See or switch the main AI", "AI and settings"),
    Command("model", "Pick the model", "AI and settings"),
    Command("mode", "Permission level", "AI and settings"),
    Command("browser", "Which browser I use (PIN to switch)", "AI and settings"),
    Command("keys", "Replace an API key (PIN)", "AI and settings"),
    Command("unlock", "Enter your PIN for 15 minutes", "Security"),
    Command("lock", "Lock remote control now", "Security"),
    Command("status", "PC health and AIs ready", "Help", text=True),
    Command("help", "All commands by area", "Help", text=True),
    Command("start", "Start or pair this chat", "Help"),
)

TEXT_ALIASES = {"menu": "help", "commands": "help"}  # typed words that mean the same as a command
NOTE = {
    "Tasks": "Send /stop all to stop everything waiting too; a plain \"stop\" works as well.\n"
             "Schedules: just say \"every day at 9 am check my attendance\" or \"what's scheduled?\"",
}


def telegram_menu() -> list[tuple[str, str]]:
    """Telegram's "/" menu, in group order."""
    return [(c.name, c.desc) for c in COMMANDS]


def _bold(text: str, style: Style) -> str:
    return f"<b>{escape(text)}</b>" if style == "telegram" else f"*{text}*"


def _esc(text: str, style: Style) -> str:
    return escape(text, quote=False) if style == "telegram" else text


def help_text(style: Style = "telegram") -> str:
    """The commands grouped by area. Telegram: every command, HTML. Text-only channels (WhatsApp): only the typed
    commands they handle, *bold* and no slashes."""
    lines = []
    if style == "telegram":
        lines.append(_bold("Just type or send a voice note", style) + " (Telugu, Hindi or English) and I will do it on "
                     "your PC. Photos and files (PDF, Word, PowerPoint, text) work too: add a caption saying what you "
                     "want.")
    else:
        lines.append(_bold("Delta menu", style) + "\nType what you want done on your PC, or send one of these:")
    for group in GROUPS:
        cmds = [c for c in COMMANDS if c.group == group and (style == "telegram" or c.text)]
        if style != "telegram" and group == "Help":
            cmds = [c for c in cmds if c.name != "help"] + [Command("menu", "This list", "Help")]
        if not cmds:
            continue
        lines.append("")
        lines.append(_bold(group, style))
        for c in cmds:
            name = f"/{c.name}" if style == "telegram" else c.name
            example = f" {c.usage}" if c.usage and c.name in ("open", "find", "volume", "connect") else ""
            lines.append(_esc(f"{name}{example} - {c.desc}", style))
        if style == "telegram" and group in NOTE:
            lines.append(_esc(NOTE[group], style))
    if style == "telegram":
        lines += ["", _esc("Your PIN is set on the PC (Settings, Remote security). Commands marked PIN ask for it.",
                           style)]
    return "\n".join(lines)


def text_command(text: str) -> tuple[str, str] | None:
    """A typed command on a text-only channel: ("open", "2") for "open 2" or "/open 2". None for anything else,
    which is a normal request for the AI. Plain words are read narrowly so requests stay requests: "stop the music",
    "open notepad" and "find me a flight" are not commands; "stop", "open 2" and "find wifi code" are."""
    raw = text.strip()
    words = raw.split(maxsplit=1)
    if not words:
        return None
    slash = raw.startswith("/")
    head = TEXT_ALIASES.get(words[0].lower().lstrip("/"), words[0].lower().lstrip("/"))
    rest = words[1].strip() if len(words) > 1 else ""
    if head not in {c.name for c in COMMANDS if c.text}:
        return None
    if head in ("new", "chats", "stop", "status", "help") and rest:
        return None
    if head == "open" and rest and not _ref(rest):
        return None
    if head == "find" and not slash and rest and (len(rest.split()) > 3 or rest.split()[0].lower() in _REQUEST_WORDS):
        return None
    return head, rest


_REQUEST_WORDS = frozenset({"me", "my", "the", "a", "an", "all", "out", "some", "any", "where", "how", "which", "what",
                            "files", "file", "folder"})


def _ref(arg: str) -> bool:
    """A chat reference: a list number or a short chat id."""
    arg = arg.strip().lstrip("#")
    return arg.isdigit() or bool(re.fullmatch(r"[0-9a-f]{4,12}", arg))


# --- old chats ----------------------------------------------------------------------------------------------
_ID_TITLE = re.compile(r"^(Telegram|WhatsApp) -?\d+$")


def shown_title(title: str) -> str:
    """A phone chat's internal title ("Telegram 6057519957") is never shown: it reads "Telegram chat"."""
    m = _ID_TITLE.match(title)
    return f"{m.group(1)} chat" if m else title


def when(ts: float, now: float | None = None) -> str:
    t, n = time.localtime(ts), time.localtime(now or time.time())
    if t[:3] == n[:3]:
        return f"today {t.tm_hour:02d}:{t.tm_min:02d}"
    if time.localtime((now or time.time()) - 86400)[:3] == t[:3]:
        return "yesterday"
    return time.strftime("%d %b" if t.tm_year == n.tm_year else "%d %b %Y", t).lstrip("0")


class PhoneChats:
    """One channel's chats, per person. The current chat is that person's most recently used one, so it survives
    a restart with nothing extra stored; /open makes an old chat the most recent again."""

    LIST_SIZE = 8

    def __init__(self, c: Any, channel: str) -> None:
        self.c = c
        self.channel = channel
        self.label = {"telegram": "Telegram", "whatsapp": "WhatsApp"}.get(channel, channel.title())
        self._listed: dict[str, list[str]] = {}  # owner -> chat ids of the last /chats, for "/open 2"

    @property
    def store(self) -> Any:
        return getattr(self.c, "chats", None)

    def owner(self, uid: str) -> str:
        return f"{self.channel}:{uid}"

    def current(self, uid: str) -> str:
        """This person's current chat; their chat from before owners existed ("Telegram <id>") is adopted."""
        store = self.store
        if store is None:
            return ""
        owner = self.owner(uid)
        if mine := store.owned(owner, 1):
            return str(mine[0].id)
        legacy = f"{self.label} {uid}"
        for ch in store.list("", 10_000):
            if ch.title == legacy and store.claim(ch.id, owner):
                return str(ch.id)
        return str(store.create(legacy, owner=owner).id)

    def new(self, uid: str) -> str:
        """A fresh chat; an empty current one is reused instead of piling up empty chats."""
        from delta.memory.chats import NEW_TITLE

        store = self.store
        if store is None:
            return ""
        mine = store.owned(self.owner(uid), 1)
        if mine and mine[0].count == 0:
            return str(mine[0].id)
        return str(store.create(NEW_TITLE, owner=self.owner(uid)).id)

    def listing(self, uid: str, style: Style) -> tuple[str, list[tuple[str, str]]]:
        """Recent chats: number, title, date, short id. Returns the text and (button label, chat id) pairs."""
        store = self.store
        if store is None:
            return "Chats are switched off on the PC.", []
        mine = [ch for ch in store.owned(self.owner(uid), self.LIST_SIZE) if ch.count]
        if not mine:
            return "No old chats yet. Just type what you want done.", []
        self._listed[self.owner(uid)] = [ch.id for ch in mine]
        lines = [_bold("Your recent chats", style)]
        buttons = []
        for i, ch in enumerate(mine, 1):
            title = shown_title(ch.title)
            mark = " (current)" if i == 1 else ""
            lines.append(_esc(f"{i}. {title[:50]} · {when(ch.updated)} · {ch.id[:6]}{mark}", style))
            buttons.append((f"{i}. {title}"[:40], ch.id))
        hint = "Tap one to continue it, or send /open 2" if style == "telegram" else "Send open 2 to continue chat 2"
        lines += ["", _esc(hint + ". New chat: " + ("/new" if style == "telegram" else "new") + ".", style)]
        return "\n".join(lines), buttons

    def resolve(self, uid: str, ref: str) -> str:
        """A chat id from "2" (a number in the last /chats list) or a short id; only this person's chats."""
        store = self.store
        ref = ref.strip().lstrip("#").lower()
        if store is None or not ref:
            return ""
        owner = self.owner(uid)
        if ref.isdigit() and len(ref) <= 2:  # a list number; a short id can be all digits too, but is 4+ long
            listed = self._listed.get(owner) or [ch.id for ch in store.owned(owner, self.LIST_SIZE) if ch.count]
            n = int(ref)
            return listed[n - 1] if 1 <= n <= len(listed) and store.owner_of(listed[n - 1]) == owner else ""
        found = [ch.id for ch in store.owned(owner, 10_000) if ch.id.startswith(ref)] if len(ref) >= 4 else []
        return found[0] if len(found) == 1 else ""

    def open(self, uid: str, ref: str, style: Style) -> str:
        store = self.store
        if store is None:
            return "Chats are switched off on the PC."
        if not ref.strip():
            return "Which chat? Send " + ("/chats" if style == "telegram" else "chats") + " to see them, then " + (
                "/open 2" if style == "telegram" else "open 2") + "."
        chat_id = self.resolve(uid, ref)
        if not chat_id:
            return "I couldn't find that chat among yours. Send " + ("/chats" if style == "telegram" else "chats") + \
                " to see the list."
        return self.reopen(uid, chat_id, style)

    def reopen(self, uid: str, chat_id: str, style: Style) -> str:
        store = self.store
        ch = store.get(chat_id) if store is not None else None
        if ch is None or store.owner_of(chat_id) != self.owner(uid):  # never someone else's, even by button
            return "I couldn't find that chat among yours."
        store.touch(chat_id)
        last = store.messages(chat_id, 4)
        lines = [_bold(f"Back in: {shown_title(ch.title)[:60]}", style) + _esc(f" ({when(ch.updated)})", style)]
        for m in last:
            text = " ".join(str(m["text"]).split())
            who = "You" if m["role"] == "user" else "Delta"
            lines.append(_esc(f"{who}: {text[:160]}{'…' if len(text) > 160 else ''}", style))
        lines += ["", _esc("Send your next message to continue here.", style)]
        return "\n".join(lines)

    def find(self, uid: str, words: str, style: Style) -> tuple[str, list[tuple[str, str]]]:
        store = self.store
        if store is None:
            return "Chats are switched off on the PC.", []
        if not words.strip():
            return "What should I look for? For example: " + ("/find wifi code" if style == "telegram"
                                                              else "find wifi code"), []
        hits = store.search(words, owner=self.owner(uid), limit=5)
        if not hits:
            return _esc(f"Nothing about \"{words[:60]}\" in your past chats.", style), []
        lines = [_bold(f"Found in your chats: {words[:40]}", style)]
        buttons: list[tuple[str, str]] = []
        for i, h in enumerate(hits, 1):
            ch = store.get(h["chat_id"])
            title = shown_title(ch.title) if ch else "Chat"
            who = "You" if h["role"] == "user" else "Delta"
            lines.append(_esc(f"{i}. {title[:40]} · {when(h['ts'])} · {h['chat_id'][:6]}", style))
            lines.append(_esc(f"   {who}: {h['snippet']}", style))
            if h["chat_id"] not in [b[1] for b in buttons]:
                buttons.append((f"Open: {title}"[:40], h["chat_id"]))
        hint = "Tap to continue one, or /open <id>." if style == "telegram" else "Send open <id> to continue one."
        lines += ["", _esc(hint, style)]
        return "\n".join(lines), buttons


async def handle_text(c: Any, chats: PhoneChats, uid: str, text: str, style: Style = "whatsapp") -> str | None:
    """The menu and chat commands for a text-only channel. Returns the reply, or None when `text` is a normal
    request for the AI. The caller has already checked the sender is paired; a locked PC answers nothing else."""
    cmd = text_command(text)
    if cmd is None:
        return None
    guard = getattr(c, "guard", None)
    if guard is not None and guard.locked:
        return str(guard.locked_message())
    name, arg = cmd
    if name == "help":
        return help_text(style)
    if name == "new":
        chats.new(uid)
        return "New chat started. What should I do?"
    if name == "chats":
        return chats.listing(uid, style)[0]
    if name == "open":
        return chats.open(uid, arg, style)
    if name == "find":
        return chats.find(uid, arg, style)[0]
    if name == "stop":  # the same stop as Telegram's /stop and a plain "stop" (delta.control.stop_command)
        from delta.control import stop_command

        return stop_command(c, "stop", by=f"{chats.channel}:{uid}")
    if name == "status":
        g = c.governor.status()
        return (f"Health {g['health']} · RAM {g['ram_pct']}% · GPU {g['gpu_temp_c']}°C · "
                f"AIs ready: {', '.join(c.router.usable()) or 'none'}")
    return None
