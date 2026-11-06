"""The installer on a PC whose execution policy runs no scripts (CI job e2e-windows).

A new Windows PC has the execution policy Restricted: no .ps1 file runs. `irm .../install.ps1 | iex` runs the
installer as text, which that policy does not cover, but step 5 used to start scripts\\delta_command.ps1 and
scripts\\create_shortcut.ps1 as files, and both failed with "running scripts is disabled on this system".

Here the runner's account gets the policy Restricted (Set-ExecutionPolicy -Scope CurrentUser, put back afterwards;
the PC-wide LocalMachine policy is left as the runner has it), checked to be the policy a new PowerShell gets. Then:
- the control: starting delta_command.ps1 as a file is refused, so this really is the owner's setup,
- the installer's text runs step 5 (the delta command and the Desktop and Start-menu icons) in a PowerShell
  started without -ExecutionPolicy, against a throwaway install folder whose name has a space, an apostrophe, `&`
  and a non-ASCII letter; it must succeed, `delta --version` must work from a new process, and the shortcuts must
  start Python, not a script,
- `delta update`'s exact command line and `Install Delta.cmd` start a stand-in installer under that policy,
- the installer's Unblock-File step clears the "downloaded from the internet" mark from the installed files.
Steps 1 to 4 (Python, files, packages) need no script files and are not run here (they download Python and every
package). The user Path, the account's policy and the shortcuts are changed for real, so this only runs on a CI
runner (GITHUB_ACTIONS) and puts every one of them back afterwards."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import warnings
import zipfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import delta

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DELTA_E2E") != "1", reason="e2e: set DELTA_E2E=1"),
    pytest.mark.skipif(sys.platform != "win32", reason="Windows only"),
    pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") != "true",
                       reason="changes the account's execution policy, Path and shortcuts: CI runners only"),
]

NEW_WINDOW_PATH = ("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false; "
                   "$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + "
                   "[Environment]::GetEnvironmentVariable('Path', 'User'); ")
DISABLED = ("running scripts is disabled", "cannot be loaded")


def _env(**extra: str) -> dict[str, str]:
    # A PowerShell started from PowerShell 7 (the runner's shell) inherits its PSModulePath, and Windows
    # PowerShell 5.1 would then try PowerShell 7's modules. A new window has none of that.
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    env.update(extra)
    return env


def _shells() -> list[str]:
    return [s for s in (shutil.which("powershell.exe"), shutil.which("pwsh")) if s]


def _run(shell: str, command: str, **extra: str) -> subprocess.CompletedProcess[str]:
    """A new PowerShell as a user opens it: no -ExecutionPolicy, so the account's policy applies."""
    return subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command",
                           "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false; " + command],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                          env=_env(**extra), check=False)


def _policy(shell: str, scope: str | None = None) -> str:
    arg = f" -Scope {scope}" if scope else ""
    out = _run(shell, f"(Get-ExecutionPolicy{arg}).ToString()")
    assert out.returncode == 0, out.stderr + out.stdout
    return out.stdout.lstrip("\ufeff").strip().splitlines()[-1]


@contextlib.contextmanager
def _restricted(shell: str) -> Iterator[None]:
    before = _policy(shell, "CurrentUser")
    try:
        out = _run(shell, "Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy Restricted -Force")
        assert out.returncode == 0, out.stderr + out.stdout
        effective = _policy(shell)
        assert effective == "Restricted", (
            f"{shell}: the policy a new PowerShell gets is {effective}, not Restricted "
            f"(LocalMachine {_policy(shell, 'LocalMachine')}, a group policy, or PSExecutionPolicyPreference)")
        yield
    finally:
        _run(shell, f"Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy {before} -Force")
        assert _policy(shell, "CurrentUser") == before


def _user_path() -> tuple[str, int] | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            value, kind = winreg.QueryValueEx(k, "Path")
            return str(value), int(kind)
    except FileNotFoundError:
        return None


@pytest.fixture
def user_path() -> Iterator[None]:
    import winreg

    before = _user_path()
    try:
        yield
    finally:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            if before is None:
                with contextlib.suppress(FileNotFoundError):
                    winreg.DeleteValue(k, "Path")
            else:
                winreg.SetValueEx(k, "Path", 0, before[1], before[0])


