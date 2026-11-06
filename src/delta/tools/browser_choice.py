"""Which browser Delta uses, as people say it: one choice model for every place it can be changed
(dashboard, Telegram /browser, or a sentence in chat).

Four options, mapped onto the existing settings keys (nothing old is renamed or removed):

  My default browser       browser.mode = your-browser, browser.prefer = auto
  Chrome                   browser.mode = your-browser, browser.prefer = chrome
  Edge                     browser.mode = your-browser, browser.prefer = edge
  Delta's private browser  browser.mode = delta-profile, browser.private_engine = auto | chrome | edge

The first three drive the user's own signed-in browser through Windows UI Automation (its window has to
be in front while Delta types). The private browser is Delta's own profile in <data_dir>/<browser>-profile,
automated in the background (faster; captcha opt-in and page diffs work there) but signed out until the
user signs in once ("Sign in to my sites"). Choosing the private browser keeps `prefer`, so switching
back restores the user's earlier signed-in pick.

Nothing here imports a heavy library: detection is file checks and one registry read.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

CHOICES = ("default", "chrome", "edge", "private")
ENGINES = ("auto", "chrome", "edge")
LABELS = {"default": "My default browser", "chrome": "Chrome", "edge": "Edge",
          "private": "Delta's private browser"}
HELP = {
    "default": "Your everyday browser, already signed in to your sites. Its window comes to the front while "
               "Delta types.",
    "chrome": "Google Chrome, signed in to your sites. Its window comes to the front while Delta types.",
    "edge": "Microsoft Edge, signed in to your sites. Its window comes to the front while Delta types.",
    "private": "Delta's own separate browser. Works in the background and is faster, but starts signed out: "
               "use \"Sign in to my sites\" once.",
}
ENGINE_LABELS = {"auto": "Automatic (Chrome if installed, else Edge)", "chrome": "Google Chrome",
                 "edge": "Microsoft Edge"}
FULL_NAMES = {"chrome": "Google Chrome", "edge": "Microsoft Edge"}

# Install locations, the same ones the browser tools launch from (browser.BROWSERS, user_browser.KNOWN).
PATHS = {
    "chrome": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
               r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
               r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"],
    "edge": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
             r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"],
}
# Windows "default browser" ProgId prefixes, for naming a default Delta cannot drive.
PROGIDS = {"ChromeHTML": "chrome", "MSEdge": "edge"}
OTHER_PROGIDS = {"FirefoxURL": "Firefox", "BraveHTML": "Brave", "BraveBHTML": "Brave", "OperaStable": "Opera",
                 "OperaGX": "Opera GX", "VivaldiHTM": "Vivaldi", "IE.HTTP": "Internet Explorer"}


@dataclass(frozen=True)
class BrowserPick:
    """One browser choice: what the user picked, and (for the private browser) which engine runs it."""

    choice: str = "default"
    engine: str = "auto"  # only meaningful for "private"

    @property
    def kind(self) -> str:
        """user = the person's own signed-in browser; private = Delta's own profile."""
        return "private" if self.choice == "private" else "user"

    @property
    def prefer(self) -> str:
        """The `browser.prefer` value for the user's own browser."""
        return "auto" if self.choice in ("default", "private") else self.choice

    @property
    def label(self) -> str:
        if self.choice == "private" and self.engine != "auto":
            return f"{LABELS['private']} ({FULL_NAMES[self.engine]})"
        return LABELS[self.choice]


def from_config(browser: Any) -> BrowserPick:
    """The four-way choice a BrowserConfig (or anything with mode/prefer) amounts to."""
    mode = getattr(browser, "mode", "your-browser")
    engine = getattr(browser, "private_engine", "auto") or "auto"
    if mode == "delta-profile":
        return BrowserPick("private", engine if engine in ENGINES else "auto")
    prefer = getattr(browser, "prefer", "auto")
    return BrowserPick(prefer if prefer in ("chrome", "edge") else "default", engine if engine in ENGINES else "auto")


