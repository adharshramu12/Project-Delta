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


def controlled(tmp_path):
    """A channel over a real Container-like object with two providers, for the instant controls."""
    from delta.config import Boundaries
    from delta.safety.policy import PolicyEngine

    providers = {"codex": SimpleNamespace(cfg=SimpleNamespace(models={}), model_options=lambda: [
                     {"id": "gpt-6-luna", "label": "GPT-6 Luna"}], model_for=lambda job: ""),
                 "cline": SimpleNamespace()}
    router = SimpleNamespace(providers=providers, preferred="cline", usable=lambda: ["codex", "cline"])
    b = Boundaries(autonomy="ask-risky")
    audit = SimpleNamespace(record=lambda *a, **k: None)
    c = SimpleNamespace(router=router, audit=audit, running=None,
                        settings=SimpleNamespace(data_dir=tmp_path, llm=SimpleNamespace(preferred="cline"),
                                                 boundaries=b, agent=SimpleNamespace(name="Delta")),
                        orchestrator=SimpleNamespace(executor=SimpleNamespace(policy=PolicyEngine(b))))
    return TelegramChannel(c, "123456:TEST-TOKEN", ["5"]), c  # type: ignore[arg-type]


async def test_switching_ai_model_and_mode_from_the_phone(tmp_path):
    ch, c = controlled(tmp_path)
    said: list[str] = []

    async def say(text):
        said.append(text)

    await ch._apply(say, "ai", "codex", 5)
    await ch._apply(say, "model", "gpt-6-luna", 5)
    await ch._apply(say, "mode", "trusted", 5)
    await ch._apply(say, "ai", "nonsense", 5)
    assert c.router.preferred == "codex" and c.router.providers["codex"].cfg.models["chat"] == "gpt-6-luna"
    assert c.orchestrator.executor.policy.b.autonomy == "trusted"
    assert said[-1].startswith("✗") and "Trusted" in said[2]
    saved = (tmp_path / "settings.yaml").read_text()
    assert "preferred: codex" in saved and "autonomy: trusted" in saved  # survives a restart


async def test_stop_cancels_whatever_is_running(tmp_path):
    import asyncio

    from delta.control import stop_running

    _, c = controlled(tmp_path)
    c.running = asyncio.ensure_future(asyncio.sleep(30))
    assert stop_running(c, "test")
    await asyncio.sleep(0)
    assert c.running.cancelled() and not stop_running(c, "test")


async def test_a_channel_that_times_out_at_start_keeps_retrying(monkeypatch):
    """'Telegram could not start: Timed out' used to leave the phone unanswered until a restart."""
    import asyncio

    from delta import daemon

    starts = {"n": 0}

    class Flaky:
        async def start(self):
            starts["n"] += 1
            if starts["n"] == 1:
                raise TimeoutError("Timed out")

        async def stop(self):
            pass

    hooks = []
    c = SimpleNamespace(settings=SimpleNamespace(channels={"telegram": SimpleNamespace(enabled=True)}),
                        warnings=[], live_channels={},
                        lifecycle=SimpleNamespace(on_shutdown=lambda name, fn: hooks.append(name)))
    c.warn = lambda m: c.warnings.append(m)
    monkeypatch.setattr(daemon, "CHANNELS", {"telegram": (lambda _c: Flaky(), "channels")})
    monkeypatch.setattr(daemon, "RETRY_DELAYS_S", (0,))
    assert await daemon._start_channels(c) == [] and "Retrying" in c.warnings[0]
    for _ in range(50):
        await asyncio.sleep(0.01)
        if "telegram" in c.live_channels:
            break
    assert "telegram" in c.live_channels and c.warnings == [] and starts["n"] == 2
