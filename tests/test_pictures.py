"""Free pictures for decks and chat: outline photo lines, keynote-style rendering with cropped photos and credits,
the picture finder's safety checks, and the find_pictures tool. No network: the finders are replaced."""

from __future__ import annotations

import asyncio
import struct
import zipfile
import zlib
from pathlib import Path
from xml.dom import minidom

import pytest

from delta.addons.writer import pictures
from delta.addons.writer.pictures import Picture, _fluent_paths, image_size
from delta.addons.writer.render_pptx import _crop, render_pptx
from delta.addons.writer.slides import parse_outline


def png(w: int, h: int) -> bytes:
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def jpeg_head(w: int, h: int) -> bytes:
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9 + sof + b"\xff\xd9"


OUTLINE = """# Clean Energy
Why now
![solar panels sunset]

## Cheapest power
![solar farm aerial]
- Tariffs fell **80%**
> Start with price.

## Money
![should be dropped]
| Area | Share |
| Roof | 35% |
"""


def test_outline_photo_lines_are_read_and_not_shown_as_points() -> None:
    deck = parse_outline(OUTLINE)
    assert deck.image == "solar panels sunset"
    assert deck.slides[0].image == "solar farm aerial"
    assert all("![" not in t for s in deck.slides for _, t in s.points)
    assert deck.slides[1].image == ""  # never a photo on a table slide


def test_image_size_reads_the_bytes_not_the_name() -> None:
    assert image_size(png(4, 3)) == ("png", 4, 3)
    assert image_size(jpeg_head(640, 480)) == ("jpeg", 640, 480)
    assert image_size(b"<html>not a picture</html>") is None


def test_crop_covers_the_box_without_stretching() -> None:
    assert _crop(2000, 1000, 1000, 1000) == '<a:srcRect l="25000" r="25000"/>'  # wide photo, square box
    assert _crop(1000, 2000, 1000, 1000) == '<a:srcRect t="25000" b="25000"/>'
    assert _crop(1600, 900, 1600, 900) == ""


def test_sticker_paths_follow_fluent_names() -> None:
    paths = _fluent_paths("🎉")
    assert paths[0].endswith("/Party%20popper/3D/party_popper_3d.png")
    assert any(p.endswith("/Thumbs%20up/Default/3D/thumbs_up_3d_default.png") for p in _fluent_paths("👍"))
    assert _fluent_paths("ab") == []


def test_deck_with_photos_is_valid_and_credits_travel(tmp_path: Path) -> None:
    deck = parse_outline(OUTLINE)
    pic = tmp_path / "openverse-0123456789ab.png"
    pic.write_bytes(png(40, 30))
    credit = '"Dawn" by A & B is licensed under CC BY 2.0.'
    deck.picture = Picture(pic, credit, "https://example.org/p", "openverse", 40, 30)
    deck.slides[0].picture = Picture(pic, credit, "https://example.org/p", "openverse", 40, 30)
    out = render_pptx(deck, tmp_path / "d.pptx")
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert "ppt/media/image1.png" in names and "ppt/media/image2.png" in names
        for n in names:
            if n.endswith((".xml", ".rels")):
                minidom.parseString(z.read(n))  # every part is well-formed (quotes in a credit included)
        slide2 = z.read("ppt/slides/slide2.xml").decode()
        assert "<p:pic>" in slide2 and "Dawn" in slide2
        assert 'Target="../media/image2.png"' in z.read("ppt/slides/_rels/slide2.xml.rels").decode()
        assert "https://example.org/p" in z.read("ppt/notesSlides/notesSlide2.xml").decode()
        assert '<Default Extension="png"' in z.read("[Content_Types].xml").decode()


def test_get_image_refuses_plain_http() -> None:
    with pytest.raises(ValueError):
        asyncio.run(pictures._get_image(None, "http://example.org/a.jpg"))


def test_add_photos_fills_what_it_finds_and_skips_the_rest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from delta.addons.writer import tools

    pic = tmp_path / "p.png"
    pic.write_bytes(png(4, 3))

    async def fake(query: str, folder: Path, count: int = 1, orientation: str = "", unsplash_key: str | None = None):
        return [] if "aerial" in query else [Picture(pic, "c", "https://x", "openverse", 4, 3)]

    monkeypatch.setattr(pictures, "find_photos", fake)
    monkeypatch.setattr("delta.system.secrets.get_secret", lambda name: None)
    deck = parse_outline(OUTLINE)
    notes = asyncio.run(tools.add_photos(deck, tmp_path))
    assert deck.picture is not None and deck.slides[0].picture is None
    assert "Added 1 free photo" in notes[0] and "Unsplash key" in notes[0]


def test_find_pictures_tool_shows_them_with_credits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from delta.addons.writer.tools import FindPictures, FindPicturesArgs

    pic = tmp_path / "Pictures" / "openverse-0123456789ab.jpg"
    pic.parent.mkdir()
    pic.write_bytes(jpeg_head(8, 6))
    published: list[tuple[str, dict]] = []

    class Events:
        async def publish(self, name: str, **data: object) -> None:
            published.append((name, data))

    async def fake(query: str, folder: Path, count: int = 1, orientation: str = "", unsplash_key: str | None = None):
        return [Picture(pic, "Photo by A on Unsplash", "https://unsplash.com/@a", "unsplash", 8, 6)]

    monkeypatch.setattr(pictures, "find_photos", fake)
    monkeypatch.setattr("delta.system.secrets.get_secret", lambda name: None)
    monkeypatch.setattr("delta.addons.writer.tools.documents_dir", lambda *a, **k: tmp_path)

    class Cfg:
        output_dir = str(tmp_path)

    tool = FindPictures(None, Cfg(), Events())
    r = asyncio.run(tool.run(FindPicturesArgs(query="mountains")))
    assert r.ok and "Photo by A on Unsplash" in r.output
    assert published[0][0] == "pictures.found"
    assert published[0][1]["pictures"][0]["name"] == pic.name
    bad = asyncio.run(tool.run(FindPicturesArgs(kind="sticker")))
    assert not bad.ok


def test_picture_route_serves_only_names_it_made() -> None:
    from delta.api.documents_routes import PICTURE

    assert PICTURE.fullmatch("unsplash-0123456789ab.jpg")
    assert PICTURE.fullmatch("sticker-0123456789ab.png")
    for bad in ("../secret.jpg", "unsplash-0123456789ab.jpg.exe", "x.jpg", "openverse-0123456789ab.svg"):
        assert not PICTURE.fullmatch(bad)
