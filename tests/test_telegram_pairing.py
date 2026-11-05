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

    wrong = update(42, "AAAAAAAA")
    await ch._on_message(wrong, None)
    assert ch.allowed == [] and "Connect my phone" in wrong.message.replies[0]

    right = update(42, f"  {code.lower()}\n")  # surrounding spaces and case don't matter
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


def test_codes_are_long_random_and_unambiguous():
    from delta.channels.telegram import PAIR_ALPHABET, PAIR_LENGTH

    ch = channel()
    codes = {ch.new_pairing_code() for _ in range(50)}
    assert len(codes) == 50 and PAIR_LENGTH >= 8
    assert all(len(c) == PAIR_LENGTH and set(c) <= set(PAIR_ALPHABET) for c in codes)
    assert not set("0O1IL") & set(PAIR_ALPHABET)


async def test_the_code_hidden_in_a_longer_message_does_not_pair():
    """Used to be a substring check: one long message of characters could hold thousands of guesses."""
    ch = channel()
    code = ch.new_pairing_code()
    for text in (f"here it is {code}", f"{code}{code}", f"x{code}", code[:-1]):
        await ch._on_message(update(42, text), None)
    assert ch.allowed == []


async def test_too_many_wrong_codes_lock_pairing_until_a_new_code():
    from delta.channels.telegram import PAIR_MAX_FAILS

    ch = channel()
    warned: list[str] = []
    ch.c.warn = warned.append
    code = ch.new_pairing_code()
    for i in range(PAIR_MAX_FAILS):
        await ch._on_message(update(100 + i, "WRONG123"), None)
    right = update(42, code)  # the real code no longer works
    await ch._on_message(right, None)
    assert ch.allowed == [] and "Connect my phone" in right.message.replies[0]
    fails = [d for e, d in ch.c.audit.events if e == "telegram.pair_failed"]
    assert len(fails) == PAIR_MAX_FAILS and fails[-1]["locked"] and warned

    fresh = ch.new_pairing_code()  # only the PC can start pairing again
    await ch._on_message(update(42, fresh), None)
    assert ch.allowed == ["42"]


async def test_an_expired_code_does_not_pair(monkeypatch):
    import delta.channels.telegram as tg

    ch = channel()
    code = ch.new_pairing_code()
    later = tg.time.monotonic() + 601
    monkeypatch.setattr(tg.time, "monotonic", lambda: later)
    await ch._on_message(update(42, code), None)
    assert ch.allowed == []


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


async def test_a_question_arrives_with_buttons_and_a_tap_answers_it(tmp_path):
    from delta.core.state import TaskResult
    from delta.memory.chats import ChatStore

    ch, c = controlled(tmp_path)
    c.chats = ChatStore(tmp_path / "chats.db")
    asked: list = []

    async def submit(req):
        asked.append(req)
        if len(asked) == 1:
            return TaskResult(req.id, "needs_user", "Which daddy photo?",
                              options=["daddy.jpg", "daddy birthday 2025.png"])
        return TaskResult(req.id, "done", "Sent daddy birthday 2025.png")

    c.submit = submit
    sent: list = []

    class Msg(FakeMessage):
        chat = SimpleNamespace(send_action=lambda a: _noop())

        async def reply_text(self, text, **kw):
            sent.append((text, kw.get("reply_markup")))

    async def _noop():
        return None

    first = SimpleNamespace(effective_user=SimpleNamespace(id=5), message=Msg("send me the daddy image"))
    await ch._on_message(first, None)
    text, kb = sent[-1]
    assert "Which daddy photo?" in text and [b.text for row in kb.inline_keyboard for b in row] == [
        "daddy.jpg", "daddy birthday 2025.png"]

    edited: list = []

    async def answer():
        return None

    async def edit_text(t):
        edited.append(t)

    tap = SimpleNamespace(callback_query=SimpleNamespace(
        from_user=SimpleNamespace(id=5), data="ans:1", answer=answer, edit_message_text=edit_text,
        message=Msg("Which daddy photo?")), message=None)
    await ch._on_answer(tap, None)
    assert asked[1].text == "daddy birthday 2025.png"
    assert "send me the daddy image" in asked[1].history and "Which daddy photo?" in asked[1].history
    assert sent[-1][0].startswith("Sent daddy birthday 2025.png")


async def test_replacing_a_key_from_the_phone(tmp_path, monkeypatch):
    """Credits ran out: tap Replace, send the new key; it is deleted from the chat, tested, then used."""
    import delta.setup.checks as checks
    import delta.system.secrets as secrets

    ch, c = controlled(tmp_path)
    store = {"groq_api_key": "gsk_OLD_working_key"}
    refreshed = []
    c.router.refresh = lambda: refreshed.append(True)
    monkeypatch.setattr(secrets, "set_secret", lambda n, v: store.__setitem__(n, v))
    monkeypatch.setattr(secrets, "get_secret", lambda n: store.get(n))

    async def validate(name, value):
        return (value.startswith("gsk_GOOD"), "rejected by Groq")

    monkeypatch.setattr(checks, "validate_key", validate)
    said, deleted = [], []

    class Chat:
        async def send_message(self, text):
            said.append(text)

    def msg(text):
        async def delete():
            deleted.append(text)
        return SimpleNamespace(text=text, delete=delete, chat=Chat())

    async def say(text):
        said.append(text)

    await ch._apply(say, "key", "groq_api_key", 5)
    assert await ch._take_key(SimpleNamespace(message=msg("gsk_BAD_typo")), "5")
    assert store["groq_api_key"] == "gsk_OLD_working_key" and "unchanged" in said[-1]  # bad key: old one kept

    await ch._apply(say, "key", "groq_api_key", 5)
    assert await ch._take_key(SimpleNamespace(message=msg("gsk_GOOD_new_key_123")), "5")
    assert store["groq_api_key"] == "gsk_GOOD_new_key_123" and refreshed and said[-1].startswith("✅")
    assert deleted == ["gsk_BAD_typo", "gsk_GOOD_new_key_123"]  # neither stays in the chat
    assert "gsk_GOOD_new_key_123" not in said[-1]  # only a masked form is ever shown
    assert not await ch._take_key(SimpleNamespace(message=msg("hello")), "5")  # normal messages are not keys