def to_changes(choice: str, engine: str | None = None) -> dict[str, str]:
    """Settings keys to write for a choice. The private browser leaves `prefer` alone."""
    if choice not in CHOICES:
        raise ValueError(f"unknown browser choice '{choice}'")
    if engine is not None and engine not in ENGINES:
        raise ValueError(f"unknown engine '{engine}'")
    if choice == "private":
        return {"mode": "delta-profile", **({"private_engine": engine} if engine else {})}
    return {"mode": "your-browser", "prefer": "auto" if choice == "default" else choice}


_WORDS = re.compile(r"[’`]")


def parse_choice(text: str) -> str | None:
    """'Microsoft Edge', "delta's private browser", 'my normal browser', 'delta-profile' -> a choice id."""
    t = _WORDS.sub("'", (text or "").lower()).strip()
    if not t:
        return None
    if t in CHOICES:
        return t
    if "private" in t or "delta" in t or "separate" in t:
        return "private"
    if "chrome" in t:
        return "chrome"
    if re.search(r"\bedge\b", t):
        return "edge"
    if re.search(r"\b(default|normal|usual|regular|own|signed[- ]?in|your[- ]browser)\b", t):
        return "default"
    return None


def parse_engine(text: str | None) -> str | None:
    t = (text or "").lower()
    if "chrome" in t:
        return "chrome"
    if "edge" in t:
        return "edge"
    if "auto" in t:
        return "auto"
    return None


# --- what is installed ---------------------------------------------------------------------------------

def windows_default_progid() -> str:
    """ProgId of the Windows default https handler ('' when unknown or not on Windows)."""
    with contextlib.suppress(OSError, ImportError):
        import winreg

        key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            return str(winreg.QueryValueEx(k, "ProgId")[0])
    return ""


def _has(*modules: str) -> bool:
    try:
        return all(importlib.util.find_spec(m) is not None for m in modules)
    except (ImportError, ValueError):
        return False


@dataclass(frozen=True)
class Installed:
    """What this PC can do. desktop: Delta can drive the user's own browser (Windows UI Automation);
    automation: Delta can run its private browser (Playwright)."""

    chrome: bool = False
    edge: bool = False
    progid: str = ""
    desktop: bool = True
    automation: bool = True

    def has(self, name: str) -> bool:
        return bool(getattr(self, name, False)) if name in ("chrome", "edge") else False

    @property
    def any(self) -> bool:
        return self.chrome or self.edge

    @property
    def windows_default(self) -> str:
        """chrome / edge / the name of another browser / '' when unknown."""
        for prefix, name in PROGIDS.items():
            if self.progid.startswith(prefix):
                return name
        for prefix, name in OTHER_PROGIDS.items():
            if self.progid.startswith(prefix):
                return name
        return "another browser" if self.progid else ""

    def default_target(self) -> str | None:
        """The browser "My default browser" really drives: the Windows default when it is Chrome or Edge,
        else Chrome, else Edge (the same order as user_browser.default_browser)."""
        d = self.windows_default
        if d in ("chrome", "edge") and self.has(d):
            return d
        return "chrome" if self.chrome else "edge" if self.edge else None

    def engine_target(self, engine: str) -> str | None:
        """The browser that runs Delta's private profile (browser.find_browser order)."""
        if engine in ("chrome", "edge") and self.has(engine):
            return engine
        return "chrome" if self.chrome else "edge" if self.edge else None


def detect(exists: Callable[[str], bool] = os.path.exists, progid: Callable[[], str] = windows_default_progid,
           desktop: bool | None = None, automation: bool | None = None) -> Installed:
    """Look, without starting anything: which of Chrome and Edge are installed, and the Windows default."""
    found = {name: any(exists(os.path.expandvars(p)) for p in paths) for name, paths in PATHS.items()}
    if desktop is None:
        desktop = sys.platform == "win32" and _has("pywinauto", "pyautogui", "mss")
    if automation is None:
        automation = _has("playwright")
    return Installed(found["chrome"], found["edge"], progid(), desktop, automation)


