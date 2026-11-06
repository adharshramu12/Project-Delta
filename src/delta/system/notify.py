"""Windows notifications ("toasts") on the PC, so whoever sits there sees what is done remotely.

Uses Windows' own toast API through PowerShell (no extra package). Fire-and-forget on a background
thread: a notification must never slow down or break the action it reports. Bursts are merged so a
run of remote commands shows one toast, not twenty."""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
import time
from xml.sax.saxutils import escape

APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"  # a registered AppID
_last: dict[str, float] = {}
MIN_GAP_S = 8.0  # same title within this gap: skipped (the audit log still has every action)


def _toast_xml(title: str, text: str) -> str:
    return (f"<toast><visual><binding template='ToastGeneric'><text>{escape(title)}</text>"
            f"<text>{escape(text)}</text></binding></visual></toast>")


# The words shown come from outside (a Telegram message, for one), so they never become part of the PowerShell
# command: the script is fixed and reads the toast's XML from an environment variable. Inside a PowerShell
# "string", text like $(...) would otherwise run as a command.
TOAST_SCRIPT = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
    "ContentType=WindowsRuntime] > $null;"
    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null;"
    "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
    "$x.LoadXml($env:DELTA_TOAST_XML);"
    "$t = [Windows.UI.Notifications.ToastNotification]::new($x);"
    f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_ID}').Show($t)"
)


def notify(title: str, text: str) -> None:
    now = time.monotonic()
    if now - _last.get(title, -1e9) < MIN_GAP_S:
        return
    _last[title] = now

    def show() -> None:
        with contextlib.suppress(OSError, subprocess.SubprocessError):  # no toast beats a broken action
            env = {**os.environ, "DELTA_TOAST_XML": _toast_xml(title, text)}
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", TOAST_SCRIPT],
                           capture_output=True, timeout=15, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    threading.Thread(target=show, name="delta-toast", daemon=True).start()
