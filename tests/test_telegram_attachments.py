"""Photos and files from Telegram: saved through the attachment store, read on this PC, the caption is the task,
albums become one request, and the usual phone rules hold (paired users only, nothing while locked, size limits)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("telegram")

from delta.attachments.service import AttachmentService
from delta.channels import telegram as tg
from delta.channels.telegram import TelegramChannel
from delta.config import AttachmentsConfig
from delta.core.state import TaskResult
from delta.evals import attachment_fixtures as F
from delta.llm.router import ModelRouter
from delta.memory.chats import ChatStore


class FakeFile:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def download_to_drive(self, custom_path: str) -> None:
        Path(custom_path).write_bytes(self.data)


class FakeMedia:
    def __init__(self, data: bytes, file_name: str = "", width: int = 0, height: int = 0, size: int | None = None):
        self.data, self.file_name, self.width, self.height = data, file_name, width, height
        self.file_size = len(data) if size is None else size
        self.downloads = 0

    async def get_file(self) -> FakeFile:
        self.downloads += 1
        return FakeFile(self.data)


class FakeChat:
    async def send_action(self, action: str) -> None:
        return None

    async def send_message(self, text: str, **kw: Any) -> None:
        return None


class FakeMessage:
    _ids = 100

    def __init__(self, caption: str = "", photo: list[FakeMedia] | None = None, document: FakeMedia | None = None,
                 group: str | None = None) -> None:
        FakeMessage._ids += 1
        self.message_id = FakeMessage._ids
        self.caption, self.photo, self.document, self.media_group_id = caption, photo or [], document, group
        self.chat = FakeChat()
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kw: Any) -> None:
        self.replies.append(text)


def make(tmp_path: Path, locked: bool = False, enabled: bool = True) -> tuple[TelegramChannel, list[Any]]:
    submitted: list[Any] = []
    audit = SimpleNamespace(events=[], record=lambda event, **data: audit.events.append((event, data)))
    svc = AttachmentService(AttachmentsConfig(ocr="off"), tmp_path, ModelRouter({}, {})) if enabled else None
    guard = SimpleNamespace(locked=locked, locked_message=lambda: "Remote control is locked.")

    async def submit(req: Any) -> TaskResult:
        submitted.append(req)
        return TaskResult(req.id, "done", "Here is the summary.")

    c = SimpleNamespace(audit=audit, settings=SimpleNamespace(agent=SimpleNamespace(name="Delta")),
                        orchestrator=SimpleNamespace(attachments=svc), chats=ChatStore(tmp_path / "chats.db"),
                        guard=guard, submit=submit)
    ch = TelegramChannel(c, "123456:TEST-TOKEN", ["42"])  # type: ignore[arg-type]
    return ch, submitted


def update(uid: int, message: FakeMessage) -> SimpleNamespace:
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid), message=message, callback_query=None)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    import delta.system.notify as notify

    monkeypatch.setattr(notify, "notify", lambda *a, **k: None)
    monkeypatch.setattr(tg, "ALBUM_WAIT_S", 0.05)


async def test_a_photo_with_a_caption_is_the_task(tmp_path):
    ch, submitted = make(tmp_path)
    sizes = [FakeMedia(F.png(90, 60), width=90, height=60), FakeMedia(F.png(1280, 960), width=1280, height=960),
             FakeMedia(b"x" * 10, width=4000, height=3000)]
    msg = FakeMessage("what is in this picture?", photo=sizes)
    await ch._on_file(update(42, msg), None)
    assert len(submitted) == 1
    req = submitted[0]
    assert req.text == "what is in this picture?" and req.channel == "telegram" and len(req.attachments) == 1
    assert sizes[1].downloads == 1 and sizes[2].downloads == 0  # the largest size up to 2560 px, not the original
    att = ch.c.orchestrator.attachments.store.get(req.attachments[0])
    assert att.kind == "image" and att.source == "telegram" and att.folder == req.chat_id
    first = ch.c.chats.messages(req.chat_id)[0]
    assert first["meta"]["attachments"][0]["id"] == att.id
    assert msg.replies == ["Here is the summary."]
    assert ("attachment.added", {"id": att.id, "kind": "image", "size": att.size, "via": "telegram", "user": "42"}) \
        in ch.c.audit.events


@pytest.mark.parametrize(("name", "data"), [("report.pdf", F.text_pdf([["One"], ["Two"]])),
                                            ("notes.docx", F.simple_docx()), ("deck.pptx", F.simple_pptx())])
async def test_documents_without_a_caption_get_the_default_question(tmp_path, name, data):
    from delta.attachments.kinds import DEFAULT_REQUEST

    ch, submitted = make(tmp_path)
    await ch._on_file(update(42, FakeMessage(document=FakeMedia(data, name))), None)
    assert submitted[0].text == DEFAULT_REQUEST and len(submitted[0].attachments) == 1


async def test_strangers_locked_remote_and_switched_off(tmp_path):
    ch, submitted = make(tmp_path)
    media = FakeMedia(F.png(), "a.png")
    await ch._on_file(update(99, FakeMessage(document=media)), None)  # not paired: ignored, nothing downloaded
    assert submitted == [] and media.downloads == 0
    locked, sub2 = make(tmp_path / "l", locked=True)
    msg = FakeMessage(document=FakeMedia(F.png(), "a.png"))
    await locked._on_file(update(42, msg), None)
    assert sub2 == [] and msg.replies == ["Remote control is locked."]
    off, sub3 = make(tmp_path / "o", enabled=False)
    msg = FakeMessage(document=FakeMedia(F.png(), "a.png"))
    await off._on_file(update(42, msg), None)
    assert sub3 == [] and "switched off" in msg.replies[0]


async def test_too_big_or_unsupported_is_refused_before_downloading(tmp_path):
    ch, submitted = make(tmp_path)
    huge = FakeMedia(b"x", "film.pdf", size=25 * 1024 * 1024)
    msg = FakeMessage(document=huge)
    await ch._on_file(update(42, msg), None)
    assert huge.downloads == 0 and "20 MB" in msg.replies[0] and submitted == []
    sheet = FakeMedia(b"PK..", "budget.xlsx")
    msg = FakeMessage(document=sheet)
    await ch._on_file(update(42, msg), None)
    assert sheet.downloads == 0 and "csv" in msg.replies[0]
    renamed = FakeMedia(b"MZ\x90\x00 program", "invoice.pdf")
    msg = FakeMessage(document=renamed)
    await ch._on_file(update(42, msg), None)
    assert "never opened or run" in msg.replies[0] and submitted == []


async def test_an_album_is_one_request_with_the_caption(tmp_path):
    ch, submitted = make(tmp_path)
    first = FakeMessage("compare these", photo=[FakeMedia(F.png(300, 200), width=300, height=200)], group="g1")
    second = FakeMessage("", photo=[FakeMedia(F.png(200, 300), width=200, height=300)], group="g1")
    await ch._on_file(update(42, first), None)
    await ch._on_file(update(42, second), None)
    await asyncio.sleep(0.2)
    assert len(submitted) == 1 and submitted[0].text == "compare these" and len(submitted[0].attachments) == 2


async def test_a_pending_pin_request_is_not_answered_by_a_file(tmp_path):
    ch, submitted = make(tmp_path)

    async def never() -> None:
        raise AssertionError("must not run")

    ch._pending_pin["42"] = (never, "camera", 1e18)
    await ch._on_file(update(42, FakeMessage(document=FakeMedia(F.png(), "p.png"))), None)
    assert "42" not in ch._pending_pin and len(submitted) == 1