def unavailable_reason(choice: str, inst: Installed, engine: str = "auto") -> str:
    """'' when the choice works on this PC, else one plain sentence why not."""
    if choice not in CHOICES:
        return f"'{choice}' is not one of: {', '.join(LABELS.values())}."
    if choice == "private":
        if not inst.automation:
            return "Delta's private browser needs the browser add-on (Playwright), which is missing from this install."
        if not inst.any:
            return "Neither Google Chrome nor Microsoft Edge is installed on this PC."
        if engine in ("chrome", "edge") and not inst.has(engine):
            return f"{FULL_NAMES[engine]} is not installed on this PC."
        return ""
    if not inst.desktop:
        return "Using your own browser needs Windows desktop control, which is not available on this PC."
    if choice in ("chrome", "edge") and not inst.has(choice):
        return f"{FULL_NAMES[choice]} is not installed on this PC."
    if not inst.any:
        return "Neither Google Chrome nor Microsoft Edge is installed on this PC."
    return ""


def uses_note(choice: str, inst: Installed, engine: str = "auto") -> str:
    """Which browser a choice really opens, in a few words ('' when obvious)."""
    if choice == "default":
        target = inst.default_target()
        d = inst.windows_default
        if target and d and d not in ("chrome", "edge"):
            return f"Your Windows default is {d}, which Delta cannot drive, so it uses {FULL_NAMES[target]}."
        return f"Uses {FULL_NAMES[target]}." if target else ""
    if choice == "private":
        target = inst.engine_target(engine)
        return f"Runs in {FULL_NAMES[target]}." if target else ""
    return ""


def options(inst: Installed, current: BrowserPick) -> list[dict[str, Any]]:
    """The four options for a picker: available ones, and unavailable ones with the reason."""
    out = []
    for c in CHOICES:
        why = unavailable_reason(c, inst, current.engine if c == "private" else "auto")
        if c == "private" and why and inst.automation and inst.any:
            why = ""  # the engine is picked separately; an engine that is missing shows there
        out.append({"id": c, "label": LABELS[c], "help": HELP[c], "available": not why, "reason": why,
                    "current": c == current.choice, "uses": uses_note(c, inst, current.engine)})
    return out


def engine_options(inst: Installed, current: BrowserPick) -> list[dict[str, Any]]:
    out = []
    for e in ENGINES:
        why = "" if e == "auto" else ("" if inst.has(e) else f"{FULL_NAMES[e]} is not installed on this PC.")
        if e == "auto" and not inst.any:
            why = "Neither Google Chrome nor Microsoft Edge is installed on this PC."
        out.append({"id": e, "label": ENGINE_LABELS[e], "available": not why, "reason": why,
                    "current": e == current.engine})
    return out


def describe(pick: BrowserPick, inst: Installed | None = None) -> str:
    """One plain line: what the choice is and what that means."""
    if pick.choice == "private":
        engine = (inst.engine_target(pick.engine) if inst else None) or (pick.engine if pick.engine != "auto" else "")
        runs = f" (running in {FULL_NAMES[engine]})" if engine in FULL_NAMES else ""
        return f"Delta's private browser{runs}: works in the background, signed in only where you signed in to it."
    if pick.choice == "default":
        target = inst.default_target() if inst else None
        return ("your default browser" + (f" ({FULL_NAMES[target]})" if target else "")
                + ", signed in to your sites")
    return f"{FULL_NAMES[pick.choice]}, signed in to your sites"


# --- the user saying it in a sentence ------------------------------------------------------------------

_B = (r"(?:my\s+)?(?:windows\s+)?(?:default|normal|usual|regular|own|everyday|signed[-\s]?in)(?:\s+web)?\s+browser"
      r"|(?:my\s+)?(?:google\s+)?chrome(?:\s+browser)?"
      r"|(?:my\s+)?(?:microsoft\s+|ms\s+)?edge(?:\s+browser)?"
      r"|delta'?s?\s+(?:own\s+|private\s+|separate\s+)+(?:browser|profile)"
      r"|(?:the\s+|your\s+)?(?:delta\s+)?private\s+browser"
      r"|(?:the\s+)?delta[-\s]profile")
