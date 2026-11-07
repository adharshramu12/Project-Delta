"""`delta doctor` setup checks: is this PC ready to run Delta, and if not, the exact fix in plain words.

Each check is quick and works offline (no model is called, nothing is installed or changed). A check is
  ok    fine
  info  worth knowing, nothing to do
  warn  Delta works, but something the user expects will not (no browser, no AI model yet, no delta command)
  fail  Delta cannot work properly until it is fixed (its data folder is read-only, another program has its port)
Only `fail` makes `delta doctor` exit with an error, so a new install that is not set up yet still passes.

Every probe is a parameter of `run_checks`, so the tests can play any PC (a missing browser, a busy port, a
Store Python) without touching the real one.
"""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]  # the Delta folder (src/delta/setup/doctor.py)


@dataclass
class Check:
    name: str
    status: str  # ok | info | warn | fail
    detail: str
    fix: str = ""


def _port_in_use(host: str, port: int) -> bool:
    with contextlib.suppress(OSError), socket.create_connection((host, port), timeout=0.3):
        return True
    return False


def _writable(folder: Path) -> str:
    """'' when a file can be made and removed in `folder` (made if missing), else the reason."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".doctor-", dir=folder)
        os.close(fd)
        os.remove(name)
        return ""
    except OSError as e:
        return e.strerror or str(e)


def _smart_app_control() -> int | None:
    """Windows 11 Smart App Control: 1 on, 2 evaluation, 0 off; None when not known (older Windows, not Windows)."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\CI\Policy") as k:
            return int(winreg.QueryValueEx(k, "VerifiedAndReputablePolicyState")[0])
    except (OSError, ValueError, ImportError):
        return None


def _signed(exe: str) -> bool | None:
    """Whether Windows says `exe` carries a valid signature (PowerShell's Get-AuthenticodeSignature); None if
    it cannot tell."""
    ps = shutil.which("powershell.exe")
    if not ps:
        return None
    try:
        out = subprocess.run(  # nosec B603: a fixed command; the path is passed as an environment value
            [ps, "-NoProfile", "-NonInteractive", "-Command",
             "(Get-AuthenticodeSignature -LiteralPath $env:DELTA_DOCTOR_EXE).Status.ToString()"],
            capture_output=True, text=True, timeout=30, check=False,
            env={**os.environ, "DELTA_DOCTOR_EXE": exe})
    except (OSError, subprocess.SubprocessError):
        return None
    status = out.stdout.strip().splitlines()[-1:] or [""]
    return status[0] == "Valid" if status[0] else None


def _ocr() -> bool | None:
    import asyncio

    from delta.perception.ocr import WindowsOcr

    ocr = WindowsOcr("auto")
    if not ocr.platform_ok():
        return None
    try:
        return asyncio.run(asyncio.wait_for(ocr.probe(), 40))
    except Exception:
        return False


def _browsers() -> Any:
    from delta.tools.browser_choice import detect

    return detect()


def _secret(name: str) -> str | None:
    from delta.system.secrets import get_secret

    try:
        return get_secret(name)
    except Exception:
        return None


def _running(data_dir: Path) -> int | None:
    from delta.__main__ import running_delta

    try:
        return running_delta(data_dir)
    except Exception:
        return None


def _cli_found(name: str, cfg: Any) -> str | None:
    from delta.llm.cli_providers import CLI_KINDS

    try:
        return CLI_KINDS[cfg.kind](name, cfg).executable()
    except Exception:
        return None


def _fix_command(root: Path) -> str:
    return (f'powershell -NoProfile -ExecutionPolicy Bypass -File "{root / "scripts" / "delta_command.ps1"}" '
            "-Action Install")


