"""Repeated refused attempts reach the owner once: wrong API tokens, and strangers writing to the Telegram bot."""

import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from delta.safety.watch import QUIET_S, IntrusionWatch


class Phone:
    def __init__(self):
        self.allowed, self.sent = ["42"], []

    async def send_text(self, user, text):
        self.sent.append((user, text))


def make(locked=False):
    seen, warns, toasts, clock = [], [], [], [0.0]
    c = SimpleNamespace(audit=SimpleNamespace(record=lambda e, **k: seen.append((e, k))), warn=warns.append,
                        live_channels={"telegram": Phone()}, guard=SimpleNamespace(locked=locked))
    w = IntrusionWatch(c, alert=lambda t, x: toasts.append((t, x)), now=lambda: clock[0])
    return w, c, seen, warns, toasts, clock


def test_a_few_wrong_tokens_alert_once_and_then_stay_quiet():
    w, _c, seen, warns, toasts, clock = make()
    assert [w.note("api_token") for _ in range(4)] == [""] * 4
    text = w.note("api_token")
    assert "wrong or missing key" in text and "refused all of them" in text
    assert seen == [("intrusion.alert", {"kind": "api_token", "who": ""})] and len(toasts) == len(warns) == 1
    assert [w.note("api_token") for _ in range(20)] == [""] * 20  # no noise while the try goes on
    clock[0] += QUIET_S + 1
    assert [bool(w.note("api_token")) for _ in range(5)] == [False] * 4 + [True]


def test_slow_or_single_refusals_never_alert():
    w, _c, _seen, _warns, toasts, clock = make()
    for _ in range(12):  # one stale tab asking now and then
        clock[0] += 200
        assert w.note("api_token") == ""
    assert w.note("telegram_stranger", "7") == "" and w.note("nonsense") == "" and toasts == []


async def test_the_paired_phone_is_told_about_a_stranger_but_not_while_locked():
    w, c, _seen, _warns, _toasts, _clock = make()
    for _ in range(3):
        w.note("telegram_stranger", "999\n<b>")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    (user, text), = c.live_channels["telegram"].sent
    assert user == "42" and "Telegram id 999<b>" in text and "cannot see or control anything" in text
    w2, c2, *_ = make(locked=True)
    for _ in range(3):
        w2.note("telegram_stranger", "999")
    await asyncio.sleep(0)
    assert c2.live_channels["telegram"].sent == []


def test_the_api_counts_every_refused_request_and_no_accepted_one(tmp_path, monkeypatch):
    from delta.api.server import create_app
    from delta.bootstrap import build
    from delta.config import load_settings

    monkeypatch.setenv("DELTA_DATA_DIR", str(tmp_path))
    c = build(load_settings())
    notes = []
    c.watch = SimpleNamespace(note=lambda kind, who="": notes.append(kind))
    client = TestClient(create_app(c, "right"))
    assert client.get("/api/status", headers={"Authorization": "Bearer right"}).status_code == 200 and notes == []
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert notes == ["api_token", "api_token"]


def test_five_wrong_tokens_against_the_real_api_raise_one_alert_and_reach_the_phone(tmp_path, monkeypatch):
    import time

    from delta.api.server import create_app
    from delta.bootstrap import build
    from delta.config import load_settings

    monkeypatch.setenv("DELTA_DATA_DIR", str(tmp_path))
    c = build(load_settings())
    toasts = []
    c.watch = IntrusionWatch(c, alert=lambda t, x: toasts.append(x))
    c.live_channels["telegram"] = phone = Phone()
    with TestClient(create_app(c, "right")) as client:  # the with-block runs start-up, as the real server does
        for _ in range(9):
            assert client.get("/api/status", headers={"Authorization": "Bearer guess"}).status_code == 401
        for _ in range(40):  # the phone message is handed from the worker thread to the server's loop
            if phone.sent:
                break
            time.sleep(0.05)
    assert c.watch.alerts == 1 and len(toasts) == 1
    assert phone.sent and phone.sent[0][0] == "42" and "wrong or missing key" in phone.sent[0][1]