def _known_folder(name: str) -> Path:
    out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                          f"[Environment]::GetFolderPath('{name}')"], capture_output=True, text=True,
                         timeout=60, env=_env(), check=False)
    return Path(out.stdout.strip())


@pytest.fixture
def shortcuts() -> Iterator[dict[str, Path]]:
    """Where create_shortcut.ps1 puts its three shortcuts; any that were there already are put back after."""
    paths = {"desktop": _known_folder("Desktop") / "Delta.lnk",
             "start": _known_folder("Programs") / "Delta.lnk",
             "update": _known_folder("Programs") / "Delta" / "Update Delta.lnk"}
    saved = {k: p.read_bytes() for k, p in paths.items() if p.exists()}
    menu_existed = paths["update"].parent.exists()
    try:
        yield paths
    finally:
        for k, p in paths.items():
            if k in saved:
                p.write_bytes(saved[k])
            else:
                with contextlib.suppress(FileNotFoundError):
                    p.unlink()
        if not menu_existed:
            with contextlib.suppress(OSError):
                paths["update"].parent.rmdir()


def _install_folder(base: Path) -> Path:
    venv = ROOT / ".venv"
    assert (venv / "Scripts" / "pythonw.exe").exists(), "run `uv sync` first"
    install = base / "R\u0101j D'Souza & Co" / "Programs" / "Delta"
    (install / "scripts").mkdir(parents=True)
    for script in SCRIPTS.glob("*.ps1"):
        shutil.copy(script, install / "scripts" / script.name)
    import _winapi

    _winapi.CreateJunction(str(venv), str(install / ".venv"))
    return install


def _lnk(path: Path) -> tuple[str, str]:
    out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                          "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false; "
                          "$l = (New-Object -ComObject WScript.Shell).CreateShortcut($env:DELTA_T_LNK); "
                          "$l.TargetPath; $l.Arguments"], capture_output=True, text=True, encoding="utf-8",
                         timeout=60, env=_env(DELTA_T_LNK=str(path)), check=False)
    assert out.returncode == 0, out.stderr
    target, args = [*out.stdout.lstrip("\ufeff").splitlines(), "", ""][:2]
    return target.strip(), args.strip()


# --- step 5 of the installer under Restricted ------------------------------------------------------------------
@pytest.mark.parametrize("shell", _shells())
def test_step5_works_under_restricted_policy(shell, tmp_path, user_path, shortcuts):
    install = _install_folder(tmp_path)
    try:
        with _restricted(shell):
            # The control: a script file is refused here, as on the owner's PC.
            refused = _run(shell, "& $env:DELTA_T_SCRIPT -Action Install -InstallDir $env:DELTA_T_DIR -NoPath",
                           DELTA_T_SCRIPT=str(install / "scripts" / "delta_command.ps1"), DELTA_T_DIR=str(install))
            assert refused.returncode != 0 and any(d in refused.stderr for d in DISABLED), refused.stderr
            assert not (install / "bin").exists()

            # The installer as text (as irm ... | iex runs it), then its step 5.
            got = _run(shell,
                       "$ErrorActionPreference = 'Stop'; "
                       "$text = [System.IO.File]::ReadAllText($env:DELTA_T_INSTALLER); "
                       ". ([scriptblock]::Create($text)) -DefineOnly -InstallDir $env:DELTA_T_DIR; "
                       "$r = Install-DeltaShortcuts $InstallDir $false $false; "
                       "'FIXES=' + $r.Fixes.Count; foreach ($f in $r.Fixes) { 'FIX ' + $f }; "
                       "if ($r.Fixes.Count) { exit 3 }",
                       DELTA_T_INSTALLER=str(SCRIPTS / "install.ps1"), DELTA_T_DIR=str(install))
            text = got.stdout + got.stderr
            assert got.returncode == 0, text
            assert "FIXES=0" in got.stdout and "could not" not in text, text
            assert "OK  delta command added" in got.stdout and "OK  Delta icon added" in got.stdout, text

            # `delta --version` from a new process, the Path read from the registry as a new window does.
            new = _run(shell, NEW_WINDOW_PATH + "(Get-Command delta).Source; delta --version; exit $LASTEXITCODE")
            assert new.returncode == 0, new.stderr + new.stdout
            lines = new.stdout.lstrip("\ufeff").strip().splitlines()
            assert Path(lines[0]).resolve() == (install / "bin" / "delta.cmd").resolve(), new.stdout
            assert delta.__version__ in new.stdout

        # The shortcuts start Python directly: nothing a Restricted policy or a script check could refuse.
        # On the CI runner the target read back through WScript.Shell has the folder's non-ASCII letter as a plain
        # one, so the folder is compared by its tail, and the .lnk file is checked (reported only) for the folder's
        # full Unicode name.
        for name, exe, args in (("desktop", "pythonw.exe", "-m delta app"), ("start", "pythonw.exe", "-m delta app"),
                                ("update", "python.exe", "-m delta update --stop")):
            target, got_args = _lnk(shortcuts[name])
            assert target.lower().endswith(f"\\programs\\delta\\.venv\\scripts\\{exe}"), (name, target)
            assert got_args == args, (name, got_args)
            if "R\u0101j D'Souza & Co".encode("utf-16-le") not in shortcuts[name].read_bytes():
                warnings.warn(f"{name} shortcut: no Unicode copy of the folder name in the .lnk", stacklevel=1)
    finally:
        os.rmdir(install / ".venv")  # the junction only, never this checkout's .venv


