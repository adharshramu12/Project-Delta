"""What Delta is doing right now, in plain words, for the live progress line in the dashboard and on Telegram.

Every `task.step` event gets a `label` worked out here, on the PC, with no AI call: "Notion: search",
"Opening youtube.com", "Reading the page". Nothing in a label comes from a web page or an app's answer, only from
the tool name and its own arguments, and it is cut short, so a long or odd argument cannot flood the screen.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

# Connected app ids -> their display names ("google_drive" -> "Google Drive"), filled at start-up from the catalog.
APP_NAMES: dict[str, str] = {}
MAX_LABEL = 80
# The latest step of each running task, so a stopped or timed-out task can say where it was.
LAST: dict[str, str] = {}
MAX_TRACKED = 200


def note(task_id: str, label: str) -> None:
    if task_id:
        LAST.pop(task_id, None)
        LAST[task_id] = label
        while len(LAST) > MAX_TRACKED:
            LAST.pop(next(iter(LAST)))

PLAIN = {
    "browser_read": "Reading the page", "browser_click": "Clicking on the page",
    "browser_fill": "Filling in the page", "browser_press": "Pressing a key on the page",
    "browser_scroll": "Scrolling the page", "browser_back": "Going back",
    "browser_select": "Choosing an option on the page", "browser_menu": "Finding a menu on the page",
    "read_file": "Reading a file", "write_file": "Writing a file", "edit_file": "Editing a file",
    "list_dir": "Looking in a folder", "search_files": "Searching your files",
    "delete_path": "Moving a file to the Recycle Bin", "move_path": "Moving a file", "make_dir": "Making a folder",
    "path_info": "Checking a file", "run_shell": "Running a command", "run_python": "Running a small script",
    "look_at_screen": "Looking at the screen", "take_screenshot": "Taking a screenshot",
    "read_window": "Reading a window", "click_element": "Clicking", "click_at": "Clicking", "type_text": "Typing",
    "press_keys": "Pressing keys", "do_actions": "Using the mouse and keyboard", "focus_window": "Switching windows",
    "list_windows": "Checking open windows", "web_search": "Searching the web",
    "web_answer": "Searching the web and reading the best pages", "fetch_page": "Reading a web page",
    "find_app_tool": "Finding the right app tool", "send_message": "Sending a message",
    "read_messages": "Reading messages", "send_file": "Sending you a file", "remember": "Saving to memory",
    "recall": "Checking memory", "play_youtube": "Playing on YouTube",
    "media_control": "Controlling the media player", "schedule_meeting": "Scheduling a meeting",
    "start_meeting": "Starting a meeting", "edit_meeting": "Changing a meeting", "set_volume": "Changing the volume",
    "set_brightness": "Changing the brightness", "set_windows_theme": "Switching the Windows theme",
    "camera_photo": "Taking a webcam photo", "alert_emergency_contacts": "Alerting your emergency contacts",
    "parallel_research": "Researching several sources", "sign_in_captcha": "Signing in",
}


def _short(text: Any, n: int = 40) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _host(url: Any) -> str:
    try:
        host = urlsplit(str(url) if "://" in str(url) else "https://" + str(url)).hostname or ""
    except ValueError:
        return ""
    return host.removeprefix("www.")


def app_name(app_id: str) -> str:
    return APP_NAMES.get(app_id) or app_id.replace("_", " ").title()


def app_action(qualified: str) -> str:
    """'notion__notion-search' -> 'Notion: search'; 'kite__get_holdings' -> 'Zerodha Kite: get holdings'."""
    app, _, tool = qualified.partition("__")
    if not tool:
        return _short(qualified)
    words = re.sub(r"[-_]+", " ", tool).strip().lower()
    prefix = app.replace("_", " ").lower()
    if words.startswith(prefix + " "):
        words = words[len(prefix) + 1:]
    return f"{app_name(app)}: {words}"


def describe(tool: str, args: dict[str, Any] | None = None) -> str:
    """The plain-words line for one step."""
    args = args or {}
    if tool == "use_app_tool":
        label = "Working in " + app_action(str(args.get("tool", ""))) if args.get("tool") else "Working in an app"
    elif "__" in tool:  # an app tool called by its own name
        label = "Working in " + app_action(tool)
    elif tool in ("browser_open", "open_target") and (host := _host(args.get("url") or args.get("target") or "")):
        label = f"Opening {host}"
    elif tool in ("open_app", "open_target"):
        label = f"Opening {_short(args.get('name') or args.get('target') or 'an app', 30)}"
    elif tool in ("browser_search", "web_search") and args.get("query"):
        label = f"Searching for “{_short(args['query'], 40)}”"
    else:
        label = PLAIN.get(tool) or tool.replace("_", " ").capitalize()
    return _short(label, MAX_LABEL)
