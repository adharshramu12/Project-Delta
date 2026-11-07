"""Delta's scripts on a PC whose execution policy runs no scripts (Restricted, the Windows default).

Bug it fixes: `irm .../install.ps1 | iex` runs the installer as text, which the execution policy does not cover,
but step 5 then started scripts\\delta_command.ps1 and scripts\\create_shortcut.ps1 as files (`& <path>.ps1`).
On a PC with the default policy both failed with "running scripts is disabled on this system", and the install
was reported as failed although Delta was installed and checked.

The rules, checked here on the scripts as text (any OS) and in PowerShell where this PC has one:
- no script starts another of Delta's .ps1 files with `&` or by dot-sourcing it: they run its text instead,
- every PowerShell started with -File (scripts, `Install Delta.cmd`, Delta's Python code, the fix lines shown to
  the user) carries -ExecutionPolicy Bypass, which applies to that one process and changes no saved policy,
- nothing sets an execution policy,
- the Desktop and Start-menu shortcuts start Python, never a PowerShell script,
- a failed delta command or icon in step 5 is a warning with the exact command that fixes it, not a failed install.
The real run under a Restricted policy is tests/e2e/test_installer_policy_e2e.py on the CI Windows runner."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PS_FILES = sorted(SCRIPTS.glob("*.ps1"))
CMD_FILES = sorted(ROOT.glob("*.cmd"))
BYPASS_FILE = re.compile(r"-ExecutionPolicy\s+Bypass\b[^\r\n]*-File\b", re.I)


def _code(text: str) -> str:
    """The script without its comments (whole-line and trailing; Delta's scripts use no block comments)."""
    assert "<#" not in text
    out = []
    for line in text.splitlines():
        quote = ""
        cut = len(line)
        for i, ch in enumerate(line):
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in "'\"":
                quote = ch
            elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
                cut = i
                break
        out.append(line[:cut].rstrip())
    return "\n".join(out)


def _ps1_variables(code: str) -> set[str]:
    """Variables that are given a path to a .ps1 file somewhere in the script."""
    found = set()
    for m in re.finditer(r"(?:^|[;{])\s*\$(\w+)\s*=(?!=)([^\n]*)", code, re.M):
        if re.search(r"\.ps1\b", m.group(2), re.I) and "ReadAllText" not in m.group(2):  # a path, not its text
            found.add(m.group(1).lower())
    return found


# --- the scripts as text ------------------------------------------------------------------------------------
def test_there_are_scripts_to_check():
    names = {p.name for p in PS_FILES}
    assert {"install.ps1", "create_shortcut.ps1", "delta_command.ps1", "uninstall.ps1",
            "setup_chrome_profile.ps1"} <= names
    assert [p.name for p in CMD_FILES] == ["Install Delta.cmd"]


@pytest.mark.parametrize("script", PS_FILES, ids=lambda p: p.name)
def test_no_child_script_is_started_as_a_file(script):
    code = _code(script.read_text(encoding="utf-8"))
    ps1_vars = _ps1_variables(code)
    for n, line in enumerate(code.splitlines(), 1):
        # `& (Join-Path ... "x.ps1")`, `& "x.ps1"`, `& .\x.ps1`
        assert not re.search(r"&\s*(\(\s*Join-Path[^\n]*|[\"']?[^\s\"'()]*)\.ps1\b", line, re.I), (script.name, n, line)
        # `. (Join-Path ... "x.ps1")`, `. "x.ps1"`, `. .\x.ps1` at the start of a statement
        assert not re.search(r"(^|[;{]\s*)\.\s+[\"'(]?[^\n]*\.ps1\b", line.strip(), re.I), (script.name, n, line)
        # `& $command` or `. $command` where $command holds a .ps1 path
        for m in re.finditer(r"(?:^|[;{(=|]|\s)[&.]\s*\$(\w+)\b", line):
            assert m.group(1).lower() not in ps1_vars, (script.name, n, line)
        # PowerShell started on a script file without Bypass
        if re.search(r"\b(powershell|pwsh)(\.exe)?\b", line, re.I) and re.search(r"-File\b", line, re.I):
            assert BYPASS_FILE.search(line), (script.name, n, line)


def test_child_scripts_run_from_their_text():
    install = _code((SCRIPTS / "install.ps1").read_text(encoding="utf-8"))
    assert "[scriptblock]::Create([System.IO.File]::ReadAllText($Path))" in install
    assert install.count("Invoke-DeltaScript (Join-Path $scripts") == 2  # delta_command.ps1, create_shortcut.ps1
    shortcut = _code((SCRIPTS / "create_shortcut.ps1").read_text(encoding="utf-8"))
    assert 'ReadAllText((Join-Path $root "scripts\\delta_command.ps1"))' in shortcut
    assert "& $block -Action Install -InstallDir $root" in shortcut
    uninstall = _code((SCRIPTS / "uninstall.ps1").read_text(encoding="utf-8"))
    assert "ReadAllText($command)" in uninstall and "& $block -Action Uninstall -InstallDir $InstallDir" in uninstall
    # run as text there is no $PSScriptRoot, so create_shortcut.ps1 is given its folder
    assert "[string]$Root" in shortcut and "if (-not $Root) { $Root = Split-Path $PSScriptRoot -Parent }" in shortcut
    assert "$PSScriptRoot" not in shortcut.split("if (-not $Root) { $Root = Split-Path $PSScriptRoot -Parent }", 1)[1]


@pytest.mark.parametrize("path", PS_FILES + CMD_FILES, ids=lambda p: p.name)
def test_no_execution_policy_is_ever_changed(path):
    code = path.read_text(encoding="utf-8")
    assert not re.search(r"Set-ExecutionPolicy", code, re.I), path.name
    assert "Microsoft.PowerShell\\ExecutionPolicy" not in code


def test_install_delta_cmd_runs_the_installer_with_bypass_for_that_run_only():
    cmd = (ROOT / "Install Delta.cmd").read_text(encoding="utf-8")
    line = next(x for x in cmd.splitlines() if "install.ps1" in x and x.strip().lower().startswith("powershell"))
    assert BYPASS_FILE.search(line) and "-NoProfile" in line


def test_python_starts_scripts_only_with_bypass():
    hits = []
    for py in (ROOT / "src").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if '"-File"' in line:
                assert '"-ExecutionPolicy", "Bypass", "-File"' in line, (py, n, line)
                hits.append(py.name)
            if ".ps1" in line and "/" not in line.split(".ps1")[0][-40:]:
                # a .ps1 named in a message to the user: the command shown must work under Restricted too
                assert "-ExecutionPolicy Bypass -File" in line, (py, n, line)
    assert "__main__.py" in hits  # delta update starts the installer


def test_delta_update_starts_the_installer_with_bypass(tmp_path, monkeypatch):
    import delta.__main__ as cli

    calls: list[Any] = []
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(cli, "running_delta", lambda data_dir: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    install = tmp_path / "Programs" / "Delta"
    assert cli._update(stop=True, root=install) == 0
    cmd = calls[0][0][0]
    assert cmd[:6] == ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                       str(install / "scripts" / "install.ps1")]


def test_shortcuts_start_python_not_a_script():
    ps = _code((SCRIPTS / "create_shortcut.ps1").read_text(encoding="utf-8"))
    # Written with Set-DeltaShortcut (scripts/shell_link.ps1: Unicode paths), one call per kind of shortcut
    targets = re.findall(r"-Target\s+(\$pythonw|\(Join-Path [^)]+\))", ps)
    assert targets == ["$pythonw", '(Join-Path $root ".venv\\Scripts\\python.exe")']  # Delta, Update Delta
    assert '$pythonw = Join-Path $root ".venv\\Scripts\\pythonw.exe"' in ps
    args = re.findall(r"-Arguments\s+(\"[^\"]*\")", ps)
    assert args == ['"-m delta app"', '"-m delta update --stop"']
    assert "CreateShortcut" not in ps  # only shell_link.ps1's fallback uses WScript.Shell
    assert not re.search(r"\.ps1|powershell", "".join(targets + args), re.I)


def test_step5_failures_are_warnings_with_a_fix():
    ps = _code((SCRIPTS / "install.ps1").read_text(encoding="utf-8"))
    step5 = ps.split("function Install-DeltaShortcuts", 1)[1].split("\n}\n", 1)[0]
    assert "Fail " not in step5 and "Fail(" not in step5  # never "Delta could not be installed"
    assert step5.count("try {") == 2 and step5.count("} catch {") == 2  # the command and the icons, each alone
    assert step5.count("$result.Fixes.Add(") == 2
    assert step5.count("powershell -NoProfile -ExecutionPolicy Bypass -File") == 2
    raw = (SCRIPTS / "install.ps1").read_text(encoding="utf-8")
    main = raw.split("# 5. Command and icons", 1)[1].split("# 6. Open", 1)[0]
    assert "Install-DeltaShortcuts $InstallDir" in main and "& (" not in main
    assert "Some optional parts were not set up (Delta itself works)" in ps
    assert 'Remove: powershell -NoProfile -ExecutionPolicy Bypass -File "{0}"' in ps
    assert "Remove: scripts\\uninstall.ps1" not in ps


def test_installed_files_are_unblocked_but_not_the_environment():
    ps = _code((SCRIPTS / "install.ps1").read_text(encoding="utf-8"))
    assert "Unblock-File -ErrorAction SilentlyContinue" in ps
    body = ps.split("try {\n    Write-Host \"Installing Delta", 1)[1]
    copy = body.index('Fail "copying Delta\'s files failed')
    assert copy < body.index("Unblock-DeltaFiles $InstallDir") < body.index('Done "files in place"')


def test_docs_say_how_to_run_scripts_under_the_default_policy():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    install = readme.split("**Steps**", 1)[1].split("### Troubleshooting", 1)[0]
    assert install.index("irm https://raw.githubusercontent.com") < install.index("Install Delta.cmd")
    assert "Smart App Control" in install
    trouble = readme.split("### Troubleshooting", 1)[1].split("\n## ", 1)[0]
    assert "running scripts is disabled" in trouble and "Smart App Control" in trouble
    for line in readme.splitlines():
        if "uninstall.ps1" in line:
            assert "-ExecutionPolicy Bypass -File" in line, line


# --- step 5 in PowerShell: a failing part is a warning --------------------------------------------------------
def _shells() -> list[str]:
    found = []
    for name in ("powershell.exe", "pwsh"):
        exe = shutil.which(name)
        if exe and (name != "powershell.exe" or sys.platform == "win32"):
            found.append(exe)
    return found


@pytest.mark.parametrize("shell", _shells() or [None])
def test_step5_keeps_going_and_prints_the_fixes(shell, tmp_path):
    if shell is None:
        pytest.skip("no PowerShell on this PC")
    # A throwaway install whose two helper scripts fail: nothing real (Path, Desktop) is touched.
    install = tmp_path / "R\u0101j D'Souza & Co" / "Delta"
    (install / "scripts").mkdir(parents=True)
    for name in ("delta_command.ps1", "create_shortcut.ps1"):
        (install / "scripts" / name).write_text(f'throw "{name} failed on purpose"\r\n', encoding="utf-8-sig")
    runner = tmp_path / f"t-{uuid.uuid4().hex[:8]}.ps1"
    runner.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false\n"
        "$text = [System.IO.File]::ReadAllText($env:DELTA_T_INSTALLER)\n"
        ". ([scriptblock]::Create($text)) -DefineOnly -InstallDir $env:DELTA_T_DIR\n"
        "$r = Install-DeltaShortcuts $InstallDir $false $false\n"
        "'FIXES=' + $r.Fixes.Count\n"
        "foreach ($f in $r.Fixes) { 'FIX ' + $f }\n"
        "$s = Install-DeltaShortcuts $InstallDir $false $true\n"
        "'SKIPPED=' + $s.Fixes.Count\n",
        encoding="utf-8-sig")
    env = {**os.environ, "DELTA_T_INSTALLER": str(SCRIPTS / "install.ps1"), "DELTA_T_DIR": str(install),
           "TEMP": os.environ.get("TEMP", str(tmp_path)), "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", str(tmp_path))}
    out = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(runner)],
                         capture_output=True, text=True, encoding="utf-8", timeout=120, env=env, check=False)
    assert out.returncode == 0, out.stderr + out.stdout
    text = out.stdout.lstrip("\ufeff")
    assert "The delta command could not be set up: delta_command.ps1 failed on purpose" in text
    assert "The Desktop and Start-menu icons could not be made: create_shortcut.ps1 failed on purpose" in text
    assert "FIXES=2" in text and "SKIPPED=1" in text  # -NoShortcuts: no icons tried, the command still is
    fixes = [line[4:] for line in text.splitlines() if line.startswith("FIX ")]
    sep = "\\" if sys.platform == "win32" else "/"
    scripts = f"{install}{sep}scripts{sep}"
    assert fixes == [
        f'powershell -NoProfile -ExecutionPolicy Bypass -File "{scripts}delta_command.ps1" -Action Install '
        f'-InstallDir "{install}"',
        f'powershell -NoProfile -ExecutionPolicy Bypass -File "{scripts}create_shortcut.ps1"',
    ]
    assert "could not be installed" not in text