_LASTING = r"(?:from\s+now\s+on|always|by\s+default|going\s+forward|for\s+everything|for\s+all\s+tasks|permanently)"
_POLITE = r"^(?:please\s+|delta[,\s]+|hey\s+delta[,\s]+|ok(?:ay)?[,\s]+)*"
_PERSISTENT = [
    re.compile(_POLITE + _LASTING + r"[,\s]+(?:please\s+)?use\s+(?P<b>" + _B + r")\s*$", re.I),
    re.compile(_POLITE + r"(?:use|make\s+delta\s+use|set\s+(?:the\s+|my\s+)?browser\s+to|change\s+(?:the\s+|my\s+)?"
               r"browser\s+to|switch\s+(?:the\s+|my\s+)?browser\s+to)\s+(?P<b>" + _B + r")(?:\s+" + _LASTING
               + r")?\s*$", re.I),
    # "switch to edge" alone could mean the window; only with "browser" or a lasting word it is the setting
    re.compile(_POLITE + r"switch\s+to\s+(?P<b>" + _B + r")\s+" + _LASTING + r"\s*$", re.I),
    re.compile(_POLITE + r"switch\s+to\s+(?P<b>(?:" + _B + r"))\s*$", re.I),
]
_PER_TASK = re.compile(r"\b(?:in|on|using|with|via|through)\s+(?P<b>" + _B + r")\b"
                       r"|" + _POLITE + r"use\s+(?P<u>" + _B + r")\s+(?:to|and|for)\b", re.I)
_LASTING_ANYWHERE = re.compile(r"\b" + _LASTING + r"\b", re.I)


def _clean(text: str) -> str:
    return " ".join(_WORDS.sub("'", text).split()).rstrip(" .!?")


def persistent_request(text: str) -> str | None:
    """The whole message is a lasting switch ("use Edge from now on", "switch to Delta's private
    browser", "use my default browser"): the choice id. Anything longer goes to the planner."""
    t = _clean(text)
    if len(t) > 120:
        return None
    for i, pattern in enumerate(_PERSISTENT):
        m = pattern.match(t)
        if not m:
            continue
        b = m["b"].lower()
        if i == 3 and "browser" not in b and "profile" not in b:
            return None  # "switch to chrome": probably the window
        return parse_choice(b)
    return None


def task_override(text: str) -> BrowserPick | None:
    """'open gmail in Chrome', 'do this in Delta's private browser': a choice for this task only.
    Messages that ask for a lasting change are not task overrides (set_browser handles them)."""
    t = _clean(text)
    if _LASTING_ANYWHERE.search(t) or persistent_request(t):
        return None
    m = _PER_TASK.search(t)
    if not m:
        return None
    words = m["b"] or m["u"]
    choice = parse_choice(words)
    if choice is None:
        return None
    # "" = the private browser's saved engine (filled in by the browser switch)
    return BrowserPick(choice, (parse_engine(words) or "") if choice == "private" else "auto")


_ASKS_ABOUT_BROWSER = re.compile(r"\b(browser|chrome|edge|private|profile)\b", re.I)


def mentions_browser(text: str) -> bool:
    """The user's own message talks about browsers (a lasting switch is only made when it does)."""
    return bool(_ASKS_ABOUT_BROWSER.search(text or ""))


_SIGNIN = re.compile(
    _POLITE + r"(?:(?:help\s+me|let\s+me|i\s+(?:want|need)\s+to|i'?ll)\s+)?(?:sign|log)\s*[-\s]?in\s+to\s+"
    r"(?:(?P<mine>my\s+(?:sites|websites|accounts))|(?P<site>[\w-]+(?:\.[\w-]+)+))"
    r"(?:\s+(?:in|on|with)\s+(?:" + _B + r"))?\s*$", re.I)
_OPEN_PRIVATE = re.compile(_POLITE + r"open\s+(?:" + _B + r")(?:\s+(?:so\s+i\s+can|to)\s+(?:sign|log)\s*[-\s]?in"
                           r"(?:\s+to\s+(?:my\s+)?(?:sites|websites|accounts))?)?\s*$", re.I)


def signin_request(text: str) -> dict[str, str] | None:
    """'sign in to my sites', 'log in to gmail.com in Delta's private browser', 'open Delta's private
    browser': the open_signin_browser arguments."""
    t = _clean(text)
    if len(t) > 120:
        return None
    if m := _SIGNIN.match(t):
        if m["site"]:
            site = m["site"].lower()
            return {"url": site if "://" in site else f"https://{site}"}
        return {"url": ""}
    if (m2 := _OPEN_PRIVATE.match(t)) and parse_choice(m2.group(0).split(None, 1)[-1]) == "private":
        return {"url": ""}
    return None
