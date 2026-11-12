"""Instant commands: common requests that need no AI at all to understand.

"pause the music", "next song", "play Kaanunna Kalyanam on YouTube", "start a Google Meet" map
straight to one tool call: 0 tokens and no wait for a planner. The patterns are deliberately strict;
anything ambiguous ("play chess", "schedule a meeting tomorrow with Ravi") goes to the planner as
before, and if the tool cannot prove its result the normal agent takes over.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Intent:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    check: str = ""  # the checkpoint this command must meet


MEDIA_WORDS = r"(?:the\s+)?(?:music|song|video|track|audio|playback|youtube)"
POLITE = r"^(?:please\s+|delta[,\s]+|hey\s+delta[,\s]+)*"
_RULES: list[tuple[re.Pattern[str], Any]] = [
    (re.compile(POLITE + rf"(?:pause|stop)\s+{MEDIA_WORDS}\s*[.!]*$", re.I),
     lambda m: Intent("media_control", {"action": "pause"}, "playback is paused")),
    (re.compile(POLITE + rf"(?:resume|continue|unpause)(?:\s+{MEDIA_WORDS})?\s*[.!]*$", re.I),
     lambda m: Intent("media_control", {"action": "play"}, "playback resumed")),
    (re.compile(POLITE + r"(?:next|skip)(?:\s+(?:song|track|video))?\s*[.!]*$", re.I),
     lambda m: Intent("media_control", {"action": "next"}, "the next track is playing")),
    (re.compile(POLITE + r"(?:previous|prev|last)\s+(?:song|track|video)\s*[.!]*$", re.I),
     lambda m: Intent("media_control", {"action": "previous"}, "the previous track is playing")),
    (re.compile(POLITE + r"(?:what(?:'s| is)\s+playing|what\s+song\s+is\s+this)\s*\??$", re.I),
     lambda m: Intent("media_status", {}, "the playing media is reported")),
    # "play X on youtube" / "play X song" / "youtube X"
    (re.compile(POLITE + r"(?:play|put on)\s+(?P<q>.+?)\s+(?:on|in|from)\s+youtube\s*[.!]*$", re.I),
     lambda m: Intent("play_youtube", {"query": _clean(m["q"])}, f"'{_clean(m['q'])}' is playing on YouTube")),
    (re.compile(POLITE + r"(?:play|put on)\s+(?P<q>.+?\b(?:song|songs|video|music|trailer)\b.*?)\s*[.!]*$", re.I),
     lambda m: Intent("play_youtube", {"query": _clean(m["q"])}, f"'{_clean(m['q'])}' is playing on YouTube")),
    (re.compile(POLITE + r"stop (?:chatting|replying|talking)(?: (?:with|to) (?P<who>.+?))?\s*[.!]*$", re.I),
     lambda m: Intent("chat_for_me", {"stop": True, **({"person": m["who"].strip()} if m["who"] else {})},
                      "Delta stopped chatting")),
    # "start / create / open a (new / instant) google meet (meeting)"
    (re.compile(POLITE + r"(?:start|create|open|make|set\s*up|begin)\s+(?:an?\s+)?(?:new\s+|instant\s+|quick\s+)*"
                r"(?P<p>google\s+meet|gmeet|meet|zoom|teams|microsoft\s+teams)(?:\s+(?:meeting|call|link))?\s*[.!]*$",
                re.I),
     lambda m: Intent("start_meeting", {"platform": _platform(m["p"])}, "a meeting link was created")),
]


# "open notepad", "launch calculator", "open the WhatsApp app": one open_app call, 0 tokens, no planner. Strict:
# one short app name and nothing else. Folders, files, websites, a second step ("and type hi"), or a target
# ("in Chrome", "for Ravi") go to the planner as before. open_app proves the result itself (the app's window is
# in front); when it cannot (not found, no window yet), the normal agent takes over.
_OPEN_APP = re.compile(POLITE + r"(?:open|launch)\s+(?:up\s+)?(?:the\s+|my\s+)?(?P<app>[^\W\d][\w+&' -]{0,38}?)"
                       r"(?:\s+app(?:lication)?)?(?:\s+(?:please|pls|now))?\s*[.!]*$", re.I)
_NOT_AN_APP = re.compile(
    r"\b(?:folder|folders|file|files|document|documents|downloads?|desktop|pictures?|photos?|music|videos?|"
    r"recycle\s+bin|this\s+pc|drive|tab|tabs|window|windows|website|site|page|link|url|web|browser|chat|chats|"
    r"conversation|email|emails|mail|message|messages|meeting|meet|call|notebook|account|settings\s+for|"
    r"and|then|with|in|on|for|to|from|at|of|it|that|this|them|again|new|last|recent|youtube|gmail|google|chatgpt|"
    r"gemini|claude|instagram|facebook|linkedin|twitter|reddit|amazon|flipkart|netflix|wikipedia|github|"
    r"notebooklm|perplexity)\b", re.I)


def open_app_intent(text: str) -> Intent | None:
    m = _OPEN_APP.match(text)
    if not m:
        return None
    app = m["app"].strip(" '")
    if not app or len(app.split()) > 3 or _NOT_AN_APP.search(app):
        return None
    return Intent("open_app", {"name": app}, f"{app}'s window is open and in front")


def _clean(q: str) -> str:
    return re.sub(r"^(?:the|a|an)\s+", "", q.strip(), flags=re.I).strip(" .!\"'")


def _platform(p: str) -> str:
    p = p.lower()
    return "zoom" if "zoom" in p else "teams" if "teams" in p else "google_meet"


# PC controls: volume, mute, brightness and light/dark mode answer at once, with no AI call. A friend's first day
# with Delta: "increase the colume" went through the planner, was slow and sometimes ended in "sorry". Strict:
# one control and an optional amount; typos people really make; casual endings ("buddy", "please"). Anything
# else ("mute Ravi on WhatsApp") goes to the planner as before.
_VOL = (r"(?:the\s+|my\s+|pc\s+|system\s+|laptop\s+)?"
        r"(?:volume|volum|volumn|voulme|volme|voluem|valume|colume|vol|sound|audio)")
_BRI = r"(?:the\s+|my\s+|screen\s+|display\s+)?(?:brightness|brightnes|brigtness|brighness|brightess|brightnss)"
_UP = r"(?:increase|incrase|increse|inc|raise|turn\s+up|up|boost|higher|more)"
_DOWN = r"(?:decrease|decrese|dec|reduce|lower|turn\s+down|down|less)"
_BY = r"(?:\s+(?:by\s+)?(?P<by>\d{1,3})\s*%?)?"
_END = r"(?:\s+(?:please|pls|plz|buddy|bro|yaar|da|now|a\s+bit|a\s+little|little|slightly))*\s*[.!]*$"
_PC_RULES: list[tuple[re.Pattern[str], Any]] = []


def _level_rules(what: str, tool: str, noun: str) -> None:
    def step(sign: int) -> Any:
        went = f"the {noun} went {'up' if sign > 0 else 'down'}"
        return lambda m: Intent(tool, {"change": sign * int(m["by"] or 10)}, went)

    to_level = rf"(?:set\s+|make\s+|put\s+|change\s+)?{what}\s+(?:to\s+|at\s+)?(?P<n>\d{{1,3}})\s*%?{_END}"
    to_max = rf"(?:(?:set\s+)?{what}\s+(?:to\s+)?(?:max|maximum|full|100\s*%)|(?:max|maximum|full)\s+{what}){_END}"
    _PC_RULES.extend([
        (re.compile(POLITE + rf"(?:can\s+you\s+|could\s+you\s+)?{_UP}\s+{what}{_BY}{_END}", re.I), step(1)),
        (re.compile(POLITE + rf"(?:can\s+you\s+|could\s+you\s+)?{_DOWN}\s+{what}{_BY}{_END}", re.I), step(-1)),
        (re.compile(POLITE + rf"(?:turn\s+)?{what}\s+(?:{_UP}){_BY}{_END}", re.I), step(1)),
        (re.compile(POLITE + rf"(?:turn\s+)?{what}\s+(?:{_DOWN}){_BY}{_END}", re.I), step(-1)),
        (re.compile(POLITE + to_level, re.I),
         lambda m: Intent(tool, {"level": min(100, int(m["n"]))}, f"the {noun} is {min(100, int(m['n']))}%")),
        (re.compile(POLITE + to_max, re.I), lambda m: Intent(tool, {"level": 100}, f"the {noun} is 100%")),
    ])


_level_rules(_VOL, "set_volume", "volume")
_level_rules(_BRI, "set_brightness", "brightness")
_PC_RULES.extend([
    (re.compile(POLITE + r"(?:make\s+it\s+|a\s+bit\s+)?(?:louder)" + _END, re.I),
     lambda m: Intent("set_volume", {"change": 10}, "the volume went up")),
    (re.compile(POLITE + r"(?:make\s+it\s+|a\s+bit\s+)?(?:quieter|softer)" + _END, re.I),
     lambda m: Intent("set_volume", {"change": -10}, "the volume went down")),
    (re.compile(POLITE + rf"mute(?:\s+(?:it|the\s+pc|{_VOL}))?{_END}", re.I),
     lambda m: Intent("set_volume", {"mute": True}, "the PC is muted")),
    (re.compile(POLITE + rf"unmute(?:\s+(?:it|the\s+pc|{_VOL}))?{_END}", re.I),
     lambda m: Intent("set_volume", {"mute": False}, "the PC is unmuted")),
    (re.compile(POLITE + r"(?:turn\s+on\s+|switch\s+(?:on\s+|to\s+)|enable\s+|change\s+to\s+|go\s+)?"
                r"(?P<mode>dark|light)\s+(?:mode|theme)(?:\s+on)?" + _END, re.I),
     lambda m: Intent("set_windows_theme", {"mode": m["mode"].lower()}, f"Windows is in {m['mode'].lower()} mode")),
    (re.compile(POLITE + r"(?:turn\s+off|disable)\s+(?P<mode>dark|light)\s+(?:mode|theme)" + _END, re.I),
     lambda m: Intent("set_windows_theme", {"mode": "light" if m["mode"].lower() == "dark" else "dark"},
                      "Windows switched theme")),
])


def pc_intent(text: str) -> Intent | None:
    for pattern, build in _PC_RULES:
        if m := pattern.match(text):
            return build(m)
    return None


# "open youtube.com", "go to github.com/adharshramu12", "open gmail", "open tradingview account": one browser_open
# call, 0 tokens, no planner. Before, the page opened and then a second, slow AI call only decided that the task was
# finished: people saw the browser open and Delta "stop" for a minute or more. Strict: a web address, or one of the
# site names below, and nothing else to do there. browser_open proves the result itself (the page that loaded is on
# the site asked for); when it cannot, the normal agent takes over with the page already open.
SITES = {
    "youtube": "youtube.com", "gmail": "mail.google.com", "google": "google.com", "chatgpt": "chatgpt.com",
    "gemini": "gemini.google.com", "claude": "claude.ai", "instagram": "instagram.com", "facebook": "facebook.com",
    "linkedin": "linkedin.com", "twitter": "x.com", "reddit": "reddit.com", "netflix": "netflix.com",
    "wikipedia": "wikipedia.org", "github": "github.com", "notebooklm": "notebooklm.google.com",
    "notebook lm": "notebooklm.google.com", "perplexity": "perplexity.ai", "tradingview": "tradingview.com",
    "trading view": "tradingview.com", "whatsapp web": "web.whatsapp.com", "google drive": "drive.google.com",
    "google docs": "docs.google.com", "google sheets": "sheets.google.com", "google calendar": "calendar.google.com",
    "google maps": "maps.google.com", "google meet": "meet.google.com", "flipkart": "flipkart.com",
    "stack overflow": "stackoverflow.com", "stackoverflow": "stackoverflow.com", "hugging face": "huggingface.co",
    "huggingface": "huggingface.co",
}
# Endings that are web addresses, not file names ("open notes.txt" and "open setup.exe" are not websites).
_TLDS = ("com|in|org|net|io|ai|dev|app|co|edu|gov|me|tv|info|xyz|tech|so|uk|us|ca|au|de|fr|jp|sg|ae|online|site|"
         "store|cloud|gg|to|ly|fm|be")
_OPEN_SITE = re.compile(
    POLITE + r"(?:open|launch|go\s+to|visit|take\s+me\s+to|show\s+me)\s+(?:up\s+)?(?:the\s+|my\s+)?"
    r"(?:(?:website|site|page|link|url)\s+)?(?P<what>.+?)"
    r"(?:\s+(?:website|site|page|account|home\s*page))?(?:\s+in\s+(?:the\s+|my\s+)?browser)?"
    r"(?:\s+(?:please|pls|now))?\s*[.!]*$", re.I)
_ADDRESS = re.compile(rf"^(?:https?://)?(?:[\w-]+\.)+(?:{_TLDS})(?::\d{{2,5}})?(?:/\S*)?$", re.I)


def open_site_intent(text: str) -> Intent | None:
    m = _OPEN_SITE.match(text)
    if not m:
        return None
    what = m["what"].strip(" '\"")
    if _ADDRESS.match(what) and " " not in what:
        url = what if "://" in what else f"https://{what}"
    elif (site := SITES.get(" ".join(what.lower().split()))) is not None:
        url = f"https://{site}"
    else:
        return None
    return Intent("browser_open", {"url": url}, f"{url} is open in the browser")


def match_intent(text: str, available: set[str]) -> Intent | None:
    t = " ".join(text.split())
    if len(t) > 160 or "\n" in text:
        return None  # long or multi-part requests need real planning
    from delta.core.routines import routine_command

    if (routine := routine_command(t)) is not None:  # "save this as X", "run X", "my routines", "delete routine X"
        tool, args = routine
        return Intent(tool, args, "the routine command is done") if tool in available else None
    for pattern, build in _RULES:
        m = pattern.match(t)
        if m:
            intent = build(m)
            return intent if intent.tool in available else None
    if (pc := pc_intent(t)) is not None:
        return pc if pc.tool in available else None
    if (site := open_site_intent(t)) is not None:  # before apps: "open tradingview" is a site, not a program
        return site if site.tool in available else None
    if (opened := open_app_intent(t)) is not None:
        return opened if opened.tool in available else None
    if (browser := browser_intent(t)) is not None:
        return browser if browser.tool in available else None
    if (sched := schedule_intent(t)) is not None:
        return sched if sched.tool in available else None
    return None


def browser_intent(text: str) -> Intent | None:
    """"use Edge from now on", "switch to Delta's private browser", "sign in to my sites": one tool call.
    A browser named inside a longer task ("open Gmail in Chrome") is a per-task choice instead
    (BrowserHub.begin_task), not a saved switch."""
    from delta.tools.browser_choice import persistent_request, signin_request

    if (choice := persistent_request(text)) is not None:
        return Intent("set_browser", {"choice": choice, "scope": "always"}, "the browser choice is saved")
    if (signin := signin_request(text)) is not None:
        return Intent("open_signin_browser", signin, "Delta's private browser is open for the user to sign in")
    return None


# "remind me at 6 pm to call Ravi", "every day at 9 am check my attendance": saved as a schedule with no
# planner call, when the sentence starts like a schedule and the time is certain (delta.scheduler.parse).
_SCHEDULE_START = re.compile(POLITE + r"(?:remind\s+me|set\s+(?:a\s+)?reminder|every|each|daily|hourly)\b", re.I)


def schedule_intent(text: str) -> Intent | None:
    if not _SCHEDULE_START.match(text):
        return None
    from delta.core.clarify import SCHEDULING
    from delta.scheduler.parse import ParseError, parse_schedule

    if SCHEDULING.search(text) and not text.lower().lstrip().startswith("remind"):
        return None  # "every Monday schedule a meeting...": a calendar job for the planner, not a routine

    try:
        parse_schedule(text)
    except ParseError:
        return None  # unclear: the planner (or a question back) handles it
    return Intent("schedule_task", {"text": text}, "the schedule is saved")


# Greetings and thanks: answered on the spot. Sending "hi" to an AI app costs ~12k tokens of its
# fixed instructions and 10-20 seconds, for a reply anyone can write.
_GREETING = re.compile(POLITE + r"(hi+|hey+|hello+|hii+|yo|namaste|vanakkam|good\s+(?:morning|afternoon|evening))"
                       r"(?:\s+(?:there|delta|buddy|bro|da))?\s*[!.]*\s*$", re.I)
_THANKS = re.compile(POLITE + r"(thanks?|thank\s+you|thx|ty|ok(?:ay)?\s+thanks?|great,?\s+thanks?|super|nice|cool)"
                     r"(?:\s+(?:so\s+much|a\s+lot|delta|buddy|bro|da))?\s*[!.]*\s*$", re.I)


def small_talk(text: str, owner: str = "") -> str | None:
    t = " ".join(text.split())
    name = f", {owner}" if owner else ""
    if _GREETING.match(t):
        return f"Hi{name}! What should I do for you?"
    if _THANKS.match(t):
        return "Anytime! Tell me if you need anything else."
    return None