# --- what starts the installer: delta update, the Start-menu item, Install Delta.cmd ----------------------------
PROBE = (
    "param([string]$Source = '', [string]$Repo = '', [string]$Branch = '', [string]$InstallDir = '',\r\n"
    "      [switch]$NoLaunch, [switch]$NoShortcuts, [switch]$NoPath, [switch]$FromGitHub, [switch]$StopRunning,\r\n"
    "      [string]$WaitForPid = '')\r\n"
    "$o = [ordered]@{ Source = $Source; InstallDir = $InstallDir; NoLaunch = [bool]$NoLaunch;\r\n"
    "  FromGitHub = [bool]$FromGitHub; StopRunning = [bool]$StopRunning; WaitForPid = $WaitForPid;\r\n"
    "  Account = (Get-ExecutionPolicy -Scope CurrentUser).ToString() }\r\n"
    "[System.IO.File]::WriteAllText($env:DELTA_T_PROBE_OUT, ($o | ConvertTo-Json), "
    "(New-Object System.Text.UTF8Encoding $false))\r\n"
)


def test_delta_update_and_install_delta_cmd_start_the_installer_under_restricted_policy(tmp_path, monkeypatch):
    import delta.__main__ as cli

    root = tmp_path / "R\u0101j D'Souza & Co" / "Programs" / "Delta"  # an installed copy: no .git
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "install.ps1").write_text(PROBE, encoding="utf-8-sig", newline="")
    calls: list[Any] = []
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(cli, "running_delta", lambda data_dir: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    assert cli._update(stop=True, root=root) == 0  # what `delta update --stop` and "Update Delta" start
    monkeypatch.undo()
    cmd = calls[0][0][0]

    cmd_dir = tmp_path / "Zip D'Souza" / "Delta-main"  # an extracted zip
    (cmd_dir / "scripts").mkdir(parents=True)
    (cmd_dir / "scripts" / "install.ps1").write_text(PROBE, encoding="utf-8-sig", newline="")
    shutil.copy(ROOT / "Install Delta.cmd", cmd_dir / "Install Delta.cmd")

    with _restricted("powershell.exe"):
        out_file = tmp_path / "update.json"
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False,
                             env=_env(DELTA_T_PROBE_OUT=str(out_file)))
        assert run.returncode == 0, run.stderr + run.stdout
        got = json.loads(out_file.read_text(encoding="utf-8"))
        assert got["InstallDir"] == str(root) and got["FromGitHub"] and got["NoLaunch"] and got["StopRunning"]
        assert got["WaitForPid"] and got["Account"] == "Restricted"  # the account's policy is left as it is

        out_file = tmp_path / "cmd.json"
        run = subprocess.run(["cmd.exe", "/d", "/c", "call", str(cmd_dir / "Install Delta.cmd")],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False,
                             env=_env(DELTA_T_PROBE_OUT=str(out_file)))
        assert run.returncode == 0, run.stderr + run.stdout
        got = json.loads(out_file.read_text(encoding="utf-8"))
        assert Path(got["Source"]).resolve() == cmd_dir.resolve() and got["Account"] == "Restricted"


