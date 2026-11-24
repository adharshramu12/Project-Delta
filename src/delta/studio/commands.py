"""Running a command in a Build project: tests, builds, installs, linters (milestone 2).

Safety, in order:
1. Refused outright, whatever the user allows: commands that act on the whole PC or on things outside a project
   (formatting disks, shutting down, the registry, user accounts, firewall, scheduled tasks, killing processes,
   deleting from a drive root or the home folder, downloading and running a script in one go). REFUSED below.
2. Every other command needs the user's yes in the Build page, unless the user ticked "always allow" for that exact
   command in this project. A command that sends something off the PC (git push, npm publish, uploads) is marked so
   the user sees it, and can never be "always allowed".
3. It runs in the project folder, with a time limit, without Delta's own keys in its environment
   (delta.tools.shell.scrubbed_env), and the whole process tree is stopped on time-out or Stop
   (delta.system.proctree). Its output is cut to the end, where errors are.

Commands that keep running (a dev server, a watcher) are not for this tool: they would only time out. Live preview
is milestone 3.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from delta.system import proctree
from delta.tools.shell import scrubbed_env

DEFAULT_TIMEOUT_S = 180
MAX_TIMEOUT_S = 900
OUTPUT_CHARS = 8_000    # the end of the output is what the model gets
MAX_COMMAND = 2_000

REFUSED = re.compile(
    r"\b(format(-volume)?|diskpart|bcdedit|shutdown|restart-computer|stop-computer|reg(\.exe)?\s+(add|delete|import)|"
    r"(set|new|remove)-itemproperty\s+.*hk(lm|cu)|net\s+(user|localgroup)|(new|remove|set)-localuser|"
    r"netsh\s+advfirewall|set-netfirewall|schtasks|register-scheduledtask|sc(\.exe)?\s+(create|delete|config)|"
    r"set-executionpolicy|taskkill|stop-process|cipher\s+/w|vssadmin|wmic|mklink|icacls|takeown|"
    r"invoke-expression|iex)\b"
    r"|(curl|wget|iwr|invoke-webrequest|irm|invoke-restmethod)\b[^|;&]*\|\s*(sh|bash|iex|powershell|pwsh|python)\b"
    # a delete with any argument that starts outside the project: a drive, the root, home, or the parent folder.
    # Windows switches (/s, /q) are single letters after a slash and are not paths.
    r"|\b(rm|del|erase|rmdir|rd|ri|remove-item)\b[^;&|]*?[\s=]['\"]?"
    r"([a-z]:|[\\/](?![a-z](\s|$))|~|\$home|\$env:|%userprofile%|%systemroot%|\.\.)",
    re.I)
SENDS = re.compile(r"\b(git\s+push|npm\s+publish|yarn\s+publish|pnpm\s+publish|twine\s+upload|"
                   r"cargo\s+publish|docker\s+push|gh\s+(release|pr|repo)\b|vercel|netlify\s+deploy|firebase\s+deploy|"
                   r"scp|rsync|ftp|curl\s+.*(-T|--upload-file|-d|--data|-F)|invoke-webrequest\s+.*-method\s+(post|put))\b",
                   re.I)


@dataclass
class Outcome:
    exit_code: int | None
    output: str
    seconds: float
    timed_out: bool = False

    def for_model(self) -> str:
        head = (f"timed out after {self.seconds:.0f}s and was stopped" if self.timed_out
                else f"exit code {self.exit_code} after {self.seconds:.1f}s")
        return f"{head}\n{self.output or '(no output)'}"


def check(command: str) -> tuple[str, bool]:
    """(why it is refused, or "") and whether it sends something off the PC."""
    text = " ".join(str(command or "").split())
    if not text:
        return "The command is empty.", False
    if len(text) > MAX_COMMAND:
        return f"The command is longer than {MAX_COMMAND} characters.", False
    if REFUSED.search(text):
        return ("Delta Build never runs this: it acts on the whole PC or on files outside a project "
                "(disks, shutdown, registry, accounts, firewall, scheduled tasks, other processes, or deleting outside "
                "the project). Run it yourself if you really need it."), False
    return "", bool(SENDS.search(text))


def _tail(text: str) -> str:
    text = text.replace("\r\n", "\n").strip()
    if len(text) <= OUTPUT_CHARS:
        return text
    return "... (earlier output cut)\n" + text[-OUTPUT_CHARS:]


async def run(command: str, cwd: Path, timeout_s: float = DEFAULT_TIMEOUT_S) -> Outcome:
    """Run it in the project folder and return how it ended. A Stop (cancel) kills the whole process tree."""
    timeout = max(5.0, min(float(timeout_s or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    if sys.platform == "win32":
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        argv = ["/bin/sh", "-c", command]
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(*argv, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                env=scrubbed_env())
    proctree.track(getattr(proc, "pid", None))
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        await asyncio.to_thread(proctree.kill_process, proc)
        return Outcome(None, "", time.monotonic() - started, timed_out=True)
    except asyncio.CancelledError:
        await asyncio.to_thread(proctree.kill_process, proc)
        raise
    return Outcome(proc.returncode, _tail(out.decode("utf-8", errors="replace")), time.monotonic() - started)
