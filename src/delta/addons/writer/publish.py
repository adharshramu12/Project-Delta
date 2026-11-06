"""Hand a finished post to the user to publish themselves.

Delta NEVER presses Post or Publish on LinkedIn, Medium or Substack:
  - LinkedIn's User Agreement (section 8.2) forbids bots and automated posting on the site.
  - Medium closed new API integration tokens and Substack has no open posting API.
So the only thing this module does is copy the text to the clipboard and open the platform's own
compose page in the user's normal browser (the Windows default handler, not a Delta-driven browser).
The user pastes, reviews and presses Post. There is no code path here that clicks, types or submits.
"""

from __future__ import annotations

import subprocess
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass

from delta.addons.writer.post import SUBSTACK_HOST, Platform

PUBLISHING_NOTE = ("Delta never presses Post/Publish on LinkedIn, Medium or Substack. The text is on your "
                   "clipboard and the compose page is open: paste (Ctrl+V), check it, and press Post yourself.")


def copy_to_clipboard(text: str) -> bool:
    """Unicode-safe clipboard copy on Windows (pywin32 if present, else clip.exe with UTF-16)."""
    from delta.tools.meetings import copy_to_clipboard as win32_copy

    if win32_copy(text):
        return True
    if sys.platform != "win32":
        return False
    try:
        subprocess.run(["clip.exe"], input=("\ufeff" + text).encode("utf-16-le"), check=True, timeout=10,
                       capture_output=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def open_in_user_browser(url: str) -> bool:
    """The user's default browser via the OS handler. No automation, no Delta profile."""
    return bool(webbrowser.open(url, new=2))


def compose_url(plat: Platform, substack_publication: str = "") -> str:
    if plat.name == "substack" and substack_publication:
        host = substack_publication.strip().lower().removeprefix("https://").split("/")[0]
        if "." not in host:
            host = f"{host}.substack.com"
        if SUBSTACK_HOST.match(host):
            return f"https://{host}/publish/post"
    return plat.compose_url


@dataclass
class Prepared:
    copied: bool
    opened: bool
    url: str


def prepare(text: str, plat: Platform, substack_publication: str = "",
            copy: Callable[[str], bool] | None = None,
            open_url: Callable[[str], bool] | None = None) -> Prepared:
    """Clipboard + compose page. Returns what happened; never publishes."""
    url = compose_url(plat, substack_publication)
    copied = (copy or copy_to_clipboard)(text)
    opened = (open_url or open_in_user_browser)(url)
    return Prepared(copied, opened, url)
