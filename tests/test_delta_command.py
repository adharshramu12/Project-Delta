"""The `delta` terminal command (scripts/delta_command.ps1): bin\\delta.cmd and the account's Path.

Bug it fixes: the installer put Delta in %LOCALAPPDATA%\\Programs\\Delta\\.venv and Python is installed with
PrependPath=0, so in a normal PowerShell `delta update` said "delta is not recognized".

Three layers, no Pester:
- the scripts read as text (any OS),
- the Path functions run in PowerShell (Windows PowerShell 5.1 and PowerShell 7, whichever this PC has),
- install and uninstall against a throwaway registry key under HKEY_CURRENT_USER (Windows only).
The real user Path, the shim running `delta --version` from a new process and uninstall are covered by
tests/e2e/test_delta_command_e2e.py on the CI Windows runner."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
COMMAND = SCRIPTS / "delta_command.ps1"


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def _shells() -> list[str]:
    found = []
    for name in ("powershell.exe", "pwsh"):
        exe = shutil.which(name)
        if exe and (name != "powershell.exe" or sys.platform == "win32"):
            found.append(exe)
    return found


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _run_ps(shell: str, body: str, tmp_path: Path, env: dict[str, str] | None = None) -> str:
    script = tmp_path / f"t-{uuid.uuid4().hex[:8]}.ps1"
    # UTF-8 with a BOM: Windows PowerShell 5.1 reads a file without one in the ANSI code page.
    script.write_text(f"$ErrorActionPreference = 'Stop'\n. {_ps_quote(str(COMMAND))}\n{body}\n", encoding="utf-8-sig")
    out = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                         capture_output=True, text=True, encoding="utf-8", timeout=120,
                         env={**os.environ, **(env or {})}, check=False)
    assert out.returncode == 0, out.stderr + out.stdout
    return out.stdout.lstrip("\ufeff")


# --- the scripts as text ------------------------------------------------------------------------------------
def test_every_installer_script_is_ascii():
    for script in SCRIPTS.glob("*.ps1"):
        assert script.read_text(encoding="utf-8").isascii(), script.name  # Windows PowerShell 5.1 reads ANSI


def test_shim_quotes_its_own_folder_forwards_args_and_returns_the_exit_code():
    ps = _read("delta_command.ps1")
    assert "'\"%~dp0..\\.venv\\Scripts\\python.exe\" -m delta %*'" in ps  # quoted, read at run time, every arg
    assert '"exit /b %ERRORLEVEL%"' in ps and '"setlocal DisableDelayedExpansion"' in ps  # ! in a folder name
    assert '-join "`r`n") + "`r`n"' in ps  # CRLF for cmd.exe
    assert "System.Text.ASCIIEncoding" in ps  # no BOM, no code page question
    assert "-cne $text" in ps  # rewritten only when it changes (a running `delta update` may be reading it)


def test_only_the_users_own_path_is_ever_written():
    for name in ("delta_command.ps1", "install.ps1", "uninstall.ps1", "create_shortcut.ps1"):
        ps = _read(name)
        assert "LocalMachine" not in ps and "HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Session Manager" not in ps
        assert not re.search(r"SetEnvironmentVariable\([^)]*Machine", ps), name
        assert "setx" not in ps.lower().replace("setx https_proxy", ""), name  # setx cuts Path at 1024 characters
    ps = _read("delta_command.ps1")
    assert "Registry]::CurrentUser" in ps and "DoNotExpandEnvironmentNames" in ps  # %USERPROFILE% kept as is
    assert "GetValueKind" in ps  # REG_EXPAND_SZ written back as REG_EXPAND_SZ
    assert "0x1A" in ps and '"Environment"' in ps  # WM_SETTINGCHANGE so new windows see it
    assert "$env:Path = Add-PathListEntry $env:Path $bin" in ps  # this window too


def test_installer_adds_the_command_respects_trial_installs_and_says_open_a_new_window():
    ps = _read("install.ps1")
    params = ps.split("param(", 1)[1].split("\n)", 1)[0]
    assert "[switch]$NoPath" in params and "[switch]$NoShortcuts" in params
    assert 'Invoke-DeltaScript (Join-Path $scripts "delta_command.ps1") @{' in ps and 'Action = "Install"' in ps
    assert "NoPath = ($noPath -or $noShortcuts)" in ps
    assert "Install-DeltaShortcuts $InstallDir ([bool]$NoPath) ([bool]$NoShortcuts)" in ps
    assert '"Open a new PowerShell window to use the delta command."' in ps
    assert 'Invoke-DeltaScript (Join-Path $scripts "create_shortcut.ps1") @{ NoCommand = $true; Root = $dir }' in ps
    robocopy = next(line for line in ps.splitlines() if line.strip().startswith("robocopy"))
    assert re.search(r"/XD .* bin ", robocopy)  # an update must not delete the delta command it was started from


def test_no_folder_called_bin_ships_with_delta():
    # robocopy /XD bin skips every folder of that name; Delta itself must not have one.
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False).stdout
    if not tracked:
        pytest.skip("not a git checkout")
    assert not [f for f in tracked.splitlines() if "bin" in Path(f).parts[:-1]]


def test_update_delta_is_in_the_start_menu_only():
    ps = _read("create_shortcut.ps1")
    assert '"Update Delta.lnk"' in ps and '"-m delta update --stop"' in ps
    assert 'Join-Path ([Environment]::GetFolderPath("Programs")) "Delta"' in ps
    assert ps.count('GetFolderPath("Desktop")') == 2  # the Desktop\Delta folder check and the one Delta icon
    assert '"Update Delta.lnk"' not in ps.split("$targets = @(", 1)[1].split(")\n", 1)[0]
    assert "-not $NoCommand" in ps and '-Action Install -InstallDir $root' in ps  # updates from older installers


def test_uninstall_takes_the_command_off_the_path_and_removes_update_delta():
    ps = _read("uninstall.ps1")
    assert "-Action Uninstall -InstallDir $InstallDir" in ps
    assert '"Delta\\Update Delta.lnk"' in ps
    assert ps.index("-Action Uninstall") < ps.index("Remove-Item $InstallDir -Recurse")


def test_docs_give_the_fallbacks_for_when_delta_is_not_recognized():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "irm https://raw.githubusercontent.com/adharshramu12/Project-Delta/main/scripts/install.ps1 | iex" in readme
    assert '& "$env:LOCALAPPDATA\\Programs\\Delta\\.venv\\Scripts\\python.exe" -m delta update --stop' in readme
    assert "new PowerShell window" in readme
    assert "in a terminal from `%LOCALAPPDATA%\\Programs\\Delta\\.venv\\Scripts`" not in readme


def test_update_command_is_unchanged_for_the_shim():
    # `delta` through bin\delta.cmd is the same `python -m delta update`: the installer still runs in its own
    # window and waits for this command's own Delta processes (cmd.exe is not one of them and locks nothing).
    src = (ROOT / "src" / "delta" / "__main__.py").read_text(encoding="utf-8")
    assert "CREATE_NEW_CONSOLE" in src and '"-WaitForPid"' in src


# --- the Path functions in PowerShell -----------------------------------------------------------------------
BIN = "C:\\Users\\R\u0101j D'Souza & Co\\AppData\\Local\\Programs\\Delta\\bin"


@pytest.mark.parametrize("shell", _shells() or [None])
def test_path_list_functions(shell, tmp_path):
    if shell is None:
        pytest.skip("no PowerShell on this PC")
    cases = {
        "empty": ["add", "", BIN],
        "append": ["add", "%DELTA_T_HOME%\\x;C:\\y", BIN],
        "trailing_semicolon": ["add", "C:\\y;", BIN],
        "dup_case_slash": ["add", "C:\\a;" + BIN.upper() + "\\", BIN],
        "dup_quoted": ["add", '"' + BIN + '";C:\\a', BIN],
        "dup_variable": ["add", "%DELTA_T_HOME%\\bin", "C:\\Users\\T\\bin"],
        "remove_all_spellings": ["remove", "C:\\a;;" + BIN + ";%DELTA_T_HOME%\\b;" + BIN + "\\", BIN],
        "remove_only": ["remove", BIN, BIN],
        "remove_absent": ["remove", "C:\\a", BIN],
        "round_trip": ["remove", "C:\\y;" + BIN + ";", BIN],
    }
    body = (
        "$cases = $env:DELTA_T_CASES | ConvertFrom-Json\n"
        "$out = @{}\n"
        "foreach ($p in $cases.PSObject.Properties) {\n"
        "  $op, $value, $dir = $p.Value\n"
        "  $r = if ($op -eq 'add') { Add-PathListEntry $value $dir } else { Remove-PathListEntry $value $dir }\n"
        "  $out[$p.Name] = if ($null -eq $r) { '<null>' } else { [string]$r }\n"
        "}\n"
        "$out['shim'] = Get-DeltaShimText\n"
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false\n"
        "[Console]::Out.Write(($out | ConvertTo-Json -Compress))"
    )
    got = json.loads(_run_ps(shell, body, tmp_path, {"DELTA_T_CASES": json.dumps(cases),
                                                     "DELTA_T_HOME": "C:\\Users\\T"}))
    assert got["empty"] == BIN
    assert got["append"] == "%DELTA_T_HOME%\\x;C:\\y;" + BIN  # other entries untouched, %VAR% not expanded
    assert got["trailing_semicolon"] == "C:\\y;" + BIN + ";"  # and removing it gives back "C:\\y;"
    assert got["dup_case_slash"] == got["dup_quoted"] == got["dup_variable"] == "<null>"  # never twice
    assert got["remove_all_spellings"] == "C:\\a;;%DELTA_T_HOME%\\b"
    assert got["remove_only"] == ""
    assert got["remove_absent"] == "<null>"
    assert got["round_trip"] == "C:\\y;"
    shim = got["shim"]
    assert shim.isascii() and shim.count("\r\n") == 5 and "\n" not in shim.replace("\r\n", "")
    assert '"%~dp0..\\.venv\\Scripts\\python.exe" -m delta %*\r\n' in shim and shim.endswith("exit /b %ERRORLEVEL%\r\n")


# --- install and uninstall against a throwaway registry key (Windows) --------------------------------------
@pytest.fixture
def env_key():
    if sys.platform != "win32":
        pytest.skip("the registry is Windows only")
    import winreg

    sub = f"Software\\DeltaTests-{uuid.uuid4().hex[:8]}"
    key = f"{sub}\\Environment"
    try:
        yield key
    finally:
        for k in (key, sub):
            with contextlib.suppress(OSError):
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, k)


def _reg_path(key: str) -> tuple[str, int] | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            value, kind = winreg.QueryValueEx(k, "Path")
            return str(value), int(kind)
    except FileNotFoundError:
        return None


def _set_reg_path(key: str, value: str, kind: int) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
        winreg.SetValueEx(k, "Path", 0, kind, value)


@pytest.mark.parametrize("shell", _shells() or [None])
def test_install_and_uninstall_keep_the_rest_of_the_path(shell, env_key, tmp_path):
    import winreg

    if shell is None:
        pytest.skip("no PowerShell on this PC")
    install = tmp_path / "R\u0101j D'Souza & Co" / "Programs" / "Delta"
    install.mkdir(parents=True)
    bin_dir = str(install / "bin")
    original = "%USERPROFILE%\\tools;C:\\Other"
    _set_reg_path(env_key, original, winreg.REG_EXPAND_SZ)

    def run(*args: str) -> None:
        out = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                              str(COMMAND), *args, "-InstallDir", str(install), "-EnvKey", env_key],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                             check=False)
        assert out.returncode == 0, out.stderr + out.stdout

    run("-Action", "Install", "-NoPath")
    assert (install / "bin" / "delta.cmd").read_bytes().count(b"\r\n") == 5
    assert _reg_path(env_key) == (original, winreg.REG_EXPAND_SZ)  # -NoPath: Path left alone

    run("-Action", "Install")
    run("-Action", "Install")  # again: nothing added twice
    assert _reg_path(env_key) == (f"{original};{bin_dir}", winreg.REG_EXPAND_SZ)  # %USERPROFILE% kept unexpanded

    run("-Action", "Uninstall")
    assert _reg_path(env_key) == (original, winreg.REG_EXPAND_SZ)
    assert not (install / "bin").exists()

    _set_reg_path(env_key, "C:\\Other;", winreg.REG_EXPAND_SZ)  # ends in ";": given back exactly
    run("-Action", "Install")
    assert _reg_path(env_key) == (f"C:\\Other;{bin_dir};", winreg.REG_EXPAND_SZ)
    run("-Action", "Uninstall")
    assert _reg_path(env_key) == ("C:\\Other;", winreg.REG_EXPAND_SZ)

    _set_reg_path(env_key, "C:\\Other", winreg.REG_SZ)  # a plain REG_SZ Path stays REG_SZ
    run("-Action", "Install")
    assert _reg_path(env_key) == (f"C:\\Other;{bin_dir}", winreg.REG_SZ)
    run("-Action", "Uninstall")
    assert _reg_path(env_key) == ("C:\\Other", winreg.REG_SZ)
