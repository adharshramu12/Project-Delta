"""`delta doctor`: the setup checks (each one played with fake probes) and the command never crashing.

Bug it fixes: with a company proxy set (HTTPS_PROXY / HTTP_PROXY), the proxy answered doctor's request for the
local Ollama server with an HTML page, and `delta doctor` died with a JSONDecodeError traceback instead of
printing its report. Local servers are now asked directly (no proxy) and any non-JSON answer is "not reachable".
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import delta.__main__ as cli
from delta.config import ChannelConfig, Settings, load_settings
from delta.setup import doctor
from delta.setup.doctor import render, run_checks


class Inst(SimpleNamespace):
    def has(self, name: str) -> bool:
        return name in self.names


def probes(tmp_path: Path, **over):
    root = tmp_path / "Programs" / "Delta"  # an installed copy (no .git)
    (root / "bin").mkdir(parents=True, exist_ok=True)
    base = dict(root=root, which=lambda n: str(root / "bin" / "delta.cmd"), python=r"C:\Py\python.exe",
                version=(3, 12), machine="AMD64", port_in_use=lambda h, p: False, running=lambda d: None,
                writable=lambda d: "", browsers=lambda: Inst(names={"edge"}, automation=True), ocr=lambda: True,
                secret=lambda n: None, sac=lambda: 0, signed=lambda exe: True, cli_found=lambda n, c: None,
                free_gb=lambda d: 50.0, captcha=lambda: "ok")
    base.update(over)
    return base


def by_name(checks):
    return {c.name: c for c in checks}


def test_a_new_install_that_is_not_set_up_yet_has_no_failure(tmp_path):
    checks = run_checks(Settings(data_dir=tmp_path / "data"), **probes(tmp_path))
    assert not [c for c in checks if c.status == "fail"], render(checks)
    got = by_name(checks)
    assert got["delta command"].status == "ok" and got["Browser"].status == "ok"
    assert got["AI models"].status == "warn" and "groq" in got["AI models"].fix  # set up later, with a fix
    assert got["Telegram"].status == "info"


def test_port_used_by_another_program_fails_with_how_to_change_it(tmp_path):
    s = Settings(data_dir=tmp_path / "data")
    got = by_name(run_checks(s, **probes(tmp_path, port_in_use=lambda h, p: True)))
    assert got["Dashboard port"].status == "fail" and "settings.yaml" in got["Dashboard port"].fix
    got = by_name(run_checks(s, **probes(tmp_path, port_in_use=lambda h, p: True, running=lambda d: 4242)))
    assert got["Dashboard port"].status == "ok" and "4242" in got["Dashboard port"].detail


def test_delta_command_missing_or_shadowed(tmp_path):
    s = Settings(data_dir=tmp_path / "data")
    got = by_name(run_checks(s, **probes(tmp_path, which=lambda n: None)))
    c = got["delta command"]
    assert c.status == "warn" and "NEW PowerShell window" in c.fix and "delta_command.ps1" in c.fix
    other = str(tmp_path / "Git" / "delta.exe")
    got = by_name(run_checks(s, **probes(tmp_path, which=lambda n: other)))
    assert got["delta command"].status == "warn" and other in got["delta command"].detail


def test_python_store_wrong_version_and_unsigned_under_smart_app_control(tmp_path):
    s = Settings(data_dir=tmp_path / "data")
    store = r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\python.exe"
    assert by_name(run_checks(s, **probes(tmp_path, python=store)))["Python"].status == "warn"
    assert by_name(run_checks(s, **probes(tmp_path, version=(3, 13))))["Python"].status == "fail"
    sac = by_name(run_checks(s, **probes(tmp_path, sac=lambda: 1, signed=lambda exe: False)))["Smart App Control"]
    assert sac.status == "fail" and "install.ps1" in sac.fix
    assert by_name(run_checks(s, **probes(tmp_path, sac=lambda: 1)))["Smart App Control"].status == "ok"


def test_read_only_data_folder_fails(tmp_path):
    got = by_name(run_checks(Settings(data_dir=tmp_path / "data"),
                             **probes(tmp_path, writable=lambda d: "Access is denied")))
    assert got["Data folder"].status == "fail" and "Access is denied" in got["Data folder"].detail


def test_browser_ocr_telegram_and_models(tmp_path):
    s = Settings(data_dir=tmp_path / "data", channels={"telegram": ChannelConfig(enabled=True)})
    got = by_name(run_checks(s, **probes(tmp_path, browsers=lambda: Inst(names=set(), automation=True),
                                         ocr=lambda: False)))
    assert got["Browser"].status == "warn" and "Edge" in got["Browser"].fix
    assert got["Screen text (OCR)"].status == "warn"
    assert got["Telegram"].status == "warn" and "telegram_bot_token" in got["Telegram"].fix
    got = by_name(run_checks(s, **probes(tmp_path, secret=lambda n: "x")))
    assert got["Telegram"].status == "warn" and "Pair" in got["Telegram"].fix  # token but no phone yet
    shipped = load_settings().model_copy(update={"data_dir": tmp_path / "data"})  # the providers Delta ships with
    assert by_name(run_checks(shipped, **probes(tmp_path)))["AI models"].status == "warn"
    assert by_name(run_checks(shipped, **probes(tmp_path, secret=lambda n: "x")))["AI models"].status == "ok"


def test_render_is_plain_ascii_lines_with_fixes(tmp_path):
    text = render(run_checks(Settings(data_dir=tmp_path / "data"), **probes(tmp_path, which=lambda n: None)))
    assert "WARN  delta command" in text and "Fix: " in text
    assert all(line.startswith("  ") for line in text.splitlines())


class NotJson:
    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")  # a proxy's HTML error page


def test_doctor_survives_a_proxy_answering_for_local_servers(tmp_path, monkeypatch, capsys):
    import httpx

    s = load_settings().model_copy(update={"data_dir": tmp_path})
    assert any(p.local for p in s.llm.providers.values()), "the default settings have a local provider"
    seen: list[dict] = []

    def fake_get(url, **kw):
        seen.append(kw)
        return NotJson()

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(cli, "load_settings", lambda: s)
    monkeypatch.setattr(doctor, "run_checks", lambda settings: [])
    assert cli._doctor() in (0, 1)  # a report, never a traceback
    out = capsys.readouterr().out
    assert "not reachable" in out or "not running" in out
    assert seen and all(kw.get("trust_env") is False for kw in seen)  # no proxy for servers on this PC


@pytest.mark.skipif(not __import__("sys").platform.startswith("win"), reason="real probes: Windows CI")
def test_real_checks_run_on_this_pc(tmp_path):
    checks = run_checks(Settings(data_dir=tmp_path / "data"), ocr=lambda: None)
    assert by_name(checks)["Python"].status == "ok"
    assert by_name(checks)["Data folder"].status == "ok"


def test_captcha_reader_is_reported_and_never_fails_the_setup(tmp_path):
    s = Settings(data_dir=tmp_path / "data")
    assert by_name(run_checks(s, **probes(tmp_path)))["Captcha reader"].status == "ok"
    missing = by_name(run_checks(s, **probes(tmp_path, captcha=lambda: "missing")))["Captcha reader"]
    assert missing.status == "info" and "Windows OCR" in missing.detail and "installer" in missing.fix
    broken = by_name(run_checks(s, **probes(tmp_path, captcha=lambda: "it could not run here (OSError)")))
    assert broken["Captcha reader"].status == "warn" and "could not run" in broken["Captcha reader"].detail


def test_the_real_captcha_model_probe_answers():
    from delta.setup.doctor import _captcha_model
    from delta.tools.captcha_model import installed

    got = _captcha_model()
    assert got == ("ok" if installed() else "missing") or got.startswith("it ")
