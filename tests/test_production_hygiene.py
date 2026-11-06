"""Batch E: production hygiene. Config lookup, the API's local-only rules, child-process environments, the
signed audit log, encryption that refuses instead of falling back, temporary folders, the browser's port,
single-source version, line endings, and the CI/installer guarantees a stranger's laptop relies on."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


# --- P5: config lookup ------------------------------------------------------------------------------------
def test_a_config_folder_in_the_current_directory_is_not_picked_up(tmp_path, monkeypatch):
    from delta import config

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "delta.yaml").write_text("agent: {name: Hijacked}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DELTA_CONFIG_DIR", raising=False)
    assert config._config_dir() == config.REPO_ROOT / "config"


def test_the_explicit_override_still_works(tmp_path, monkeypatch):
    from delta import config

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "delta.yaml").write_text("agent: {name: Mine}\n")
    monkeypatch.setenv("DELTA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DELTA_DATA_DIR", str(tmp_path / "data"))
    assert config._config_dir() == tmp_path / "config"
    assert config.load_settings().agent.name == "Mine"


# --- L7: the API only listens on this PC ------------------------------------------------------------------
@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "localhost", "::1", "[::1]"])
def test_loopback_hosts_are_kept(host):
    from delta.config import ApiConfig

    assert ApiConfig(host=host).host == host.strip()


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "example.com", "", "127.0.0.1.evil.com"])
def test_anything_else_becomes_loopback_and_old_files_still_load(host):
    from delta.config import ApiConfig, Settings

    assert ApiConfig(host=host).host == "127.0.0.1"
    assert Settings.model_validate({"api": {"host": host, "port": 7878}}).api.host == "127.0.0.1"


# --- L4 / L2: the API token -------------------------------------------------------------------------------
def test_token_is_created_once_and_reused(tmp_path):
    from delta.api.server import load_token, same_token

    first = load_token(tmp_path)
    assert len(first) >= 32 and load_token(tmp_path) == first
    assert [p.name for p in tmp_path.iterdir()] == ["api_token"]  # no temporary file left behind
    if os.name != "nt":
        assert (tmp_path / "api_token").stat().st_mode & 0o077 == 0  # owner only
    assert same_token(first, first) and not same_token("wrong", first) and not same_token(None, first)
    assert not same_token("töken-ü", first)  # non-ASCII: simply wrong, never a crash


def test_an_empty_token_file_is_replaced(tmp_path):
    from delta.api.server import load_token

    (tmp_path / "api_token").write_text("")
    assert len(load_token(tmp_path)) >= 32


def test_racing_starts_agree_on_one_token(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from delta.api.server import load_token

    with ThreadPoolExecutor(8) as pool:
        tokens = set(pool.map(lambda _: load_token(tmp_path), range(16)))
    assert len(tokens) == 1


# --- M3 / M4: WebSocket clients -----------------------------------------------------------------------------
class _SlowWS:
    def __init__(self, delay: float) -> None:
        self.delay, self.got = delay, []

    async def send_json(self, message: dict[str, Any]) -> None:
        await asyncio.sleep(self.delay)
        self.got.append(message)


async def test_a_stalled_client_is_dropped_and_does_not_hold_up_the_others(monkeypatch):
    from delta.api import server

    monkeypatch.setattr(server, "SEND_TIMEOUT_S", 0.2)
    hub = server.Hub()
    fast, stalled = _SlowWS(0), _SlowWS(30)
    hub.clients.update({fast, stalled})  # type: ignore[arg-type]
    loop = asyncio.get_running_loop()
    started = loop.time()
    await hub.broadcast({"type": "event"})
    assert loop.time() - started < 5
    assert fast.got == [{"type": "event"}] and fast in hub.clients and stalled not in hub.clients


def test_websocket_leaves_the_hub_however_it_ends(tmp_path):
    """Any end of a socket (here: a client that disconnects during the approval replay) removes it."""
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from delta.api import server

    hubs: list[Any] = []
    real_hub = server.Hub

    class SpyHub(real_hub):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__()
            hubs.append(self)

    pending = [SimpleNamespace(id="a1", tool="t", summary="s", risk="write", reason="r", task_id="x", for_task=False)]
    from unittest.mock import MagicMock

    c = MagicMock()
    c.approvals.pending = lambda: pending
    original = server.Hub
    server.Hub = SpyHub  # type: ignore[misc]
    try:
        app = server.create_app(c, "tok")  # type: ignore[arg-type]
    finally:
        server.Hub = original  # type: ignore[misc]
    with TestClient(app) as client:
        with client.websocket_connect("/api/ws?token=tok") as ws:
            assert ws.receive_json()["type"] == "approval"
            assert len(hubs[0].clients) == 1
        for _ in range(50):
            if not hubs[0].clients:
                break
            import time

            time.sleep(0.02)
        assert not hubs[0].clients


# --- M10: secrets never reach programs Delta starts ----------------------------------------------------------
def test_secret_looking_variables_are_removed():
    from delta.system import childenv

    childenv.configure([])
    env = {"PATH": "x", "PATHEXT": ".EXE", "SystemRoot": "C:\\Windows", "TEMP": "t", "USERPROFILE": "u",
           "OPENAI_API_KEY": "sk-1", "GITHUB_TOKEN": "g", "AWS_SECRET_ACCESS_KEY": "a", "AWS_ACCESS_KEY_ID": "i",
           "DB_PASSWORD": "p", "AZURE_CLIENT_SECRET": "c", "HF_TOKEN": "h", "DELTA_GROQ_API_KEY": "k",
           "DELTA_TELEGRAM_BOT_TOKEN": "b", "DELTA_DATA_DIR": "d", "DELTA_CONFIG_DIR": "c2", "SSH_AUTH_SOCK": "s",
           "HTTPS_PROXY": "p2", "KEYBOARD_LAYOUT": "us", "SESSIONNAME": "Console", "NPM_CONFIG_AUTH": "n"}
    out = childenv.scrubbed_env({"PYTHONIOENCODING": "utf-8"}, base=env)
    assert set(out) == {"PATH", "PATHEXT", "SystemRoot", "TEMP", "USERPROFILE", "DELTA_DATA_DIR",
                        "DELTA_CONFIG_DIR", "SSH_AUTH_SOCK", "HTTPS_PROXY", "KEYBOARD_LAYOUT", "SESSIONNAME",
                        "PYTHONIOENCODING"}
    assert "OPENAI_API_KEY" in childenv.removed(env) and "PATH" not in childenv.removed(env)


def test_an_explicitly_allowed_name_is_passed_through():
    from delta.system import childenv

    try:
        childenv.configure(["npm_token"])
        out = childenv.scrubbed_env(base={"NPM_TOKEN": "x", "GITHUB_TOKEN": "y"})
        assert out == {"NPM_TOKEN": "x"}
    finally:
        childenv.configure([])


async def test_run_python_does_not_see_secrets_and_leaves_no_temp_folders(tmp_path, monkeypatch):
    import tempfile

    from delta.tools.power import PythonArgs, RunPython

    monkeypatch.setenv("DELTA_GROQ_API_KEY", "gsk_secret")
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "tok_secret")
    monkeypatch.setenv("DELTA_TEST_PLAIN", "visible")  # not a secret name, but DELTA_*: dropped too
    monkeypatch.setenv("ORDINARY_SETTING", "visible")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    code = ("import os\nprint(os.environ.get('DELTA_GROQ_API_KEY'), os.environ.get('SOME_SERVICE_TOKEN'), "
            "os.environ.get('ORDINARY_SETTING'))")
    r = await RunPython().run(PythonArgs(code=code))
    assert r.ok, r.output
    assert "None None visible" in r.output and "secret" not in r.output
    assert list(tmp_path.iterdir()) == []  # script and empty scratch folder removed (L10)

    r = await RunPython().run(PythonArgs(code="open('result.txt', 'w').write('kept')\nprint('ok')"))
    assert r.ok
    kept = [p for p in tmp_path.iterdir()]
    assert len(kept) == 1 and (kept[0] / "result.txt").read_text() == "kept"  # a file it made is never lost


def test_run_shell_passes_a_scrubbed_environment(monkeypatch):
    from delta.tools import shell

    seen: dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)

        class P:
            pid = None
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b""

        return P()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(shell.RunShell().run(shell.ShellArgs(command="dir")))
    assert "OPENAI_API_KEY" not in seen["env"] and "PATH" in {k.upper() for k in seen["env"]}


# --- L6: signed audit log -------------------------------------------------------------------------------------
def _key() -> Any:
    from delta.safety.audit import AuditKey

    return AuditKey(b"k" * 32, "2000-01-01T00:00:00+00:00")


def test_a_signed_log_verifies_and_a_rewritten_one_does_not(tmp_path):
    from delta.safety.audit import GENESIS, AuditLog, _digest

    log = AuditLog(tmp_path / "a.jsonl", _key())
    for i in range(3):
        log.record("tool", n=i)
    report = log.verify_report()
    assert report.ok and report.records == 3 and report.signed == 3 and "intact" in report.text()

    # An attacker without the key edits a record and recomputes the whole plain hash chain: caught.
    lines = [json.loads(x) for x in (tmp_path / "a.jsonl").read_text().splitlines()]
    lines[1]["data"]["n"] = 99
    prev = GENESIS
    for rec in lines:
        rec.pop("hash")
        rec["prev"] = prev
        rec["hash"] = prev = _digest({k: v for k, v in rec.items() if k != "mac"})
    (tmp_path / "a.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines))
    bad = log.verify_report()
    assert not bad.ok and bad.line == 2 and "audit key" in bad.reason
    assert AuditLog(tmp_path / "a.jsonl").verify()  # without the key only the plain chain is checked

    # Stripping the signatures to pass it off as an old, unsigned log: caught too.
    for rec in lines:
        rec.pop("mac", None)
    (tmp_path / "a.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines))
    stripped = log.verify_report()
    assert not stripped.ok and "missing" in stripped.reason


def test_an_old_unsigned_log_keeps_verifying_and_gets_signed_from_now_on(tmp_path):
    from delta.safety.audit import AuditKey, AuditLog

    old = AuditLog(tmp_path / "a.jsonl")
    old.record("before")
    key = AuditKey(b"k" * 32, "2999-01-01T00:00:00+00:00")  # signing "starts" after the old record
    new = AuditLog(tmp_path / "a.jsonl", key)
    new.record("after")
    report = new.verify_report()
    assert report.ok and report.records == 2 and report.signed == 1
    lines = (tmp_path / "a.jsonl").read_text().splitlines()
    assert "mac" not in json.loads(lines[0]) and "mac" in json.loads(lines[1])


def test_the_key_is_created_once_and_never_without_a_store():
    from delta.safety.audit import load_or_create_key

    store: dict[str, str] = {}
    k1 = load_or_create_key(store.get, store.__setitem__)
    k2 = load_or_create_key(store.get, store.__setitem__)
    assert k1 is not None and k2 is not None and k1.key == k2.key and len(k1.key) == 32
    assert load_or_create_key(lambda n: None, store.__setitem__, create=False) is None

    def broken(*a: Any) -> Any:
        raise RuntimeError("no keyring backend")

    assert load_or_create_key(broken, broken) is None  # the plain chain, as before


# --- M14: never plain text on Windows ---------------------------------------------------------------------
def test_sealing_refuses_on_windows_without_dpapi(monkeypatch):
    from delta.system import vault

    monkeypatch.setattr(vault.sys, "platform", "win32")
    monkeypatch.setattr(vault, "available", lambda: False)
    with pytest.raises(vault.VaultUnavailableError, match="never stores"):
        vault.seal("my secret note")
    assert vault.seal_or_keep("already on disk") == "already on disk"  # start-up re-seal: left as it was
    assert "pywin32" in vault.problem()


def test_a_dpapi_failure_also_refuses(monkeypatch):
    from delta.system import vault

    class FakeCrypt:
        @staticmethod
        def CryptProtectData(*a: Any) -> bytes:
            raise OSError("DPAPI broke")

    monkeypatch.setattr(vault.sys, "platform", "win32")
    monkeypatch.setattr(vault, "available", lambda: True)
    monkeypatch.setitem(sys.modules, "win32crypt", FakeCrypt)
    with pytest.raises(vault.VaultUnavailableError, match="DPAPI failed"):
        vault.seal("note")


def test_a_chat_store_on_a_broken_windows_starts_but_refuses_new_text(tmp_path, monkeypatch):
    from delta.memory.chats import ChatStore
    from delta.system import vault

    store = ChatStore(tmp_path / "chats.db")
    chat = store.create("old")
    store.close()
    monkeypatch.setattr(vault.sys, "platform", "win32")
    monkeypatch.setattr(vault, "available", lambda: False)
    again = ChatStore(tmp_path / "chats.db")  # starts: old rows are left as they were
    with pytest.raises(vault.VaultUnavailableError):
        again.add(chat.id, "user", "new private text")
    assert again.messages(chat.id) == []
    again.close()


# --- L16: Delta's browser port ------------------------------------------------------------------------------
def test_no_fixed_cdp_port_by_default():
    from delta.tools import browser

    assert browser.PORT == 0 and browser.LEGACY_PORT == 9223
    assert "--remote-debugging-port=9223" in browser.chrome_args(Path("p"))  # old callers keep working
    assert "--remote-debugging-port=5555" in browser.chrome_args(Path("p"), 5555)
    assert 0 < browser.free_port() < 65536


def test_only_a_browser_delta_started_is_reused(tmp_path, monkeypatch):
    from delta.tools import browser

    data = tmp_path / "data"
    procs = [
        SimpleNamespace(info={"cmdline": ["chrome.exe", "--remote-debugging-port=4444",
                                          f"--user-data-dir={tmp_path / 'elsewhere'}"]}),
        SimpleNamespace(info={"cmdline": ["chrome.exe", "--type=renderer",
                                          f"--user-data-dir={data / 'chrome-profile'}"]}),
        SimpleNamespace(info={"cmdline": None}),
        SimpleNamespace(info={"cmdline": ["msedge.exe", "--remote-debugging-port=5555",
                                          f"--user-data-dir={data / 'edge-profile'}"]}),
    ]
    monkeypatch.setattr(browser.psutil, "process_iter", lambda attrs=None: iter(procs))
    monkeypatch.setattr(browser, "_listens_on", lambda proc, port: True)
    assert browser.owned_browser_port(data) == 5555
    assert browser.owned_browser_port(tmp_path / "other") is None
    procs.pop()
    assert browser.owned_browser_port(data) is None  # someone else's browser on a port is never trusted


# --- F: version ---------------------------------------------------------------------------------------------
def test_version_comes_from_pyproject_only():
    import tomllib

    import delta

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert delta.__version__ == project["version"] == "0.2.0"
    init = (ROOT / "src" / "delta" / "__init__.py").read_text(encoding="utf-8")
    assert not re.search(r'__version__\s*=\s*"\d', init)  # no second copy to forget
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert re.search(r'name = "delta-agent"\nversion = "0\.2\.0"', lock)


# --- P3: line endings ---------------------------------------------------------------------------------------
def test_windows_scripts_get_crlf_line_endings():
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    for pattern in ("*.cmd", "*.bat", "*.ps1"):
        assert f"{pattern} text eol=crlf" in attrs


# --- E: CI and supply chain ---------------------------------------------------------------------------------
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def test_every_action_is_pinned_to_a_full_sha_with_its_version():
    assert WORKFLOWS
    for wf in WORKFLOWS:
        for line in wf.read_text(encoding="utf-8").splitlines():
            if "uses:" in line:
                assert re.search(r"uses: [\w./-]+@[0-9a-f]{40} # v\d+(\.\d+)*\s*$", line), f"{wf.name}: {line}"


def test_ci_keeps_its_required_jobs_and_reads_only_by_default():
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    assert {"full", "minimal", "e2e-windows", "evals-scripted"} <= set(ci["jobs"])
    for wf in WORKFLOWS:
        assert yaml.safe_load(wf.read_text(encoding="utf-8"))["permissions"] == {"contents": "read"}, wf.name
    for job in ("full", "minimal", "e2e-windows", "evals-scripted"):
        syncs = [s["run"] for s in ci["jobs"][job]["steps"] if "uv sync" in s.get("run", "")]
        assert syncs and all("--locked" in s for s in syncs), job


def test_dependabot_watches_actions_and_python_weekly():
    cfg = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    kinds = {u["package-ecosystem"]: u["schedule"]["interval"] for u in cfg["updates"]}
    assert kinds == {"github-actions": "weekly", "uv": "weekly"}


# --- B: installer --------------------------------------------------------------------------------------------
def test_installer_finds_python_safely_and_handles_arm_and_running_delta():
    ps = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    assert "py.exe" in ps and "PythonCore\\3.12\\InstallPath" in ps  # P4: launcher and registry
    assert "WindowsApps" in ps  # never the Store Python (redirected AppData)
    assert "Test-Arm64" in ps and "--architecture x64" in ps and "python-3.12.10-amd64.exe" in ps  # P1
    assert "Get-RunningDelta" in ps and "-StopRunning" not in ps.split("param(")[0]  # P2
    assert "$StopRunning" in ps and "$WaitForPid" in ps
    assert ps.isascii()  # Windows PowerShell 5.1 reads BOM-less files as the ANSI code page


def test_delta_update_refuses_while_delta_runs(tmp_path, monkeypatch, capsys):
    import delta.__main__ as cli

    calls: list[Any] = []
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(cli, "running_delta", lambda data_dir: 4242)
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    install = tmp_path / "Programs" / "Delta"  # an installed copy (no .git)
    assert cli._update(stop=False, root=install) == 1 and not calls
    assert "delta update --stop" in capsys.readouterr().out
    assert cli._update(stop=True, root=install) == 0
    cmd = calls[0][0][0]
    assert "-StopRunning" in cmd and "-WaitForPid" in cmd and str(os.getpid()) in cmd[cmd.index("-WaitForPid") + 1]


def test_running_delta_reads_the_pid_file(tmp_path):
    from delta.__main__ import running_delta

    assert running_delta(tmp_path) is None
    (tmp_path / "delta.pid").write_text(str(os.getpid()))
    assert running_delta(tmp_path) is None  # this process itself is not "another Delta"
    (tmp_path / "delta.pid").write_text("not a pid")
    assert running_delta(tmp_path) is None
