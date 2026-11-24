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

# Programs that act on the whole PC: refused wherever they appear as a word of the command.
REFUSED_WORDS = {"format", "format-volume", "diskpart", "bcdedit", "shutdown", "restart-computer", "stop-computer",
                 "new-localuser", "remove-localuser", "set-localuser", "set-netfirewallrule", "set-netfirewallprofile",
                 "schtasks", "register-scheduledtask", "set-executionpolicy", "taskkill", "stop-process", "vssadmin",
                 "wmic", "mklink", "icacls", "takeown", "invoke-expression", "iex"}
# ... and these when followed by the given next word.
REFUSED_PAIRS = {("reg", "add"), ("reg", "delete"), ("reg", "import"), ("reg.exe", "add"), ("reg.exe", "delete"),
                 ("reg.exe", "import"), ("net", "user"), ("net", "localgroup"), ("netsh", "advfirewall"),
                 ("sc", "create"), ("sc", "delete"), ("sc", "config"), ("sc.exe", "create"), ("sc.exe", "delete"),
                 ("sc.exe", "config"), ("cipher", "/w")}
REGISTRY_EDITS = {"set-itemproperty", "new-itemproperty", "remove-itemproperty", "new-item", "remove-item"}
DOWNLOADERS = {"curl", "curl.exe", "wget", "iwr", "invoke-webrequest", "irm", "invoke-restmethod"}
SHELLS = {"sh", "bash", "iex", "invoke-expression", "powershell", "pwsh", "python", "python3", "cmd"}
DELETERS = {"rm", "del", "erase", "rmdir", "rd", "ri", "remove-item"}
OUTSIDE = ("~", "$home", "$env:", "%userprofile%", "%systemroot%", "..")
SENDING_PAIRS = {("git", "push"), ("npm", "publish"), ("yarn", "publish"), ("pnpm", "publish"), ("twine", "upload"),
                 ("cargo", "publish"), ("docker", "push"), ("gh", "release"), ("gh", "pr"), ("gh", "repo"),
                 ("netlify", "deploy"), ("firebase", "deploy")}
SENDING_WORDS = {"vercel", "scp", "rsync", "ftp", "sftp"}
UPLOAD_FLAGS = {"-t", "--upload-file", "-d", "--data", "--data-binary", "-f", "--form"}


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
    if _refused(text):
        return ("Delta Build never runs this: it acts on the whole PC or on files outside a project "
                "(disks, shutdown, registry, accounts, firewall, scheduled tasks, other processes, or deleting outside "
                "the project). Run it yourself if you really need it."), False
    return "", _sends(text)


def _segments(text: str) -> list[list[str]]:
    """The command split into its parts (at ; & | and newlines), each a list of lower-case words without quotes."""
    out: list[list[str]] = []
    part: list[str] = []
    word = ""
    for ch in text.lower():
        if ch in ";&|\n":
            if word:
                part.append(word)
            if part:
                out.append(part)
            part, word = [], ""
            if ch == "|":
                out.append(["|"])
        elif ch.isspace():
            if word:
                part.append(word)
            word = ""
        elif ch not in "'\"`()":
            word += ch
    if word:
        part.append(word)
    if part:
        out.append(part)
    return out


def _outside_path(arg: str) -> bool:
    """An argument that starts outside the project: a drive, the root, home or the parent folder. Windows switches
    (/s, /q: a slash and one letter) are not paths."""
    a = arg.split("=", 1)[-1]
    if len(a) >= 2 and a[0].isalpha() and a[1] == ":":
        return True
    if a[:1] in ("/", "\\"):
        return not (len(a) == 2 and a[1].isalpha())
    return a.startswith(OUTSIDE)


def _refused(text: str) -> bool:
    segs = _segments(text)
    for i, words in enumerate(segs):
        for j, w in enumerate(words):
            if w in REFUSED_WORDS or (j + 1 < len(words) and (w, words[j + 1]) in REFUSED_PAIRS):
                return True
            if w in REGISTRY_EDITS and any(x.startswith(("hklm", "hkcu", "registry::")) for x in words[j + 1:]):
                return True
        if words and words[0] in DELETERS and any(_outside_path(a) for a in words[1:] if not a.startswith("-")):
            return True
        if words and words[0] in DOWNLOADERS and i + 2 < len(segs) and segs[i + 1] == ["|"] \
                and segs[i + 2] and segs[i + 2][0] in SHELLS:
            return True
    return False


def _sends(text: str) -> bool:
    for words in _segments(text):
        for j, w in enumerate(words):
            if w in SENDING_WORDS or (j + 1 < len(words) and (w, words[j + 1]) in SENDING_PAIRS):
                return True
        if words and words[0] in DOWNLOADERS and any(x in UPLOAD_FLAGS for x in words[1:]):
            return True
        if words and words[0] in ("invoke-webrequest", "iwr", "invoke-restmethod", "irm") and \
                any(x in ("post", "put") for x in words[1:]):
            return True
    return False


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
