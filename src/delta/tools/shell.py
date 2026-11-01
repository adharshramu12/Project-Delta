"""PowerShell tool with command risk classification.

Known read-only commands run automatically; anything else asks; commands that touch the OS,
security, disks, users or credentials are blocked outright.
"""

from __future__ import annotations

import asyncio
import re

from pydantic import BaseModel, Field

from delta.tools.base import Risk, Tool, ToolResult

SYSTEM_PATTERNS = [
    r"\bformat(-volume)?\b", r"\bdiskpart\b", r"\bbcdedit\b", r"\bclear-disk\b", r"\binitialize-disk\b",
    r"\breg(\.exe)?\s+(add|delete|import)\b", r"\b(set|remove|new)-itemproperty\s+.*hklm",
    r"\bset-mppreference\b", r"\badd-mppreference\b", r"\bnetsh\s+(advfirewall|firewall)\b",
    r"\bset-netfirewall", r"\bset-executionpolicy\b", r"\bnet\s+(user|localgroup)\b",
    r"\b(new|remove|set)-localuser\b", r"\bcmdkey\b", r"\bvssadmin\b", r"\bwbadmin\b", r"\bcipher\s+/w\b",
    r"\b(stop-computer|restart-computer|shutdown(\.exe)?)\b", r"\btakeown\b", r"\bicacls\b",
    r"\bsc(\.exe)?\s+(delete|config|stop)\b", r"\bsecedit\b", r"\bgpupdate\b", r"\bmanage-bde\b",
    r"invoke-webrequest.*\|\s*iex", r"\biex\s*\(", r"\binvoke-expression\b",
]
DESTRUCTIVE_PATTERNS = [
    r"\b(remove-item|rm|del|erase|rd|rmdir)\b", r"\bstop-process\b", r"\btaskkill\b", r"\bkill\b",
    r"\bgit\s+(reset\s+--hard|clean|push\s+.*--force)", r"\bclear-content\b", r"\bset-content\b",
    r"\bout-file\b", r">\s*[^&|]", r"\buninstall\b", r"\bwinget\s+uninstall\b",
]
EXTERNAL_PATTERNS = [r"\binvoke-(webrequest|restmethod)\b.*-method\s+(post|put|delete)", r"\bsend-mailmessage\b",
                     r"\bgit\s+push\b", r"\bcurl\b.*\s-(d|x\s*post)", r"\bscp\b", r"\bssh\b"]
READ_ONLY = re.compile(
    r"^\s*(get-[a-z]+|dir|ls|pwd|whoami|hostname|echo|type|cat|where(\.exe)?|test-path|select-string|"
    r"measure-object|resolve-path|systeminfo|ipconfig|nvidia-smi|python\s+--version|node\s+--version|"
    r"git\s+(status|log|diff|branch|remote\s+-v|show))\b",
    re.I,
)


def classify(command: str) -> Risk:
    c = command.lower()
    if any(re.search(p, c) for p in SYSTEM_PATTERNS):
        return Risk.SYSTEM
    if any(re.search(p, c) for p in DESTRUCTIVE_PATTERNS):
        return Risk.DESTRUCTIVE
    if any(re.search(p, c) for p in EXTERNAL_PATTERNS):
        return Risk.EXTERNAL
    parts = [s for s in re.split(r"[;|]|&&", command) if s.strip()]
    if parts and all(READ_ONLY.match(s) for s in parts):
        return Risk.READ
    return Risk.WRITE  # unknown commands are treated as modifying -> approval


class ShellArgs(BaseModel):
    command: str = Field(description="A PowerShell command")
    cwd: str | None = None
    timeout_s: int = Field(default=60, le=600)


class RunShell(Tool[ShellArgs]):
    name = "run_shell"
    description = "Run a PowerShell command and return stdout/stderr and exit code."
    risk = Risk.WRITE
    Args = ShellArgs

    def risk_for(self, args: ShellArgs) -> Risk:
        return classify(args.command)

    def paths(self, args: ShellArgs) -> list[str]:
        return [args.cwd] if args.cwd else []

    def scope(self, args: ShellArgs) -> str:
        words = args.command.strip().split()
        return words[0].lower() if words else ""

    def summary(self, args: ShellArgs) -> str:
        return f"run: {args.command}" + (f"  (in {args.cwd})" if args.cwd else "")

    async def run(self, args: ShellArgs) -> ToolResult:
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", args.command,
            cwd=args.cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), args.timeout_s)
        except TimeoutError:
            proc.kill()
            return ToolResult(False, f"timed out after {args.timeout_s}s", {"exit_code": None})
        text = out.decode(errors="replace")
        if err:
            text += "\n[stderr]\n" + err.decode(errors="replace")
        code = proc.returncode
        return ToolResult(code == 0, f"exit {code}\n{text.strip()}", {"exit_code": code})
