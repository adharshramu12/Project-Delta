"""Pictures the user sends (dashboard chat, Telegram photo) really reach a model that can see them.

Reported on a real PC: "Delta is unable to recognise images". Covered here:
- the picture bytes reach the vision call as a JPEG data URL, made small (longest side, byte cap) and upright;
- transparent PNG / WebP / palette pictures are flattened on white (before: transparent pixels turned black, so a
  screenshot with a transparent background reached the model as a black square);
- WebP and animated GIF work; iPhone HEIC works when pillow-heif is installed and otherwise gets clear words;
- a picture is looked at unless the request is plainly about its text and the OCR text is enough;
- a model that answers "I can't see images" is not taken as having seen it: the next model that can see is asked;
- when the vision call fails the user hears why (not "connect an AI" when one is connected), and when no model can
  see pictures the note names models that can;
- the AI answering the user is told that the picture WAS seen, so it never claims it cannot see images.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeProvider, text

from delta.attachments import images as I
from delta.attachments.kinds import IMAGE, AttachmentError, detect
from delta.attachments.service import AttachmentService
from delta.config import AttachmentsConfig, ProviderConfig
from delta.core.state import TaskRequest
from delta.llm.router import ModelRouter
from delta.llm.schemas import LLMError, LLMResponse


class Eyes(FakeProvider):
    """A provider that can see (or not), records each picture it was sent and answers with `say`."""

    def __init__(self, name: str, say: str | Exception = "A red bicycle leaning on a blue wall.",
                 sees: bool = True) -> None:
        super().__init__(name, handler=self._answer)
        self.say = say
        self.sees = sees
        self.cfg = ProviderConfig()
        self.pictures: list[dict[str, Any]] = []

    def supports(self, task: str) -> bool:
        return self.sees or task != "vision"

    def _answer(self, task: str, messages: list[Any]) -> LLMResponse:
        for part in messages[-1]["content"]:
            if isinstance(part, dict) and part.get("type") == "image_url":
                self.pictures.append(part["image_url"])
        if isinstance(self.say, Exception):
            raise self.say
        return LLMResponse(content=self.say, provider=self.name)


def make(tmp_path: Path, *providers: Eyes, **cfg: Any) -> AttachmentService:
    names = [p.name for p in providers]
    router = ModelRouter({p.name: p for p in providers}, {"vision": names, "chat": names})
    return AttachmentService(AttachmentsConfig(ocr="off", **cfg), tmp_path, router)


def sent_image(part: dict[str, Any]) -> Any:
    from PIL import Image

    url = part["url"]
    assert url.startswith("data:image/jpeg;base64,")
    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))


def picture(mode: str, size: tuple[int, int], fmt: str, **save: Any) -> bytes:
    from PIL import Image, ImageDraw

    if mode == "RGBA":
        img = Image.new("RGBA", size, (0, 0, 0, 0))  # fully transparent background
        ImageDraw.Draw(img).rectangle((10, 10, 30, 30), fill=(0, 0, 0, 255))  # black "text" in one corner
    elif mode == "P":
        img = Image.new("P", size, 0)
        img.putpalette([0, 0, 0, 200, 30, 30] + [0] * 762)
        ImageDraw.Draw(img).rectangle((10, 10, 30, 30), fill=1)
        save.setdefault("transparency", 0)
    else:
        img = Image.new(mode, size, 128)
    buf = io.BytesIO()
    img.save(buf, fmt, **save)
    return buf.getvalue()


# ---- the bytes that reach the model -------------------------------------------------------------------------------
async def test_a_photo_reaches_the_vision_model_as_a_small_jpeg(tmp_path):
    from delta.evals import attachment_fixtures as F

    eyes = Eyes("eyes")
    svc = make(tmp_path, eyes)
    att, _ = await svc.ingest_bytes(F.jpeg(4000, 3000), "photo.jpg")
    out = await svc.prepare(TaskRequest("what is this?", attachments=[att.id]))
    assert len(eyes.pictures) == 1
    img = sent_image(eyes.pictures[0])
    assert max(img.size) <= 1024 and img.format == "JPEG"
    assert "A red bicycle" in out.context
    assert out.notes == []


@pytest.mark.parametrize(("mode", "fmt", "name"), [("RGBA", "PNG", "shot.png"), ("RGBA", "WEBP", "shot.webp"),
                                                   ("P", "PNG", "palette.png"), ("P", "GIF", "anim.gif")])
async def test_transparent_pictures_are_flattened_on_white_not_black(tmp_path, mode, fmt, name):
    eyes = Eyes("eyes")
    svc = make(tmp_path, eyes)
    att, _ = await svc.ingest_bytes(picture(mode, (200, 120), fmt), name)
    await svc.prepare(TaskRequest("what is in this picture?", attachments=[att.id]))
    img = sent_image(eyes.pictures[0]).convert("RGB")
    corner = img.getpixel((190, 110))  # was transparent
    assert min(corner) > 230, f"transparent area became {corner}, not white"
    mark = img.getpixel((20, 20))      # the drawn shape is still there
    assert max(mark) < 230


def test_open_image_handles_16_bit_and_greyscale_pictures(tmp_path):
    from PIL import Image

    for mode in ("I;16", "L", "LA", "CMYK"):
        p = tmp_path / f"x-{mode.replace(';', '')}.png"
        img = Image.new(mode, (50, 40))
        img.save(p, "TIFF" if mode == "CMYK" else "PNG")
        out = I.open_image(p)
        assert out.mode == "RGB" and out.size == (50, 40)


def test_prepare_for_model_keeps_a_noisy_picture_under_the_byte_cap():
    import os

    from PIL import Image

    noisy = Image.frombytes("RGB", (2048, 2048), os.urandom(2048 * 2048 * 3))
    b64, w, h = I.prepare_for_model(noisy, 2048, low=False)
    assert max(w, h) <= 2048
    assert len(b64) <= I.MAX_B64_BYTES  # providers refuse very large pictures (Groq: 4 MB base64)


def test_heic_is_read_when_pillow_heif_is_there_and_explained_when_not(monkeypatch):
    head = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"
    monkeypatch.setattr(I, "heif_support", lambda: False)
    with pytest.raises(AttachmentError) as e:
        detect(head, "IMG_0001.HEIC")
    words = str(e.value)
    assert "HEIC" in words and ("JPG" in words or "JPEG" in words) and "pillow-heif" in words
    monkeypatch.setattr(I, "heif_support", lambda: True)
    assert detect(head, "IMG_0001.HEIC") == IMAGE
    assert detect(head, "photo.jpg") == IMAGE  # renamed: the first bytes decide


# ---- when the picture is looked at --------------------------------------------------------------------------------
class Ocr:
    def __init__(self, words: str) -> None:
        self.words = words

    async def probe(self) -> bool:
        return True

    async def read(self, img: Any) -> list[Any]:
        from delta.perception.ocr import OcrLine

        return [OcrLine(self.words, (0, 0, 10, 10))]


@pytest.mark.parametrize("ask", ["explain this", "can you recognise this?", "solve this", "check this one",
                                 "Look at the attached file and tell me briefly what it is and what matters in it."])
async def test_a_screenshot_with_text_is_still_looked_at_unless_only_its_text_is_wanted(tmp_path, ask):
    from delta.evals import attachment_fixtures as F

    eyes = Eyes("eyes")
    svc = make(tmp_path, eyes)
    svc.ocr = Ocr("Error 0x80070005 access is denied while installing the update package on this computer " * 3)
    att, _ = await svc.ingest_bytes(F.png(), "error.png")
    out = await svc.prepare(TaskRequest(ask, attachments=[att.id]))
    assert len(eyes.pictures) == 1 and "What the picture shows" in out.context


async def test_only_text_wanted_and_ocr_enough_skips_the_model(tmp_path):
    from delta.evals import attachment_fixtures as F

    eyes = Eyes("eyes")
    svc = make(tmp_path, eyes)
    svc.ocr = Ocr("Invoice 4471 total due 12,400 rupees by 30 November " * 3)
    att, _ = await svc.ingest_bytes(F.png(), "invoice.png")
    out = await svc.prepare(TaskRequest("copy the text from this", attachments=[att.id]))
    assert eyes.pictures == [] and "Invoice 4471" in out.context


# ---- models that cannot or did not see it -------------------------------------------------------------------------
@pytest.mark.parametrize("refusal", ["I'm sorry, but I can't view images. Could you describe it?",
                                     "As a text-based AI, I am unable to see the image you attached.",
                                     "I cannot see images; please paste the text."])
async def test_a_model_that_says_it_cannot_see_is_not_taken_as_having_seen(tmp_path, refusal):
    from delta.evals import attachment_fixtures as F

    blind, eyes = Eyes("blind", refusal), Eyes("eyes")
    svc = make(tmp_path, blind, eyes)
    att, _ = await svc.ingest_bytes(F.png(), "photo.png")
    out = await svc.prepare(TaskRequest("what is in this photo?", attachments=[att.id]))
    assert len(blind.pictures) == 1 and len(eyes.pictures) == 1
    assert "A red bicycle" in out.context and "can't view" not in out.context
    assert "seen by eyes" in out.context


async def test_when_every_model_refuses_the_user_is_told_which_and_why(tmp_path):
    from delta.evals import attachment_fixtures as F

    blind = Eyes("blind", "I can't see images.")
    svc = make(tmp_path, blind)
    att, _ = await svc.ingest_bytes(F.png(), "photo.png")
    out = await svc.prepare(TaskRequest("what is in this photo?", attachments=[att.id]))
    assert "NOT seen" in out.context
    assert out.notes and "blind" in out.notes[0] and "cannot see pictures" in out.notes[0]


async def test_a_failed_vision_call_says_why_not_connect_an_ai(tmp_path):
    from delta.evals import attachment_fixtures as F

    eyes = Eyes("codex", LLMError("codex: timed out after 240s"))
    svc = make(tmp_path, eyes)
    att, _ = await svc.ingest_bytes(F.png(), "photo.png")
    out = await svc.prepare(TaskRequest("what is in this photo?", attachments=[att.id]))
    assert out.notes and "timed out" in out.notes[0]
    assert "connect an AI that can see" not in out.notes[0]


async def test_an_unexpected_error_while_looking_does_not_lose_the_request(tmp_path, monkeypatch):
    from delta.evals import attachment_fixtures as F

    eyes = Eyes("eyes")
    svc = make(tmp_path, eyes)
    att, _ = await svc.ingest_bytes(F.png(), "photo.png")

    def broken(*a: Any, **k: Any) -> Any:
        raise OSError("disk hiccup")

    monkeypatch.setattr(I, "prepare_for_model", broken)
    out = await svc.prepare(TaskRequest("what is in this photo?", attachments=[att.id]))
    assert "photo.png" in out.context and out.notes  # still answered, and the user hears the picture was not seen


async def test_no_model_that_can_see_names_ones_that_can(tmp_path):
    from delta.evals import attachment_fixtures as F

    svc = make(tmp_path, Eyes("groq", sees=False))
    att, _ = await svc.ingest_bytes(F.png(), "photo.png")
    out = await svc.prepare(TaskRequest("what is in this photo?", attachments=[att.id]))
    note = out.notes[0]
    assert "Codex" in note and "Claude Code" in note and "vision" in note.lower()


async def test_the_answering_ai_is_told_the_picture_was_seen(tmp_path):
    from delta.evals import attachment_fixtures as F

    svc = make(tmp_path, Eyes("eyes"))
    att, _ = await svc.ingest_bytes(F.png(), "photo.png")
    out = await svc.prepare(TaskRequest("what is in this photo?", attachments=[att.id]))
    assert "never say you cannot see" in out.context.lower()


def test_refusal_detector_keeps_real_descriptions():
    from delta.attachments.service import is_refusal

    assert is_refusal("I'm unable to view images in this chat.")
    assert is_refusal("Sorry, I can't see the image.")
    assert not is_refusal("A sign that says 'I can't see you' on a door, white text on red.")
    assert not is_refusal("Screenshot of an error: 'Cannot open image file'. The dialog has an OK button.")
    assert not is_refusal(text("x").content)