# --- the "downloaded from the internet" mark ----------------------------------------------------------------------
ZONE = "[ZoneTransfer]\r\nZoneId=3\r\n"


def _marked(path: Path) -> bool:
    return os.path.exists(f"{path}:Zone.Identifier")


def _mark(path: Path) -> None:
    with open(f"{path}:Zone.Identifier", "w", encoding="ascii") as f:
        f.write(ZONE)


def test_installer_unblocks_installed_files_under_restricted_policy(tmp_path):
    install = tmp_path / "Delta"
    (install / "scripts").mkdir(parents=True)
    (install / ".venv" / "Scripts").mkdir(parents=True)
    script, cmd = install / "scripts" / "x.ps1", install / "Install Delta.cmd"
    venv_file = install / ".venv" / "Scripts" / "y"
    for f in (script, cmd, venv_file):
        f.write_text("x", encoding="ascii")
        _mark(f)
        assert _marked(f)
    with _restricted("powershell.exe"):
        got = _run("powershell.exe",
                   "$ErrorActionPreference = 'Stop'; "
                   "$text = [System.IO.File]::ReadAllText($env:DELTA_T_INSTALLER); "
                   ". ([scriptblock]::Create($text)) -DefineOnly -InstallDir $env:DELTA_T_DIR; "
                   "Unblock-DeltaFiles $InstallDir",
                   DELTA_T_INSTALLER=str(SCRIPTS / "install.ps1"), DELTA_T_DIR=str(install))
        assert got.returncode == 0, got.stderr + got.stdout
    assert not _marked(script) and not _marked(cmd)
    assert _marked(venv_file)  # the environment is left alone


def test_where_the_mark_travels(tmp_path):
    """Informational (reported as warnings): does Expand-Archive or robocopy carry a zip's mark to its files, and
    does Invoke-WebRequest mark a download? The installer unblocks its files either way."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.ps1").write_text("'a'", encoding="ascii")
    zpath = tmp_path / "d.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.write(src / "a.ps1", "Delta-main/scripts/a.ps1")
    _mark(zpath)
    _mark(src / "a.ps1")
    dl = tmp_path / "dl.txt"
    with _restricted("powershell.exe"):
        got = _run("powershell.exe",
                   "$ProgressPreference = 'SilentlyContinue'; "
                   "try { Expand-Archive $env:DELTA_T_ZIP -DestinationPath $env:DELTA_T_OUT -Force } "
                   "catch { 'EXPAND FAILED ' + $_ }; "
                   "robocopy $env:DELTA_T_SRC $env:DELTA_T_COPY /MIR /NFL /NDL /NJH /NJS /NP | Out-Null; "
                   "try { Invoke-WebRequest 'https://raw.githubusercontent.com/adharshramu12/Project-Delta/main/"
                   "LICENSE' -OutFile $env:DELTA_T_DL -UseBasicParsing } catch { 'DOWNLOAD FAILED ' + $_ }; exit 0",
                   DELTA_T_ZIP=str(zpath), DELTA_T_OUT=str(tmp_path / "out"), DELTA_T_SRC=str(src),
                   DELTA_T_COPY=str(tmp_path / "copy"), DELTA_T_DL=str(dl))
    expanded = tmp_path / "out" / "Delta-main" / "scripts" / "a.ps1"
    report = {
        "expand_archive_ran_under_restricted": got.returncode == 0 and expanded.exists(),
        "expand_archive_carries_mark": expanded.exists() and _marked(expanded),
        "robocopy_carries_mark": _marked(tmp_path / "copy" / "a.ps1"),
        "invoke_webrequest_marks_download": dl.exists() and _marked(dl),
    }
    detail = (got.stdout + got.stderr).strip()[:300]
    warnings.warn(f"mark of the web on this runner: {json.dumps(report)} {detail}", stacklevel=1)
