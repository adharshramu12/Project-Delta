"""Windows notifications ("toasts") on the PC, so whoever sits there sees what is done remotely.

Uses Windows' own toast API through PowerShell (no extra package). Fire-and-forget on a background
thread: a notification must never slow down or break the action it reports. Bursts are merged so a
run of remote commands shows one toast, not twenty."""

from __future__ import annotations

import contextlib
import subprocess
import threading
import time
from xml.sax.saxutils import escape

APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"  # a registered AppID
_last: dict[str, float] = {}
MIN_GAP_S = 8.0  # same title within this gap: skipped (the audit log still has every action)


def _toast_script(title: str, text: str) -> str:
    xml = (f"<toast><visual><binding template='ToastGeneric'><text>{escape(title)}</text>"
           f"<text>{escape(text)}</text></binding></visual></toast>")
    return (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType=WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml(\"{xml.replace(chr(34), '&quot;')}\");"
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
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _toast_script(title, text)],
                           capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    threading.Thread(target=show, name="delta-toast", daemon=True).start()
