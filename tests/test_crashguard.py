"""Crash guard: unexpected errors in threads, background tasks, dashboard routes and Telegram handlers are logged
and shown, and Delta keeps running (delta.system.crashguard)."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient

from delta.api.server import create_app, load_token
from delta.bootstrap import build
from delta.channels.telegram import TelegramChannel
from delta.config import Settings
from delta.system.crashguard import CrashGuard, guard_of


def test_report_logs_shows_once_per_window_and_never_raises(tmp_path):
    shown: list[str] = []
    now = [0.0]
    g = CrashGuard(tmp_path, shown.append, clock=lambda: now[0])
    try:
        raise ValueError("boom\nsecond line")
    except ValueError as e:
        msg = g.report("the scheduler", e)
        g.report("the scheduler", e)  # the same kind again at once: logged, not shown again
    assert "the scheduler" in msg and "ValueError: boom" in msg and "Delta kept running" in msg
    assert len(shown) == 1 and g.count == 2
    text = (tmp_path / "logs" / "crashes.log").read_text(encoding="utf-8")
    assert text.count("the scheduler: ValueError: boom") == 2 and "Traceback" in text
    now[0] += 601
    g.report("the scheduler", ValueError("again"))
    assert len(shown) == 2

    class Nasty(Exception):
        def __str__(self) -> str:
            raise RuntimeError("str() itself fails")

    assert "went wrong" in g.report("x", Nasty())  # the guard is never the next crash
    g.warn = lambda m: (_ for _ in ()).throw(RuntimeError("warn fails"))
    g.report("y", KeyError("k"))


def test_a_dying_thread_is_reported(tmp_path):
    shown: list[str] = []
    g = CrashGuard(tmp_path, shown.append)
    before = threading.excepthook
    try:
        g.install_threads()
        t = threading.Thread(target=lambda: 1 / 0, name="delta-apps-index")
        t.start()
        t.join()
    finally:
        threading.excepthook = before
    assert g.count == 1 and "delta-apps-index" in shown[0] and "ZeroDivisionError" in shown[0]


async def test_background_task_errors_are_reported_but_socket_resets_stay_quiet(tmp_path):
    shown: list[str] = []
    g = CrashGuard(tmp_path, shown.append)
    loop = asyncio.get_running_loop()
    before = loop.get_exception_handler()
    try:
        g.install_loop(loop)
        loop.call_exception_handler({"message": "x", "exception": ConnectionResetError(10054, "reset")})
        assert g.count == 0
        task = loop.create_task(asyncio.sleep(0), name="watcher-loop")
        await task
        loop.call_exception_handler({"message": "Task exception was never retrieved", "task": task,
                                     "exception": RuntimeError("watcher died")})
    finally:
        loop.set_exception_handler(before)
    assert g.count == 1 and "watcher-loop" in shown[0] and "watcher died" in shown[0]


def test_a_failing_dashboard_route_answers_json_and_warns(tmp_path, monkeypatch):
    c = build(Settings(data_dir=tmp_path))
    token = load_token(tmp_path)

    def broken():
        raise RuntimeError("sensor exploded")

    monkeypatch.setattr(c.governor, "status", broken)
    with TestClient(create_app(c, token), raise_server_exceptions=False) as tc:
        tc.headers["Authorization"] = f"Bearer {token}"
        r = tc.get("/api/status")
        assert r.status_code == 500
        assert "sensor exploded" in r.json()["detail"] and "/api/status" in r.json()["detail"]
        assert tc.get("/api/boundaries").status_code == 200  # the service is still up
    assert any("sensor exploded" in w for w in c.warnings)
    assert guard_of(c).count == 1


async def test_a_failing_telegram_handler_tells_the_allowed_user(tmp_path):
    c = build(Settings(data_dir=tmp_path))
    replies: list[str] = []

    async def reply_text(text, **kw):
        replies.append(text)

    def update(uid):
        return SimpleNamespace(effective_user=SimpleNamespace(id=uid),
                               effective_message=SimpleNamespace(reply_text=reply_text))

    channel = SimpleNamespace(c=c, allowed=["42"])
    ctx = SimpleNamespace(error=KeyError("chat_id"))
    await TelegramChannel._on_error(channel, update(42), ctx)  # type: ignore[arg-type]
    assert len(replies) == 1 and "still running" in replies[0]
    await TelegramChannel._on_error(channel, update(7), ctx)  # a stranger gets nothing
    await TelegramChannel._on_error(channel, None, ctx)  # an error with no message at all
    assert len(replies) == 1 and guard_of(c).count == 3
    assert any("Telegram" in w for w in c.warnings)
