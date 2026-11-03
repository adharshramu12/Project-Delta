"""Telegram pairing: a phone joins by sending the one-time code shown on the dashboard."""

from types import SimpleNamespace

import pytest

pytest.importorskip("telegram")

from delta.channels.telegram import TelegramChannel


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


def update(uid: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid), message=FakeMessage(text))


def channel() -> TelegramChannel:
    audit = SimpleNamespace(events=[], record=lambda event, **data: audit.events.append((event, data)))
    c = SimpleNamespace(audit=audit, settings=SimpleNamespace(agent=SimpleNamespace(name="Delta")))
    return TelegramChannel(c, "123456:TEST-TOKEN", [])  # type: ignore[arg-type]


async def test_the_right_code_pairs_the_phone_once():
    ch = channel()
    saved: list[list[str]] = []
    ch.on_pair = lambda ids: saved.append(list(ids))
    code = ch.new_pairing_code()

    wrong = update(42, "000000")
    await ch._on_message(wrong, None)
    assert ch.allowed == [] and "Connect my phone" in wrong.message.replies[0]

    right = update(42, f"here it is {code}")
    await ch._on_message(right, None)
    assert ch.allowed == ["42"] and saved == [["42"]] and "Paired" in right.message.replies[0]

    stranger = update(99, code)  # the code is single use
    await ch._on_message(stranger, None)
    assert ch.allowed == ["42"]


async def test_start_link_carries_the_code():
    ch = channel()
    code = ch.new_pairing_code()
    up = update(7, "/start")
    await ch._start_cmd(up, SimpleNamespace(args=[code]))
    assert ch.allowed == ["7"]
