"""Fresh-laptop audit: what a stranger's Windows PC runs into after a download, a power cut or a full disk.

Damaged data files, a cut-off audit log line, a broken settings file, a stale delta.pid after a reboot, a save
folder on a drive that is gone, user names with an apostrophe, a phone message shown as a Windows toast, local app
servers left running, and the CodeQL findings fixed in the same pass."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest

from delta.system import sqlite_safe

ROOT = Path(__file__).resolve().parents[1]
GARBAGE = b"\x00\xffthis is not a database\x13\x37" * 50


# --- damaged databases ------------------------------------------------------------------------------------------
def _stores() -> list[tuple[str, Any]]:
    from delta.engine.store import TaskStore
    from delta.learning.store import LearningStore
    from delta.memory.chats import ChatStore
    from delta.memory.skills import SkillStore
    from delta.memory.store import MemoryStore
    from delta.scheduler.store import ScheduleStore

    return [("memory.db", MemoryStore), ("chats.db", ChatStore), ("learning.db", LearningStore),
            ("skills.db", SkillStore), ("schedules.db", ScheduleStore), ("tasks.db", TaskStore)]


@pytest.mark.parametrize("name,store", _stores(), ids=lambda v: v if isinstance(v, str) else "")
def test_a_damaged_database_is_kept_aside_and_a_new_one_opens(tmp_path, name, store):
    path = tmp_path / name
    path.write_bytes(GARBAGE)
    (tmp_path / f"{name}-wal").write_bytes(b"old wal")
    sqlite_safe.SET_ASIDE.clear()
    s = store(path)  # before: sqlite3.DatabaseError "file is not a database", and Delta never started
    s.close()
    assert [orig for orig, _ in sqlite_safe.SET_ASIDE] == [path]
    moved = sqlite_safe.SET_ASIDE[0][1]
    assert moved.name.startswith(f"{name}.damaged-") and moved.read_bytes() == GARBAGE  # nothing deleted
    assert Path(f"{moved}-wal").read_bytes() == b"old wal"
    assert sqlite3.connect(path).execute("SELECT count(*) FROM sqlite_master").fetchone()[0] > 0  # a real database
    sqlite_safe.SET_ASIDE.clear()


def test_a_locked_or_busy_database_is_never_moved():
    assert sqlite_safe.is_damage(sqlite3.DatabaseError("file is not a database"))
    assert not sqlite_safe.is_damage(sqlite3.OperationalError("database is locked"))
    assert not sqlite_safe.is_damage(OSError("no"))


def test_a_healthy_database_and_memory_databases_are_left_alone(tmp_path):
    path = tmp_path / "ok.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE t (x)")
    db.commit()
    db.close()
    sqlite_safe.SET_ASIDE.clear()
    sqlite_safe.connect(path).close()
    sqlite_safe.connect(":memory:").close()
    assert sqlite_safe.SET_ASIDE == [] and path.exists()


async def test_delta_starts_with_damaged_files_and_says_so(tmp_path, monkeypatch):
    """The whole start-up (build + start), as the service runs it, with every damaged file at once."""
    from delta.bootstrap import build
    from delta.config import Settings

    data = tmp_path / "data"
    data.mkdir()
    for name in ("memory.db", "chats.db", "learning.db", "skills.db", "schedules.db", "tasks.db"):
        (data / name).write_bytes(GARBAGE)
    (data / "audit.jsonl").write_bytes(b'{"ts": "x", "event": "cut off by a power c')
    monkeypatch.setattr("delta.config.known_folder", lambda name: tmp_path / name)
    sqlite_safe.SET_ASIDE.clear()
    c = build(Settings(data_dir=data))
    await c.start()
    try:
        damaged = [w for w in c.warnings if "could not be read" in w]
        assert len(damaged) == 6 and all("Nothing else was changed" in w for w in damaged)
        assert c.engine is not None  # the task engine works again (before: switched off for good)
    finally:
        await c.stop()
        sqlite_safe.SET_ASIDE.clear()


# --- the audit log after a power cut ---------------------------------------------------------------------------
def test_a_cut_off_audit_line_never_stops_recording_and_stays_visible(tmp_path):
    from delta.safety.audit import AuditLog

    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("one")
    log.record("two")
    with (tmp_path / "audit.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"ts": "2026-11-06", "event": "thr')  # the write a power cut stopped half way
    log.record("after the crash")  # before: JSONDecodeError on every record, so every task and start-up failed
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines[-1].startswith("{") and json.loads(lines[-1])["event"] == "after the crash"
    assert json.loads(lines[-1])["prev"] == json.loads(lines[1])["hash"]  # chained to the last real record
    report = log.verify_report()
    assert not report.ok and report.line == 3 and "not a valid record" in report.reason  # not hidden
    assert [r["event"] for r in log.tail(10)] == ["one", "two", "after the crash"]


def test_bad_bytes_in_the_audit_log_are_reported_not_crashed_on(tmp_path):
    from delta.safety.audit import AuditLog

    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("one")
    with (tmp_path / "audit.jsonl").open("ab") as f:
        f.write(b"\xff\xfe\x00garbage\n")
    log.record("two")
    report = log.verify_report()
    assert not report.ok and report.line == 2


# --- settings files Delta cannot use ---------------------------------------------------------------------------
@pytest.mark.parametrize("content,why", [
    (b"agent: [unclosed", "not valid YAML"),
    (b"\xff\xfe\x00\x00binary", "not a text file"),
    (b"- just\n- a list\n", "does not hold settings"),
    (b"api:\n  port: not-a-number\n", "api.port"),
])
def test_a_broken_settings_file_gets_a_plain_message(tmp_path, monkeypatch, content, why):
    from delta.config import SettingsError, load_settings

    monkeypatch.setenv("DELTA_DATA_DIR", str(tmp_path))
    (tmp_path / "settings.yaml").write_bytes(content)
    with pytest.raises(SettingsError) as e:
        load_settings()
    assert why in str(e.value) and "settings.yaml" in str(e.value) and "data are kept" in str(e.value)


def test_the_command_line_prints_the_settings_problem_without_a_traceback(tmp_path):
    (tmp_path / "settings.yaml").write_text("agent: [unclosed", encoding="utf-8")
    env = {**os.environ, "DELTA_DATA_DIR": str(tmp_path)}
    r = subprocess.run([sys.executable, "-m", "delta", "tasks", "list"], capture_output=True, text=True, env=env,
                       cwd=ROOT, timeout=60)
    assert r.returncode == 2
    assert "not valid YAML" in r.stderr and "Traceback" not in r.stderr + r.stdout


def test_old_settings_files_still_load(tmp_path, monkeypatch):
    from delta.config import load_settings

    monkeypatch.setenv("DELTA_DATA_DIR", str(tmp_path))
    (tmp_path / "settings.yaml").write_text("agent:\n  owner_name: Raj\nconnectors:\n  call_timeout_s: 30\n",
                                            encoding="utf-8")
    s = load_settings()
    assert s.agent.owner_name == "Raj" and s.connectors.call_timeout_s == 30 and s.connectors.idle_close_s == 600


# --- one copy at a time, without blocking a restart ------------------------------------------------------------
def test_a_stale_pid_file_naming_another_program_does_not_block_start_up(tmp_path):
    from delta.system.lifecycle import Lifecycle

    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])  # not Delta
    try:
        (tmp_path / "delta.pid").write_text(str(other.pid))
        Lifecycle(tmp_path).acquire_single_instance()  # before: "already running" until the file was deleted
        assert (tmp_path / "delta.pid").read_text() == str(os.getpid())
    finally:
        other.kill()
        other.wait()


def test_a_running_delta_still_blocks_a_second_copy(tmp_path):
    from delta.system.lifecycle import AlreadyRunningError, Lifecycle

    delta_like = subprocess.Popen([sys.executable, "-c", "import time, sys; time.sleep(30)", "delta", "run"])
    try:
        (tmp_path / "delta.pid").write_text(str(delta_like.pid))
        with pytest.raises(AlreadyRunningError, match="Delta is already running"):
            Lifecycle(tmp_path).acquire_single_instance()
    finally:
        delta_like.kill()
        delta_like.wait()


def test_the_launcher_recognises_the_plain_already_running_line():
    from delta.daemon import ALREADY_RUNNING
    from delta.system.lifecycle import AlreadyRunningError

    assert str(AlreadyRunningError("Delta is already running (pid 5).")).startswith(ALREADY_RUNNING)


# --- start-up on unusual PCs -----------------------------------------------------------------------------------
async def test_a_save_folder_on_a_missing_drive_does_not_stop_start_up(tmp_path, monkeypatch):
    from delta.bootstrap import build
    from delta.config import FilesConfig, Settings

    monkeypatch.setattr("delta.config.known_folder", lambda name: tmp_path / name)
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    c = build(Settings(data_dir=tmp_path / "data", files=FilesConfig(save_dir=str(blocker / "Delta"))))
    await c.start()
    try:
        assert any("save folder" in w and "not available" in w for w in c.warnings)
    finally:
        await c.stop()


def test_desktop_shortcut_quotes_names_with_an_apostrophe(tmp_path, monkeypatch):
    from delta import config

    desktop = tmp_path / "D'Souza" / "Desktop"
    desktop.mkdir(parents=True)
    seen: list[str] = []
    monkeypatch.setattr(config, "known_folder", lambda name: desktop)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.append(cmd[-1]))
    config.ensure_desktop_shortcut(tmp_path / "D'Souza" / "Delta")
    assert "D''Souza" in seen[0] and "'D'Souza" not in seen[0].replace("D''Souza", "")


def test_a_slow_powershell_never_stops_start_up(tmp_path, monkeypatch):
    from delta import config

    (tmp_path / "Desktop").mkdir()
    monkeypatch.setattr(config, "known_folder", lambda name: tmp_path / "Desktop")

    def slow(*a: Any, **k: Any) -> None:
        raise subprocess.TimeoutExpired("powershell.exe", 15)

    monkeypatch.setattr(subprocess, "run", slow)
    assert config.ensure_desktop_shortcut(tmp_path / "Delta") is None


def test_a_phone_message_in_a_toast_is_never_run_as_powershell(monkeypatch):
    from delta.system import notify

    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)))
    monkeypatch.setattr(notify.threading, "Thread",
                        lambda target, **kw: SimpleNamespace(start=target))
    notify._last.clear()
    evil = 'hi $(Start-Process calc) `"; Remove-Item C:\\x'
    notify.notify("Delta: request from Telegram", evil)
    cmd, kw = calls[0]
    assert cmd[-1] == notify.TOAST_SCRIPT and "Start-Process" not in " ".join(cmd)
    assert "$(Start-Process calc)" in kw["env"]["DELTA_TOAST_XML"]  # only ever data, read by LoadXml


# --- startup cost ------------------------------------------------------------------------------------------------
def test_the_command_line_starts_without_the_http_client():
    """`delta` and the Desktop icon (`delta app`) load the command line first: httpx (~0.4 s) loads only for
    `delta doctor` and the service."""
    code = "import sys, delta.__main__; print('httpx' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=True)
    assert out.stdout.strip() == "False"


# --- connected apps: local servers, crashes, huge results, logs ------------------------------------------------
def _stdio_manager(tmp_path: Path, **settings: Any) -> Any:
    from test_connectors import FAST, box, stdio_spec

    from delta.connectors.catalog import Catalog
    from delta.connectors.manager import AppManager
    from delta.connectors.manager import Settings as MSettings

    cfg = MSettings(**{**FAST.__dict__, **settings})
    return AppManager(tmp_path, Catalog({"local": stdio_spec()}), cfg, secrets=box(tmp_path))


async def test_an_idle_local_server_is_stopped_and_starts_again_when_needed(tmp_path):
    from delta.tools.base import Risk

    m = _stdio_manager(tmp_path, idle_close_s=0.3)
    assert (await m.connect("local"))["status"] == "connected"
    assert (await m.call("local", "read_note", {"id": "n1"}, Risk.READ)).ok
    pid = m.sessions["local"].transport.pid
    await asyncio.sleep(1.0)
    assert "local" not in m.sessions  # stopped: its memory is free
    await asyncio.sleep(0.3)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    out = await m.call("local", "read_note", {"id": "n1"}, Risk.READ)  # started again, transparently
    assert out.ok and "milk" in out.text and m.sessions["local"].transport.pid != pid
    await m.close()


async def test_a_local_server_is_never_stopped_in_the_middle_of_a_call(tmp_path):
    from delta.tools.base import Risk

    m = _stdio_manager(tmp_path, idle_close_s=0.2)
    assert (await m.connect("local"))["status"] == "connected"
    out = await m.call("local", "slow_note", {"seconds": 1}, Risk.READ)
    assert out.ok and out.text == "slow note done" and out.evidence["attempts"] == 1
    await m.close()


async def test_a_server_that_crashes_mid_call_fails_cleanly_and_a_write_is_not_resent(tmp_path):
    from delta.tools.base import Risk

    m = _stdio_manager(tmp_path, idle_close_s=0)
    assert (await m.connect("local"))["status"] == "connected"
    assert (await m.call("local", "whoami", {}, Risk.READ)).ok
    pid = m.sessions["local"].transport.pid
    started = time.monotonic()
    call = asyncio.create_task(m.call("local", "slow_note", {"seconds": 3}, Risk.EXTERNAL))
    await asyncio.sleep(0.5)
    psutil.Process(pid).kill()  # the server program dies while it works on the call
    out = await call
    assert not out.ok and "Local Notes is not answering right now" in out.text
    assert time.monotonic() - started < 3  # told at once, not after the time limit
    assert out.evidence["attempts"] == 1  # something that changes data is never sent twice
    out = await m.call("local", "read_note", {"id": "n1"}, Risk.READ)  # a new server program, next call works
    assert out.ok and "milk" in out.text
    await m.close()


async def test_a_read_cut_off_by_a_crash_is_retried_on_a_fresh_server(tmp_path):
    from delta.tools.base import Risk

    m = _stdio_manager(tmp_path, idle_close_s=0)
    assert (await m.connect("local"))["status"] == "connected"
    assert (await m.call("local", "whoami", {}, Risk.READ)).ok
    pid = m.sessions["local"].transport.pid
    call = asyncio.create_task(m.call("local", "slow_note", {"seconds": 1}, Risk.READ))
    await asyncio.sleep(0.4)
    psutil.Process(pid).kill()
    out = await call
    assert out.ok and out.evidence["attempts"] == 2
    await m.close()


async def test_disconnect_then_reconnect_works_and_leaves_no_server_running(tmp_path):
    from delta.tools.base import Risk

    m = _stdio_manager(tmp_path, idle_close_s=0)
    assert (await m.connect("local"))["status"] == "connected"
    assert (await m.call("local", "whoami", {}, Risk.READ)).ok
    pid = m.sessions["local"].transport.pid
    await m.disconnect("local", by="test")
    await asyncio.sleep(0.3)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    assert not (await m.call("local", "whoami", {}, Risk.READ)).ok  # says to connect it first
    assert (await m.connect("local"))["status"] == "connected"
    assert (await m.call("local", "whoami", {}, Risk.READ)).ok
    await m.close()


async def test_a_huge_app_result_reaches_the_model_cut_to_size(tmp_path):
    from test_connectors import connected

    from delta.evals.fakemcp import FakeApp
    from delta.tools.base import Risk, ToolResult

    with FakeApp(auth="none") as app:
        app.logic.notes["big"] = {"title": "Big", "body": "x" * 3_000_000}
        m = await connected(tmp_path, app, auth="none")
        out = await m.call("fake", "read_note", {"id": "big"}, Risk.READ)
        assert out.ok and len(out.text) > 3_000_000
        assert len(ToolResult(out.ok, out.text).for_llm()) < 6100  # what the model is sent
        await m.close()


def test_a_servers_own_log_is_kept_small(tmp_path):
    from delta.connectors.client import rotate_log

    log = tmp_path / "app.log"
    log.write_bytes(b"x" * 2_000_000)
    rotate_log(log)
    assert not log.exists() and (tmp_path / "app.log.1").stat().st_size == 2_000_000
    log.write_bytes(b"small")
    rotate_log(log)
    assert log.read_bytes() == b"small"


# --- bounded memory in the agent loop ---------------------------------------------------------------------------
async def test_tasks_that_end_needing_the_user_are_forgotten_by_the_learner():
    from delta.core.orchestrator import Orchestrator
    from delta.core.state import TaskRequest, TaskResult

    orch = Orchestrator.__new__(Orchestrator)
    orch._after, orch._planned = {}, set()
    for i in range(50):
        req = TaskRequest(f"task {i}")
        orch._planned.add(req.id)
        await orch._learn(req, TaskResult(req.id, "needs_user", "Which one?"))
    assert orch._planned == set()


# --- CodeQL findings ---------------------------------------------------------------------------------------------
async def test_a_telegram_token_must_look_like_one_before_it_is_sent(monkeypatch):
    from delta.setup.checks import TELEGRAM_TOKEN, validate_key

    ok, msg = await validate_key("telegram_bot_token", "123/../../evil?x=")
    assert not ok and "does not look like a bot token" in msg
    assert TELEGRAM_TOKEN.fullmatch("123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
    assert not TELEGRAM_TOKEN.fullmatch("123456789:abc@evil.example/x")


def test_emoji_pattern_matches_exactly_what_it_did_before():
    from delta.addons.writer.rules import EMOJI

    # The old character class, as code point ranges (overlaps included, as it was written before).
    before = [(0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x1F900, 0x1F9FF), (0x2B00, 0x2BFF), (0xFE0F, 0xFE0F),
              (0x200D, 0x200D), (0x20E3, 0x20E3), (0x1F1E6, 0x1F1FF), (0x2190, 0x21FF), (0x2300, 0x23FF),
              (0x3030, 0x3030), (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299), (0x24C2, 0x24C2),
              (0x25A0, 0x25FF)]
    for cp in range(0x2000, 0x20000):
        # One change on purpose: a lone zero-width joiner is part of Indic scripts (Telugu, Hindi), not an emoji.
        # It still goes when it joins two emojis (a family emoji), see test_writer_rules.
        was = any(lo <= cp <= hi for lo, hi in before) and cp != 0x200D
        assert bool(EMOJI.match(chr(cp))) == was, hex(cp)


def test_trailing_schedule_words_are_cut_in_linear_time():
    from delta.scheduler.parse import _clean_prompt

    assert _clean_prompt("remind me to call mom, at") == "remind me to call mom"
    assert _clean_prompt("water the plants and") == "water the plants"
    assert _clean_prompt("check the hand") == "check the hand"
    started = time.monotonic()
    _clean_prompt("x" + " ," * 50_000 + "y")
    _clean_prompt("x" + "\t" * 100_000 + "y")
    assert time.monotonic() - started < 1.0


def test_app_tokens_are_written_only_when_sealed(tmp_path, monkeypatch):
    from delta.connectors.secrets import SecretBox, SecretsUnavailableError
    from delta.system import vault

    # a "sealer" that keeps the text inside its output (patched in, so no fake sealer is wired into SecretBox)
    monkeypatch.setattr(vault, "seal_strict", lambda t: vault.PREFIX + t)
    keeps = SecretBox(tmp_path)
    with pytest.raises(SecretsUnavailableError):
        keeps.save("notion", {"access_token": "abc"})  # the plain text is still in there: refused
    assert not list(tmp_path.iterdir())


def test_documents_are_served_only_by_their_listed_name(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from delta.api import documents_routes

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "Plan.pdf").write_bytes(b"%PDF-1.4 x")
    (tmp_path / "secret.pdf").write_bytes(b"%PDF secret")
    app = FastAPI()
    c = SimpleNamespace(settings=SimpleNamespace(writer=SimpleNamespace(output_dir=str(docs))))
    documents_routes.register(app, c, lambda: None)
    client = TestClient(app)
    assert client.get("/api/documents/Plan.pdf").content == b"%PDF-1.4 x"
    for bad in ("..%2Fsecret.pdf", "%2E%2E%5Csecret.pdf", "plan.pdf"):
        assert client.get(f"/api/documents/{bad}").status_code == 404
