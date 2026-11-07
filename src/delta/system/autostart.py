"""Settings > Setup and shutdown: "Start Delta when I sign in to Windows" (off unless the user turns it on).

Without it, nothing starts Delta after a restart, so Telegram stops answering until the user opens Delta again
(found by the fresh-user CI journey). On: one value in the account's own Run key (HKCU, no administrator rights)
that starts Delta in the background at sign-in, with no window: `pythonw.exe -m delta app --background`.
Off, and the uninstaller (scripts/uninstall), delete that value again. Nothing else is written: no Startup-folder
shortcut, no scheduled task, nothing PC-wide.
"""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# Where Task Manager's Startup apps tab remembers that the user turned an entry off. Delta only removes its own value
# there, so turning the setting on in Delta really turns it on again.
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE = "DeltaAgent"  # not "Delta": another program could use that name. The uninstaller deletes this one.
ARGS = "-m delta app --background"


@dataclass
class Status:
    supported: bool
    enabled: bool
    command: str = ""
    blocked_in_task_manager: bool = False

    def to_dict(self) -> dict[str, object]:
        return {"supported": self.supported, "enabled": self.enabled, "command": self.command,
                "blocked_in_task_manager": self.blocked_in_task_manager}


def command(executable: str | None = None) -> str:
    """The sign-in command: Delta's own pythonw.exe (no console window), started in the background."""
    exe = Path(executable or sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return f'"{pythonw if pythonw.exists() else exe}" {ARGS}'


def _winreg() -> Any:
    if sys.platform != "win32":
        return None
    import winreg

    return winreg


def status() -> Status:
    reg = _winreg()
    if reg is None:
        return Status(supported=False, enabled=False)
    value = ""
    with contextlib.suppress(OSError), reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY) as k:
        value = str(reg.QueryValueEx(k, VALUE)[0])
    blocked = False
    with contextlib.suppress(OSError), reg.OpenKey(reg.HKEY_CURRENT_USER, APPROVED_KEY) as k:
        data = reg.QueryValueEx(k, VALUE)[0]
        blocked = bool(data) and isinstance(data, bytes) and data[0] % 2 == 1  # 03 = turned off in Task Manager
    return Status(supported=True, enabled=bool(value) and not blocked, command=value, blocked_in_task_manager=blocked)


def _forget_task_manager_choice(reg: Any) -> None:
    with contextlib.suppress(OSError), reg.OpenKey(reg.HKEY_CURRENT_USER, APPROVED_KEY, 0, reg.KEY_SET_VALUE) as k:
        reg.DeleteValue(k, VALUE)


def enable(executable: str | None = None) -> Status:
    reg = _winreg()
    if reg is None:
        return Status(supported=False, enabled=False)
    cmd = command(executable)
    with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as k:
        reg.SetValueEx(k, VALUE, 0, reg.REG_SZ, cmd)
    _forget_task_manager_choice(reg)
    return status()


def disable() -> Status:
    reg = _winreg()
    if reg is None:
        return Status(supported=False, enabled=False)
    with contextlib.suppress(OSError), reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as k:
        reg.DeleteValue(k, VALUE)
    _forget_task_manager_choice(reg)
    return status()


def set_enabled(on: bool, executable: str | None = None) -> Status:
    return enable(executable) if on else disable()
