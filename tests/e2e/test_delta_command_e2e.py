"""The `delta` command on a clean Windows PC (CI job e2e-windows): the real user Path, a new process, uninstall.

scripts/delta_command.ps1 makes bin\\delta.cmd in an install folder whose name has a space, an apostrophe, `&` and
a non-ASCII letter, and adds that bin folder to the account's Path (HKCU\\Environment). A new process that builds
its Path from the registry, as a new PowerShell window does, then runs `delta --version`, passes arguments with
spaces and gets Delta's exit code back, in Windows PowerShell 5.1, PowerShell 7 and cmd.exe. Then
scripts/uninstall.ps1 takes it off the Path again.

The install folder's .venv is a junction to this checkout's .venv (no second package install). The user Path is
changed for real, so this only runs on a CI runner (GITHUB_ACTIONS) and puts the old value back afterwards."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import delta

ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DELTA_E2E") != "1", reason="e2e: set DELTA_E2E=1"),
    pytest.mark.skipif(sys.platform != "win32", reason="Windows only"),
    pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") != "true",
                       reason="changes the real user Path: CI runners only"),
]

# What a new window sees: the PC-wide Path, then the account's own, read from the registry now.
# UTF-8 out, so the folder name with its non-ASCII letter comes back intact (5.1 writes the OEM code page).
NEW_WINDOW_PATH = ("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false; "
                   "$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + "
                   "[Environment]::GetEnvironmentVariable('Path', 'User'); ")


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


def _ps(shell: str, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                           NEW_WINDOW_PATH + command], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=180, check=False)


def _shells() -> list[str]:
    return [s for s in (shutil.which("powershell.exe"), shutil.which("pwsh")) if s]


def test_install_command_new_process_and_uninstall(tmp_path, user_path):
    import _winapi

    venv = ROOT / ".venv"
    assert (venv / "Scripts" / "python.exe").exists(), "run `uv sync` first"
    install = tmp_path / "R\u0101j D'Souza & Co" / "Programs" / "Delta"
    install.mkdir(parents=True)
    (install / "scripts").mkdir()
    for name in ("delta_command.ps1", "uninstall.ps1"):
        shutil.copy(ROOT / "scripts" / name, install / "scripts" / name)
    _winapi.CreateJunction(str(venv), str(install / ".venv"))
    bin_dir = install / "bin"
    shim = bin_dir / "delta.cmd"
    before = _user_path()
    try:
        # Install with Windows PowerShell 5.1, the one every Windows PC has, and once more (nothing twice).
        for _ in range(2):
            out = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                  str(ROOT / "scripts" / "delta_command.ps1"), "-Action", "Install",
                                  "-InstallDir", str(install)], capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=120, check=False)
            assert out.returncode == 0, out.stderr + out.stdout
        after = _user_path()
        assert after is not None
        entries = [e for e in after[0].split(";") if e.strip()]
        assert entries.count(str(bin_dir)) == 1 and entries[-1] == str(bin_dir)
        if before is not None:
            assert after[1] == before[1]  # REG_EXPAND_SZ stays REG_EXPAND_SZ
            assert after[0] == (before[0] + ("" if before[0].endswith(";") or not before[0] else ";") + str(bin_dir))
        data = shim.read_bytes()
        assert data.isascii() and data.count(b"\r\n") == 5 and b"\n" not in data.replace(b"\r\n", b"")

        version = delta.__version__
        for shell in _shells():
            got = _ps(shell, "(Get-Command delta).Source; delta --version; exit $LASTEXITCODE")
            assert got.returncode == 0, f"{shell}: {got.stderr}{got.stdout}"
            lines = got.stdout.lstrip("\ufeff").strip().splitlines()
            assert Path(lines[0]).resolve() == shim.resolve(), f"{shell}: {got.stdout}"
            assert version in got.stdout, f"{shell}: {got.stdout}"
            # an argument with a space arrives whole, and Delta's exit code (argparse: 2) comes back
            bad = _ps(shell, "delta 'no such command'; exit $LASTEXITCODE")
            assert bad.returncode == 2, f"{shell}: {bad.stderr}{bad.stdout}"
            assert "'no such command'" in bad.stderr, f"{shell}: {bad.stderr}"
        # cmd.exe, as a new Command Prompt
        got = _ps("powershell.exe", "cmd.exe /d /c 'delta --version'; exit $LASTEXITCODE")
        assert got.returncode == 0 and version in got.stdout, got.stderr + got.stdout
    finally:
        os.rmdir(install / ".venv")  # the junction only, never this checkout's .venv

    out = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(install / "scripts" / "uninstall.ps1"), "-InstallDir", str(install)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
                         check=False)
    assert out.returncode == 0, out.stderr + out.stdout
    assert "removed the delta command from your Path" in out.stdout
    assert not install.exists()
    gone = _user_path()
    assert gone is None or str(bin_dir).lower() not in gone[0].lower()
    if before is not None:
        assert gone == before
    got = _ps("powershell.exe",
              "$c = Get-Command delta -ErrorAction SilentlyContinue; if ($c) { $c.Source } else { 'none' }")
    assert str(shim).lower() not in got.stdout.lower(), got.stdout