def run_checks(settings: Any, *, root: Path = ROOT, which: Callable[[str], str | None] = shutil.which,
               python: str = sys.executable, version: tuple[int, int] = sys.version_info[:2],
               machine: str = "", port_in_use: Callable[[str, int], bool] = _port_in_use,
               running: Callable[[Path], int | None] = _running, writable: Callable[[Path], str] = _writable,
               browsers: Callable[[], Any] = _browsers, ocr: Callable[[], bool | None] = _ocr,
               secret: Callable[[str], str | None] = _secret, sac: Callable[[], int | None] = _smart_app_control,
               signed: Callable[[str], bool | None] = _signed,
               cli_found: Callable[[str, Any], str | None] = _cli_found,
               free_gb: Callable[[Path], float] | None = None) -> list[Check]:
    out: list[Check] = []
    machine = machine or platform.machine()
    installed = not (root / ".git").exists()
    reinstall = ("run the installer again: irm https://raw.githubusercontent.com/adharshramu12/Project-Delta/main/"
                 "scripts/install.ps1 | iex" if installed else "run: uv sync --extra all")

    # Python ------------------------------------------------------------------------------------------------
    py = f"Python {version[0]}.{version[1]} ({machine}) at {python}"
    if version != (3, 12):
        out.append(Check("Python", "fail", py, f"Delta needs Python 3.12; {reinstall}"))
    elif "\\windowsapps\\" in python.lower():
        out.append(Check("Python", "warn", py + " (the Microsoft Store Python, which keeps Delta's data elsewhere)",
                         f"Use the python.org Python 3.12: {reinstall}"))
    else:
        out.append(Check("Python", "ok", py))
    state = sac()
    if state == 1:
        ok = signed(python)
        if ok is False:
            out.append(Check("Smart App Control", "fail",
                             "on, and Delta's Python is not signed, so Windows blocks it when started from the "
                             "Desktop icon", f"Rebuild Delta on the official signed Python: {reinstall}"))
        else:
            out.append(Check("Smart App Control", "ok", "on; Delta's Python is signed"
                             if ok else "on (could not read Python's signature)"))

    # Install, the delta command ----------------------------------------------------------------------------
    out.append(Check("Delta folder", "info", f"{root} ({'installed copy' if installed else 'developer checkout'})"))
    found = which("delta")
    shim = root / "bin" / "delta.cmd"
    if found is None:
        fix = (f"Open a NEW PowerShell window and try again. If it still says 'delta is not recognized': "
               f"{_fix_command(root)}  (or run Delta as \"{shim}\")") if installed else \
            "In a developer checkout use: uv run delta"
        out.append(Check("delta command", "warn", "not on your Path in this window", fix))
    else:
        f = Path(found)
        ours = root in f.resolve().parents or f.resolve() == shim.resolve()
        if ours or not installed:
            out.append(Check("delta command", "ok", str(f)))
        else:
            out.append(Check("delta command", "warn", f"'delta' runs another program: {f}",
                             f"Use the full path instead: \"{shim}\", or remove the other one from your Path"))

    # Folders, settings, disk -------------------------------------------------------------------------------
    data = Path(settings.data_dir)
    why = writable(data)
    out.append(Check("Data folder", "fail" if why else "ok", f"{data}" + (f" is not writable: {why}" if why else ""),
                     "Give your account write access to that folder, or close the program that locks it "
                     "(antivirus, OneDrive 'Files On-Demand' errors), then run delta doctor again." if why else ""))
    out.append(Check("Settings", "ok", f"read from {settings.config_dir}"))
    free = free_gb(data) if free_gb else shutil.disk_usage(data if data.exists() else data.anchor or ".").free / 1e9
    if free < 1:
        out.append(Check("Disk space", "warn", f"{free:.1f} GB free where Delta keeps its data",
                         "Free some space (Settings > System > Storage, or empty the Recycle Bin)."))

    # Port -------------------------------------------------------------------------------------------------
    host, port = settings.api.host, settings.api.port
    if port_in_use(host, port):
        pid = running(data)
        if pid is not None:
            out.append(Check("Dashboard port", "ok", f"{host}:{port} (Delta is running, process {pid})"))
        else:
            out.append(Check("Dashboard port", "fail", f"{host}:{port} is used by another program",
                             f"Close that program, or give Delta another port: add  api: {{port: 7879}}  to "
                             f"{data / 'settings.yaml'}, then start Delta again."))
    else:
        out.append(Check("Dashboard port", "ok", f"{host}:{port} is free (Delta is not running)"))

    # Browser, OCR ------------------------------------------------------------------------------------------
    try:
        inst = browsers()
        names = [n for n in ("chrome", "edge") if inst.has(n)]
        if not names:
            out.append(Check("Browser", "warn", "neither Google Chrome nor Microsoft Edge was found",
                             "Install Microsoft Edge or Google Chrome: Delta reads and uses websites through one."))
        elif not inst.automation:
            out.append(Check("Browser", "warn", f"{', '.join(names)} found, but Delta's browser control "
                             "(Playwright) is not installed", reinstall))
        else:
            out.append(Check("Browser", "ok", ", ".join(n.title() for n in names)))
    except Exception as e:
        out.append(Check("Browser", "warn", f"could not check: {e}"))
    seen = ocr()
    if seen is None:
        out.append(Check("Screen text (OCR)", "info", "Windows OCR is not available here (Windows only); Delta "
                         "reads windows through their controls instead"))
    elif seen:
        out.append(Check("Screen text (OCR)", "ok", "Windows OCR works"))
    else:
        out.append(Check("Screen text (OCR)", "warn", "Windows OCR did not answer",
                         "Add an OCR language: Settings > Time & language > Language & region > your language > "
                         "Language options > Optical character recognition. Delta still works without it."))

    # Phone (Telegram) --------------------------------------------------------------------------------------
    tg = settings.channels.get("telegram")
    if tg is None or not tg.enabled:
        out.append(Check("Telegram", "info", "off (turn it on in Delta's Settings > Phone to use Delta from your "
                         "phone)"))
    elif not secret("telegram_bot_token"):
        out.append(Check("Telegram", "warn", "on, but no bot token is saved",
                         "Paste the token from @BotFather in Delta's Settings > Phone, or run: "
                         "delta secrets set telegram_bot_token"))
    elif not tg.allowed_user_ids:
        out.append(Check("Telegram", "warn", "on, token saved, but no phone is paired yet",
                         "In Delta's Settings > Phone press 'Pair a phone' and send the code to your bot."))
    else:
        out.append(Check("Telegram", "ok", f"on, {len(tg.allowed_user_ids)} paired"))

    # AI models ---------------------------------------------------------------------------------------------
    ready: list[str] = []
    for name, p in settings.llm.providers.items():
        if p.kind != "openai":
            if cli_found(name, p):
                ready.append(name)
        elif p.local:
            continue  # local servers are checked (and reported) by the model section below, over the network
        elif p.secret and secret(p.secret):
            ready.append(name)
    local = [n for n, p in settings.llm.providers.items() if p.kind == "openai" and p.local]
    preferred = settings.llm.preferred
    if preferred not in ("", "auto") and preferred not in settings.llm.providers:
        out.append(Check("Main AI", "warn", f"'{preferred}' is chosen as the main AI but is not configured",
                         "Pick the main AI again in Delta's Settings > AI."))
    if ready:
        out.append(Check("AI models", "ok", "ready: " + ", ".join(ready)))
    else:
        out.append(Check("AI models", "warn", "no cloud key or AI app is set up yet" +
                         (f" (local: {', '.join(local)}, checked below)" if local else ""),
                         "Open Delta and follow the setup page (a free Groq key takes a minute), or run: "
                         "delta secrets set groq_api_key"))
    return out


LABEL = {"ok": "OK  ", "info": "INFO", "warn": "WARN", "fail": "FAIL"}


def render(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"  {LABEL.get(c.status, c.status.upper()):<4}  {c.name}: {c.detail}")
        if c.fix:
            lines.append(f"        Fix: {c.fix}")
    return "\n".join(lines)
