"""The minor problems the fresh-user CI journey (tests/ci/fresh_user_journey.ps1) found on a clean Windows PC:

1. `irm ... | iex` left $ErrorActionPreference = "Stop", the installer's variables and functions in the user's own
   PowerShell window, and a failure ran `exit 1`, which closed that window before the error could be read.
2. After `delta update --stop`, Edge processes of Delta's closed dashboard window kept running.
3. uninstall -RemoveData kept the Desktop "Delta files" folder and Python 3.12 without a word.
4. Nothing started Delta after a restart, so Telegram stopped answering: "Start Delta when I sign in to Windows".
5. (docs) SmartScreen, Defender and the firewall are not mentioned anywhere.
6. uninstall.ps1 asked port 7878 to shut down, whatever port Delta was set to.
7. With `delta cli` running, the Desktop icon waited 120 s and then said "taking too long".
8. The installer measured free space on the %LOCALAPPDATA% drive, even when DELTA_INSTALL_DIR was on another one.
9. The no-model warnings (top bar, `delta doctor`) led with a terminal command.

PowerShell parts run in a real PowerShell when one is on this PC (Windows PowerShell or pwsh); the rest is static.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _ps() -> str | None:
    for name in ("powershell.exe", "pwsh"):
        exe = shutil.which(name)
        if exe and (name != "powershell.exe" or sys.platform == "win32"):
            return exe
    return None


def _run_ps(script: str, tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    exe = _ps()
    if exe is None:
        pytest.skip("no PowerShell on this PC")
    runner = tmp_path / "t.ps1"
    runner.write_text("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false\n" + script,
                      encoding="utf-8-sig")
    full = {**os.environ, "TEMP": str(tmp_path), "TMP": str(tmp_path),
            "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", str(tmp_path / "lad")),
            "DELTA_T_INSTALLER": str(SCRIPTS / "install.ps1"), **env}
    for k in ("CI", "GITHUB_ACTIONS"):  # Test-Interactive is false anyway: input is not a console here
        full.pop(k, None)
    return subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(runner)],
                          capture_output=True, text=True, encoding="utf-8", timeout=180, env=full, check=False,
                          stdin=subprocess.DEVNULL)


# A failing download stands in for any failure; winget is stubbed so no real Python install starts.
STUBS = ("function Invoke-WebRequest { throw 'network is down (test)' }\n"
         "function winget { }\n")


# --- 1. the installer in the user's own window ------------------------------------------------------------------
def test_installer_runs_in_its_own_scope_and_never_exits_the_window():
    ps = (SCRIPTS / "install.ps1").read_text(encoding="utf-8")
    assert "$__DeltaInstaller = {\nparam(" in ps.replace("\r\n", "\n")
    assert "& $__DeltaInstaller @args" in ps and "Remove-Variable -Name __DeltaInstaller" in ps
    fail = ps[ps.index("function Fail("):ps.index("function Test-FreeSpace")]
    assert "exit" not in fail.replace("`exit`", "") and "throw" in fail
    # exit only when run as a file (Install Delta.cmd, delta update), never under irm | iex
    exits = [m.start() for m in re.finditer(r"^\s*exit 1\s*$", ps, re.M)]
    assert len(exits) == 1 and "if ($PSCommandPath) {" in ps[ps.rindex("if ($failed)", 0, exits[0]):exits[0]]
    assert "[Net.ServicePointManager]::SecurityProtocol = $tlsSaved" in ps  # put back for the user's window


def test_a_failing_install_under_iex_leaves_the_window_as_it_was(tmp_path):
    got = _run_ps(STUBS +
                  "$ErrorActionPreference = 'Continue'; $ProgressPreference = 'Continue'\n"
                  "Get-Content -Raw -LiteralPath $env:DELTA_T_INSTALLER | Invoke-Expression\n"
                  "'STILL-OPEN'\n"
                  "'EAP=' + $ErrorActionPreference + ' PP=' + $ProgressPreference + ' CODE=' + $LASTEXITCODE\n"
                  "'FUNCS=' + ((Get-Command Fail, Test-Interactive, Get-DeltaRuntime, Step -ErrorAction "
                  "SilentlyContinue | ForEach-Object Name) -join ',')\n"
                  "'VARS=' + ((Get-Variable InstallDir, Repo, Source, Branch, failMark, log, __DeltaInstaller "
                  "-ErrorAction SilentlyContinue | ForEach-Object Name) -join ',')\n",
                  tmp_path, DELTA_INSTALL_DIR=str(tmp_path / "Delta"), DELTA_NO_LAUNCH="1")
    out = got.stdout
    assert got.returncode == 0, got.stderr + out
    assert "Delta could not be installed: network is down (test)" in out, out
    assert "STILL-OPEN" in out and "EAP=Continue PP=Continue CODE=1" in out, out
    assert "FUNCS=\n" in out.replace("\r", "") + "\n" or re.search(r"^FUNCS=$", out, re.M), out
    assert re.search(r"^VARS=$", out, re.M), out
    assert out.count("Delta could not be installed") == 1, out  # said once, not again by the outer catch


def test_a_failing_install_run_as_a_file_still_ends_with_exit_code_1(tmp_path):
    got = _run_ps(STUBS +
                  "& $env:DELTA_T_INSTALLER -InstallDir (Join-Path $env:TEMP 'Delta') -NoLaunch\n"
                  "'CODE=' + $LASTEXITCODE\n", tmp_path)
    assert "Delta could not be installed: network is down (test)" in got.stdout, got.stdout + got.stderr
    assert "CODE=1" in got.stdout, got.stdout


def test_named_parameters_still_reach_the_installer_when_run_as_a_file(tmp_path):
    """`delta update` runs install.ps1 -FromGitHub -InstallDir ... -NoLaunch -WaitForPid ... -StopRunning: the script
    block gets them through @args."""
    got = _run_ps("$t = [IO.File]::ReadAllText($env:DELTA_T_INSTALLER)\n"
                  ". ([scriptblock]::Create($t)) -DefineOnly -InstallDir 'X:\\Odd Folder' -NoLaunch -WaitForPid '1,2'\n"
                  "'DIR=' + $InstallDir; 'NOLAUNCH=' + $NoLaunch; 'WAIT=' + $WaitForPid\n", tmp_path)
    assert "DIR=X:\\Odd Folder" in got.stdout and "NOLAUNCH=True" in got.stdout and "WAIT=1,2" in got.stdout, (
        got.stdout + got.stderr)


# --- 8. free space on the drive Delta goes to -------------------------------------------------------------------
def test_free_space_is_checked_on_the_install_folders_drive():
    ps = (SCRIPTS / "install.ps1").read_text(encoding="utf-8")
    assert "Test-FreeSpace $InstallDir $(if ($fresh) { 2 } else { 0.5 })" in ps
    assert "Test-FreeSpace $env:LOCALAPPDATA $(if" not in ps


def test_free_space_message_names_the_target_drive(tmp_path):
    target = tmp_path / "elsewhere" / "Delta"
    got = _run_ps("$t = [IO.File]::ReadAllText($env:DELTA_T_INSTALLER)\n"
                  ". ([scriptblock]::Create($t)) -DefineOnly\n"
                  "try { Test-FreeSpace $env:DELTA_T_TARGET 999999; 'NO-FAIL' }\n"
                  "catch { 'CAUGHT ' + $_.Exception.Message }\n",
                  tmp_path, DELTA_T_TARGET=str(target))
    root = os.path.splitdrive(str(target))[0] + os.sep if sys.platform == "win32" else "/"
    assert "CAUGHT DELTA-INSTALL-FAILED: there is only" in got.stdout, got.stdout + got.stderr
    assert f"GB free on {root}," in got.stdout, got.stdout


# --- 2. the dashboard window's Edge processes -------------------------------------------------------------------
def test_update_closes_only_deltas_own_dashboard_window(tmp_path):
    data = str(tmp_path / "Data D'Souza")
    prof = os.path.join(data, "app-window")
    lines = {
        f'"msedge.exe" --app=http://127.0.0.1:7878/ --user-data-dir={prof} --window-size=1180,800': True,
        f'"msedge.exe" "--user-data-dir={prof}" --app=file:///x': True,
        f'"msedge.exe" --type=renderer --user-data-dir={prof} --lang=en': True,
        '"msedge.exe" --profile-directory=Default': False,  # the user's own Edge
        f'"msedge.exe" --user-data-dir={prof}2 --app=x': False,  # another folder that starts the same
        f'"msedge.exe" --app=file:///{data}/app-window-splash.html': False,
    }
    objs = ",".join("[pscustomobject]@{ ProcessId = " + str(i) + "; CommandLine = '" + c.replace("'", "''") + "' }"
                    for i, c in enumerate(lines))
    got = _run_ps("$t = [IO.File]::ReadAllText($env:DELTA_T_INSTALLER)\n"
                  ". ([scriptblock]::Create($t)) -DefineOnly\n"
                  f"function Get-CimInstance {{ @({objs}) }}\n"
                  "'IDS=' + ((Get-DeltaWindowProcesses $env:DELTA_T_DATA | ForEach-Object ProcessId) -join ',')\n",
                  tmp_path, DELTA_T_DATA=data)
    want = ",".join(str(i) for i, ok in enumerate(lines.values()) if ok)
    assert f"IDS={want}" in got.stdout, got.stdout + got.stderr
    ps = (SCRIPTS / "install.ps1").read_text(encoding="utf-8")
    stop = ps[ps.index("$rt = Get-DeltaRuntime $InstallDir"):ps.index('Done ("closed the running Delta"')]
    assert "Request-DeltaShutdown $rt" in stop and "Close-DeltaWindows $rt.Data" in stop


# --- 6. the configured port ----------------------------------------------------------------------------------------
def _runtime_code(script: str) -> str:
    ps = (SCRIPTS / script).read_text(encoding="utf-8")
    start = ps.index("$code = (")
    body = ps[start:ps.index("$line = ", start)]
    return "".join(re.findall(r'"([^"]*)"', body))


@pytest.mark.parametrize("script", ["install.ps1", "uninstall.ps1"])
def test_scripts_read_the_port_and_folders_the_way_delta_does(script, tmp_path, monkeypatch):
    code = _runtime_code(script)
    assert "'" in code and '"' not in code  # Windows PowerShell 5.1 passes it to python intact
    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.yaml").write_text("api:\n  port: 7979\nfiles:\n  save_dir: " + str(tmp_path / "Mine") + "\n",
                                        encoding="utf-8")
    env = {**os.environ, "DELTA_DATA_DIR": str(data), "PYTHONPATH": str(ROOT / "src")}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60, check=False)
    line = next(x for x in out.stdout.splitlines() if x.startswith("DELTA-RUNTIME "))
    got = json.loads(line[len("DELTA-RUNTIME "):])
    assert got["port"] == 7979 and Path(got["data"]) == data and Path(got["save"]) == tmp_path / "Mine"
    assert not (tmp_path / "Mine").exists()  # only asked, never created


def test_uninstall_shuts_down_on_the_configured_port():
    ps = (SCRIPTS / "uninstall.ps1").read_text(encoding="utf-8")
    assert "7878/api/shutdown" not in ps
    assert '("http://{0}:{1}/api/shutdown" -f $rt.Host, $rt.Port)' in ps
    assert ps.index("$rt = Get-DeltaRuntime $InstallDir") < ps.index("Remove-Item $InstallDir")


# --- 3. what uninstall keeps, said out loud ----------------------------------------------------------------------
def test_uninstall_says_what_it_keeps_and_how_to_remove_python():
    ps = (SCRIPTS / "uninstall.ps1").read_text(encoding="utf-8")
    tail = ps[ps.index('Write-Host "Delta is uninstalled."'):]
    assert "Kept on this PC:" in tail
    assert "The files Delta made for you: $save" in tail and "They are yours" in tail
    assert 'Join-Path ([Environment]::GetFolderPath("Desktop")) "Delta files"' in tail  # the default save folder
    assert "Python 3.12 ($python)" in tail and "winget uninstall --id Python.Python.3.12" in tail
    assert "$python = $basePython" in tail  # the Python Delta's environment was built from, read from .venv\pyvenv.cfg
    assert ps.index('".venv\\pyvenv.cfg"') < ps.index("Remove-Item $InstallDir")
    assert "Settings > Apps > Installed apps" in tail
    assert "-RemoveData to delete them" in tail


def test_install_docs_cover_what_uninstall_keeps():
    doc = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for text in (doc, readme):
        assert "Delta files" in text and "Python 3.12" in text
    assert "winget uninstall --id Python.Python.3.12" in doc


# --- 5. SmartScreen, Defender, firewall ----------------------------------------------------------------------------
def test_docs_explain_smartscreen_defender_and_the_firewall():
    doc = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Windows protected your PC" in doc and "More info" in doc and "Run anyway" in doc
    assert "Defender" in doc and "127.0.0.1" in doc and "firewall" in doc.lower()
    assert "SmartScreen" in readme and "firewall" in readme.lower()
    for text in (doc, readme):
        assert "\u2014" not in text  # house style: no em dashes


# --- 4. Start Delta when I sign in to Windows -----------------------------------------------------------------------
class FakeRegistry(types.ModuleType):
    """Just enough of winreg for HKCU Run (and for config.known_folder, which then falls back)."""
    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self) -> None:
        super().__init__("winreg")
        self.keys: dict[str, dict[str, object]] = {}

    class _Key:
        def __init__(self, reg: FakeRegistry, path: str) -> None:
            self.reg, self.path = reg, path

        def __enter__(self) -> FakeRegistry._Key:
            return self

        def __exit__(self, *a: object) -> None:
            return None

    def OpenKey(self, root, path, reserved=0, access=0):
        if path not in self.keys:
            raise FileNotFoundError(path)
        return self._Key(self, path)

    def CreateKeyEx(self, root, path, reserved=0, access=0):
        self.keys.setdefault(path, {})
        return self._Key(self, path)

    def SetValueEx(self, key, name, reserved, kind, value):
        self.keys[key.path][name] = value

    def QueryValueEx(self, key, name):
        if name not in self.keys[key.path]:
            raise FileNotFoundError(name)
        return self.keys[key.path][name], 1

    def DeleteValue(self, key, name):
        if name not in self.keys[key.path]:
            raise FileNotFoundError(name)
        del self.keys[key.path][name]


@pytest.fixture
def reg(monkeypatch):
    from delta.system import autostart

    fake = FakeRegistry()
    monkeypatch.setattr(autostart, "_winreg", lambda: fake)
    return fake


def test_autostart_is_one_hkcu_run_value_that_starts_delta_in_the_background(reg, tmp_path):
    from delta.system import autostart

    scripts = tmp_path / "R\u0101j D'Souza" / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    for exe in ("python.exe", "pythonw.exe"):
        (scripts / exe).write_bytes(b"")
    assert not autostart.status().enabled  # off unless the user turns it on
    st = autostart.enable(str(scripts / "python.exe"))
    assert st.enabled and st.supported
    value = reg.keys[autostart.RUN_KEY][autostart.VALUE]
    assert value == f'"{scripts / "pythonw.exe"}" -m delta app --background'  # no console window, no dashboard
    assert autostart.VALUE == "DeltaAgent" and autostart.RUN_KEY.startswith("Software\\Microsoft\\Windows")
    st = autostart.disable()
    assert not st.enabled and autostart.VALUE not in reg.keys[autostart.RUN_KEY]
    assert not autostart.disable().enabled  # turning off twice is fine


def test_autostart_turned_off_in_task_manager_is_reported_and_turned_on_again(reg):
    from delta.system import autostart

    autostart.enable()
    reg.keys[autostart.APPROVED_KEY] = {autostart.VALUE: b"\x03" + b"\x00" * 11}
    assert not autostart.status().enabled and autostart.status().blocked_in_task_manager
    st = autostart.enable()
    assert st.enabled and autostart.VALUE not in reg.keys[autostart.APPROVED_KEY]


def test_autostart_is_not_offered_off_windows(monkeypatch):
    from delta.system import autostart

    monkeypatch.setattr(autostart, "_winreg", lambda: None)
    assert autostart.status().to_dict() == {"supported": False, "enabled": False, "command": "",
                                            "blocked_in_task_manager": False}


def test_uninstall_removes_the_autostart_value():
    from delta.system import autostart

    ps = (SCRIPTS / "uninstall.ps1").read_text(encoding="utf-8")
    assert f'$autostartName = "{autostart.VALUE}"' in ps
    assert '"HKCU:\\' + autostart.RUN_KEY + '"' in ps and '"HKCU:\\' + autostart.APPROVED_KEY + '"' in ps
    assert "Remove-ItemProperty -Path $key -Name $autostartName" in ps


@pytest.fixture
def client(tmp_path, monkeypatch, reg):
    from fastapi.testclient import TestClient

    from delta.api.server import create_app, load_token
    from delta.bootstrap import build
    from delta.config import Settings

    if sys.platform != "win32":
        monkeypatch.setitem(sys.modules, "winreg", reg)  # config.known_folder: no such key, falls back
    monkeypatch.setattr("delta.api.setup_routes.get_secret", lambda n: None)
    monkeypatch.setattr("delta.bootstrap.get_secret", lambda n: None)
    c = build(Settings(data_dir=tmp_path))
    token = load_token(tmp_path)
    with TestClient(create_app(c, token)) as tc:
        tc.headers["Authorization"] = f"Bearer {token}"
        yield tc


def test_settings_switch_turns_autostart_on_and_off(client, reg):
    from delta.system import autostart

    assert client.get("/api/autostart").json()["enabled"] is False
    r = client.put("/api/autostart", json={"enabled": True}).json()
    assert r["ok"] and r["enabled"] and "sign in to Windows" in r["message"]
    assert autostart.VALUE in reg.keys[autostart.RUN_KEY]
    assert client.get("/api/autostart").json()["enabled"] is True
    r = client.put("/api/autostart", json={"enabled": False}).json()
    assert r["ok"] and r["enabled"] is False and autostart.VALUE not in reg.keys[autostart.RUN_KEY]


def test_onboarding_offers_autostart_with_telegram(client, reg):
    from delta.system import autostart

    body = {"owner_name": "A", "autonomy": "ask-risky", "telegram_enabled": True, "telegram_user_ids": ["123"],
            "start_with_windows": True}
    r = client.post("/api/setup/complete", json=body).json()
    assert r["ok"] and r["start_with_windows"] is True and autostart.VALUE in reg.keys[autostart.RUN_KEY]
    r = client.post("/api/setup/complete", json={"owner_name": "A", "autonomy": "ask-risky"}).json()
    assert r["start_with_windows"] is None and autostart.VALUE in reg.keys[autostart.RUN_KEY]  # not asked: unchanged


def test_dashboard_has_the_switch_and_the_onboarding_offer():
    html = (ROOT / "src" / "delta" / "api" / "static" / "index.html").read_text(encoding="utf-8")
    about = html[html.index('id="set_about"'):html.index("</section>", html.index('id="set_about"'))]
    assert "Start Delta when I sign in to Windows" in about and 'id="s_autostart_on"' in about
    tg = html[html.index('data-step="telegram"'):html.index('data-step="name"')]
    assert 'id="tgauto"' in tg and "Start Delta when I sign in to Windows" in tg
    js = (ROOT / "src" / "delta" / "api" / "static" / "setup.js").read_text(encoding="utf-8")
    assert "body.start_with_windows = $(\"tgauto\").checked" in js


def test_readme_no_longer_promises_delta_keeps_listening_after_a_restart():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Start Delta when I sign in to Windows" in readme


# --- 7. `delta cli` running, then the Desktop icon -----------------------------------------------------------------
def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(data_dir=tmp_path, api=SimpleNamespace(host="127.0.0.1", port=7878))


def test_terminal_delta_is_the_running_delta_marked_by_delta_cli(tmp_path, monkeypatch):
    import delta.__main__ as main

    monkeypatch.setattr(main, "running_delta", lambda d: 4242)
    assert main.terminal_delta(tmp_path) is None  # the service (no marker)
    (tmp_path / main.CLI_MARKER).write_text("4242", encoding="ascii")
    assert main.terminal_delta(tmp_path) == 4242
    (tmp_path / main.CLI_MARKER).write_text("999", encoding="ascii")  # left by an older terminal Delta
    assert main.terminal_delta(tmp_path) is None
    monkeypatch.setattr(main, "running_delta", lambda d: None)
    assert main.terminal_delta(tmp_path) is None


def test_icon_says_at_once_that_delta_runs_in_a_terminal(tmp_path, monkeypatch):
    import delta.__main__ as main
    import delta.daemon as daemon

    alerts: list[str] = []
    monkeypatch.setattr(daemon, "load_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(daemon, "_port_open", lambda *a, **k: False)
    monkeypatch.setattr(main, "terminal_delta", lambda d: 4242)
    monkeypatch.setattr(daemon, "alert", alerts.append)
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("started")))
    monkeypatch.setattr(daemon, "_show_splash", lambda *a: (_ for _ in ()).throw(AssertionError("splash")))
    assert daemon.launch_app() == 1
    assert len(alerts) == 1 and "running in a terminal window" in alerts[0] and "4242" in alerts[0]
    assert "/quit" in alerts[0]
    assert "running in a terminal window" in (tmp_path / "logs" / "launcher.log").read_text(encoding="utf-8")


def test_waiting_icon_stops_waiting_when_the_running_delta_is_the_terminal_one(tmp_path, monkeypatch):
    import delta.__main__ as main
    import delta.daemon as daemon

    alerts: list[str] = []
    console = tmp_path / "service.log"
    console.write_text("Delta is already running (pid 4242).\n", encoding="utf-8")
    monkeypatch.setattr(daemon, "_port_open", lambda *a, **k: False)
    monkeypatch.setattr(main, "terminal_delta", lambda d: 4242)
    monkeypatch.setattr(daemon, "alert", alerts.append)
    proc = SimpleNamespace(poll=lambda: 1)
    import time

    t = time.monotonic()
    assert daemon._wait_then_open(_settings(tmp_path), "tok", proc, console) is False  # type: ignore[arg-type]
    assert time.monotonic() - t < 5 and len(alerts) == 1 and "terminal" in alerts[0]


def test_cli_marks_itself_while_it_runs():
    src = (ROOT / "src" / "delta" / "__main__.py").read_text(encoding="utf-8")
    cli = src[src.index("async def _cli()"):src.index("def running_delta")]
    assert "marker.write_text(str(os.getpid())" in cli and "marker.unlink()" in cli
    assert cli.index("await c.start()") < cli.index("marker.write_text")  # only once it holds the lock


def test_background_start_opens_no_window(tmp_path, monkeypatch):
    import delta.__main__ as main
    import delta.daemon as daemon

    started: list[list[str]] = []
    waited: list[bool] = []
    monkeypatch.setattr(daemon, "load_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(daemon, "_port_open", lambda *a, **k: False)
    monkeypatch.setattr(main, "running_delta", lambda d: None)
    monkeypatch.setattr(daemon, "_show_splash", lambda *a: (_ for _ in ()).throw(AssertionError("splash")))
    monkeypatch.setattr(daemon, "open_dashboard", lambda *a: (_ for _ in ()).throw(AssertionError("window")))
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda cmd, **k: started.append(cmd) or SimpleNamespace())
    monkeypatch.setattr(daemon, "_wait_then_open", lambda *a, show=True, **k: waited.append(show) or True)
    assert daemon.launch_app(background=True) == 0
    assert started and started[0][-3:] == ["-m", "delta", "run"] and waited == [False]


def test_background_start_leaves_a_running_delta_alone(tmp_path, monkeypatch):
    import delta.daemon as daemon

    monkeypatch.setattr(daemon, "load_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(daemon, "_delta_on_port", lambda s: True)
    monkeypatch.setattr(daemon, "open_dashboard", lambda *a: (_ for _ in ()).throw(AssertionError("window")))
    assert daemon.launch_app(background=True) == 0


# --- 9. no model: the dashboard first, the terminal second -------------------------------------------------------
def test_no_model_warnings_lead_with_settings_keys():
    from delta.bootstrap import NO_MODEL_WARNING

    w = NO_MODEL_WARNING
    assert w.startswith("No AI model")  # setup_routes clears it by this text once a key is saved
    assert w.index("Settings > Keys") < w.index("Ollama") < w.index("delta secrets set groq_api_key")
    doctor = (ROOT / "src" / "delta" / "setup" / "doctor.py").read_text(encoding="utf-8")
    fix = doctor[doctor.index('"no cloud key or AI app is set up yet"'):]
    fix = fix[:fix.index("return out")]
    assert fix.index("Settings > Keys") < fix.index("Ollama") < fix.index("delta secrets set groq_api_key")
