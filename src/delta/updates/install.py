"""Running an update: Delta's own installer (scripts/install.ps1), pinned to one commit, with the hashes it must find.

Both ways in end here: `delta update` in a terminal, and Update now in the dashboard. The dashboard cannot run the
installer itself (the installer closes the running Delta, and a child of Delta would be closed with it), so it
starts `python -m delta update --stop --from-dashboard ...` as a separate process, which starts the installer in its
own window (the progress) and exits. The installer then:
  - downloads https://github.com/<repo>/archive/<commit>.zip (that exact commit, over HTTPS),
  - checks the SHA-256 of pyproject.toml and uv.lock in it against what Delta read from that commit beforehand, and
    the version, and stops without changing anything if one differs,
  - closes Delta (your data folder is never touched), installs the exact locked packages, and opens Delta again.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404: runs only Delta's own installer and Delta itself, with fixed arguments
import sys
from pathlib import Path

from delta.updates.checker import REPO_RE, SHA_RE, Target

ROOT = Path(__file__).resolve().parents[3]  # the install folder (src/delta/updates/install.py)


def is_dev_checkout(root: Path = ROOT) -> bool:
    return (root / ".git").exists()


def supported() -> bool:
    """Update now runs Delta's Windows installer: Windows only."""
    return sys.platform == "win32"


def installer_command(root: Path, wait_pids: list[int], stop: bool, target: Target | None,
                      relaunch: bool = False) -> list[str]:
    """The installer's command line. Fixed words and values Delta checked; nothing from a web page or the model."""
    cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(root / "scripts" / "install.ps1"),
           "-FromGitHub", "-InstallDir", str(root), "-WaitForPid", ",".join(map(str, wait_pids))]
    if target is not None:
        if not (REPO_RE.match(target.repo) and SHA_RE.match(target.sha)):
            raise ValueError("not a pinned update target")
        cmd += ["-Repo", target.repo, "-Branch", target.sha, "-ExpectSha256", target.expect_arg(),
                "-ExpectVersion", target.version]
    if not relaunch:
        cmd.append("-NoLaunch")
    if stop:
        cmd.append("-StopRunning")
    return cmd


def run_installer(cmd: list[str]) -> None:
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen(cmd, creationflags=flags, close_fds=True)  # nosec B603: fixed command (installer_command)


def updater_command(target: Target) -> list[str]:
    """`python -m delta update` for the dashboard's Update now, pinned to `target`."""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")  # the service may run under pythonw.exe; either works without a window
    py = str(console if console.exists() else exe)
    return [py, "-m", "delta", "update", "--stop", "--from-dashboard", "--pin", target.sha, "--pin-repo", target.repo,
            "--pin-version", target.version, "--pin-sha256", target.expect_arg()]


def launch(target: Target) -> None:
    """Start the update for the dashboard and return at once. The tests replace this function."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    env = {k: v for k, v in os.environ.items() if k not in ("DELTA_REF",)}  # the pin decides, nothing else
    subprocess.Popen(updater_command(target), creationflags=flags, close_fds=True, cwd=str(ROOT), env=env,  # nosec B603
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def parse_expect(text: str) -> tuple[str, str] | None:
    """pyproject.toml=<hex>;uv.lock=<hex> -> (pyproject hash, lock hash), or None if it is not exactly that."""
    parts = dict(p.split("=", 1) for p in text.split(";") if "=" in p)
    py, lock = parts.get("pyproject.toml", "").lower(), parts.get("uv.lock", "").lower()
    ok = len(parts) == 2 and all(len(h) == 64 and all(c in "0123456789abcdef" for c in h) for h in (py, lock))
    return (py, lock) if ok else None
