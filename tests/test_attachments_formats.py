"""Reading attached files on this PC: PDF, Word, PowerPoint, text and pictures, including hostile files (zip and XML
bombs, external entities, macros, path traversal, renamed programs, oversized files). Fixtures are made in code."""

from __future__ import annotations

import asyncio
import struct
import time
import zlib
from pathlib import Path

import pytest

from delta.attachments import extract as extract_mod
from delta.attachments.extract import ExtractCache, parse_pages, read_document
from delta.attachments.images import open_image, prepare_for_model, tokens_for
from delta.attachments.kinds import AttachmentError, detect, safe_name
from delta.attachments.ooxml import OfficeError, read_docx, read_pptx
from delta.attachments.pdf import PdfError, read_pdf
from delta.attachments.store import AttachmentStore
from delta.evals import attachment_fixtures as F


def put(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


# ---- PDF --------------------------------------------------------------------------------------------------------
def test_pdf_pages_in_order_with_title(tmp_path):
    doc = read_pdf(put(tmp_path, "a.pdf", F.text_pdf([["Hello (world)", "Line two"], ["Page two"], ["Three"]],
                                                     title="Water Report")))
    assert doc.page_count == 3 and doc.title == "Water Report" and not doc.encrypted
    assert doc.pages[0].text == "Hello (world)\nLine two"
    assert [p.text for p in doc.pages[1:]] == ["Page two", "Three"]


def test_pdf_uncompressed_and_object_streams(tmp_path):
    assert read_pdf(put(tmp_path, "u.pdf", F.text_pdf([["plain text"]], compress=False))).pages[0].text == "plain text"
    doc = read_pdf(put(tmp_path, "o.pdf", F.objstm_pdf("Inside an object stream")))
    assert doc.pages[0].text == "Inside an object stream"


def test_pdf_tounicode_two_byte_fonts_keep_telugu_and_rupee(tmp_path):
    doc = read_pdf(put(tmp_path, "t.pdf", F.tounicode_pdf("నమస్కారం Total ₹500")))
    assert doc.pages[0].text == "నమస్కారం Total ₹500"


def test_pdf_unknown_glyph_names_fall_back_to_the_base_encoding(tmp_path):
    """TeX bitmap fonts name glyphs a65, a66...: the code itself is the letter."""
    raw = F.text_pdf([["Budget"]]).replace(
        b"/Encoding /WinAnsiEncoding", b"/Encoding << /Differences [66 /a66 /u /d /g /e /t] >>")
    assert read_pdf(put(tmp_path, "g.pdf", raw)).pages[0].text == "Budget"


def test_pdf_scanned_page_and_encrypted_file_are_reported(tmp_path):
    scan = read_pdf(put(tmp_path, "s.pdf", F.scanned_pdf()))
    assert scan.pages[0].scanned and scan.pages[0].images == 1
    locked = read_pdf(put(tmp_path, "e.pdf", F.encrypted_pdf()))
    assert locked.encrypted and locked.pages == [] and locked.page_count == 1


def test_pdf_zip_bomb_stops_at_the_cap_quickly(tmp_path):
    started = time.monotonic()
    with pytest.raises(PdfError, match="zip bomb"):
        read_pdf(put(tmp_path, "b.pdf", F.bomb_pdf(40)))
    assert time.monotonic() - started < 10


def test_pdf_page_limit_and_garbage(tmp_path):
    doc = read_pdf(put(tmp_path, "m.pdf", F.text_pdf([[f"page {i}"] for i in range(1, 11)])), max_pages=4)
    assert doc.page_count == 10 and len(doc.pages) == 4 and doc.truncated
    with pytest.raises(PdfError):
        read_pdf(put(tmp_path, "x.pdf", b"%PDF-1.4 nothing here"))
    with pytest.raises(PdfError):
        read_pdf(put(tmp_path, "empty.pdf", b""))


def test_pdf_extract_marks_scans_and_protection(tmp_path):
    ex = read_document(put(tmp_path, "s.pdf", F.scanned_pdf()), "pdf", 50)
    assert ex.scanned == [1] and ex.summary() == "1 page, 1 scanned"
    locked = read_document(put(tmp_path, "e.pdf", F.encrypted_pdf()), "pdf", 50)
    assert locked.encrypted and "protected" in locked.notes[0] and locked.summary() == "protected PDF"


# ---- Word ---------------------------------------------------------------------------------------------------------
def test_docx_headings_pages_and_tables(tmp_path):
    doc = read_docx(put(tmp_path, "a.docx", F.simple_docx()))
    assert doc.paged and [p.title for p in doc.parts] == ["Budget 2026", "Next steps"]
    assert doc.parts[0].text == "# Budget 2026\nWe plan to buy one laptop."
    assert "Item | Cost\nLaptop | 52000" in doc.parts[1].text


def test_docx_without_page_breaks_is_split_into_parts_at_headings(tmp_path):
    body = "".join(F._para(f"Section {i}", "Heading1") + F._para("word " * 400) for i in range(3))
    doc = read_docx(put(tmp_path, "b.docx", F.docx(body)), part_chars=1500)
    assert not doc.paged and len(doc.parts) == 3 and doc.parts[2].title == "Section 2"


@pytest.mark.parametrize("maker", [F.docx_with_doctype_bomb, F.docx_with_external_entity])
def test_docx_xml_bombs_and_external_entities_are_refused(tmp_path, maker):
    started = time.monotonic()
    with pytest.raises(OfficeError, match="DOCTYPE"):
        read_docx(put(tmp_path, "evil.docx", maker()))
    assert time.monotonic() - started < 5


def test_docx_zip_bomb_is_refused_without_inflating_it(tmp_path):
    with pytest.raises(OfficeError, match="zip bomb"):
        read_docx(put(tmp_path, "bomb.docx", F.zip_bomb_docx(300)))


def test_docx_macros_and_embedded_objects_are_never_opened(tmp_path, monkeypatch):
    import zipfile

    path = put(tmp_path, "m.docx", F.docx_with_macro_and_injection())
    opened: list[str] = []
    real = zipfile.ZipFile.open

    def spy(self, name, *a, **k):
        opened.append(getattr(name, "filename", name))
        return real(self, name, *a, **k)

    monkeypatch.setattr(zipfile.ZipFile, "open", spy)
    doc = read_docx(path)
    assert opened == ["word/document.xml"]
    assert "It contains macros. They were not run." in doc.notes
    assert "It contains embedded objects. They were not opened." in doc.notes
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in doc.parts[0].text  # kept as data, labelled outside content later


def test_damaged_office_files_say_so(tmp_path):
    with pytest.raises(OfficeError, match="damaged"):
        read_docx(put(tmp_path, "x.docx", b"PK\x03\x04garbage"))


# ---- PowerPoint ---------------------------------------------------------------------------------------------------
def test_pptx_slides_titles_notes_tables_and_picture_alt_text(tmp_path):
    doc = read_pptx(put(tmp_path, "a.pptx", F.simple_pptx(table_on=2)))
    assert doc.total == 3 and [p.title for p in doc.parts] == ["Launch plan", "Budget", "Risks"]
    assert "[picture: Bar chart of users]" in doc.parts[0].text
    assert "Quarter | Users\nQ1 | 1200" in doc.parts[1].text
    assert doc.parts[1].text.endswith("Speaker notes: Mention the grant from the state.")
    assert any("did not look at them" in n for n in doc.notes)


def test_pptx_relationship_outside_the_package_is_never_followed(tmp_path):
    doc = read_pptx(put(tmp_path, "t.pptx", F.traversal_pptx()))
    assert doc.parts == [] and doc.total == 0


# ---- text ---------------------------------------------------------------------------------------------------------
def test_csv_parts_repeat_the_header(tmp_path):
    rows = "name,marks\n" + "".join(f"student{i},{i}\n" for i in range(800))
    ex = read_document(put(tmp_path, "m.csv", rows.encode()), "text", 50)
    assert ex.total > 2 and all(s.text.startswith("name,marks\n") for s in ex.sections)
    assert ex.notes[0] == "Columns: name,marks"


def test_text_encodings(tmp_path):
    assert read_document(put(tmp_path, "u.txt", "నమస్కారం".encode("utf-16")), "text", 5).sections[0].text == "నమస్కారం"
    assert read_document(put(tmp_path, "c.txt", "café".encode("cp1252")), "text", 5).sections[0].text == "café"


# ---- pictures -----------------------------------------------------------------------------------------------------
def test_photos_are_downscaled_before_any_model_sees_them(tmp_path):
    img = open_image(put(tmp_path, "big.jpg", F.jpeg(4000, 3000)))
    b64, w, h = prepare_for_model(img, 1024, low=False)
    assert (w, h) == (1024, 768) and len(b64) < 200_000
    _, lw, lh = prepare_for_model(img, 1024, low=True)
    assert max(lw, lh) == 512
    assert tokens_for(w, h, False) == 765 and tokens_for(lw, lh, True) == 85


def _png_header(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" * 10)) + chunk(b"IEND", b""))


def test_a_decompression_bomb_picture_is_refused_before_decoding(tmp_path):
    with pytest.raises(AttachmentError, match="too large"):
        open_image(put(tmp_path, "bomb.png", _png_header(30000, 30000)))


def test_damaged_picture(tmp_path):
    with pytest.raises(AttachmentError):
        open_image(put(tmp_path, "x.png", b"\x89PNG\r\n\x1a\nnot really"))


# ---- what a file is, and its name -------------------------------------------------------------------------------
def test_detect_by_content_not_name():
    assert detect(b"%PDF-1.7 ...", "report.pdf") == "pdf"
    assert detect(b"\x89PNG\r\n\x1a\n....", "photo.pdf") == "image"  # a picture renamed .pdf is still a picture
    with pytest.raises(AttachmentError, match="never opened or run"):
        detect(b"MZ\x90\x00", "invoice.pdf")  # a program renamed .pdf
    with pytest.raises(AttachmentError, match=r"\.docx"):
        detect(b"\xd0\xcf\x11\xe0", "old.doc")
    with pytest.raises(AttachmentError, match="macros"):
        detect(b"PK\x03\x04", "evil.docm")
    with pytest.raises(AttachmentError, match="empty"):
        detect(b"", "a.txt")
    with pytest.raises(AttachmentError, match="binary"):
        detect(b"abc\x00def", "notes.txt")


def test_safe_names():
    assert safe_name("../../Windows/System32/evil.pdf") == "evil.pdf"
    assert safe_name("..\\..\\boot.ini") == "boot.ini"
    assert safe_name("CON.txt") == "file.txt"
    assert safe_name("a\x00b<c>.pdf") == "ab_c_.pdf"
    assert len(safe_name("x" * 500 + ".pdf")) <= 120 and safe_name("x" * 500 + ".pdf").endswith(".pdf")


def test_parse_pages():
    assert parse_pages("3", 10) == [3]
    assert parse_pages("2-4, 9", 10) == [2, 3, 4, 9]
    assert parse_pages("8-20", 10) == [8, 9, 10]
    assert parse_pages("last", 7) == [7]
    assert parse_pages("all", 50, limit=5) == [1, 2, 3, 4, 5]
    assert parse_pages("nonsense", 5) == []


# ---- the store ------------------------------------------------------------------------------------------------------
async def _chunks(data: bytes, size: int = 4096):
    for i in range(0, len(data), size):
        yield data[i:i + size]
        await asyncio.sleep(0)


async def test_an_oversized_upload_stops_while_streaming_and_leaves_nothing(tmp_path):
    store = AttachmentStore(tmp_path / "attachments", max_mb=1)
    with pytest.raises(AttachmentError, match="The limit is 1 MB"):
        await store.save_stream(_chunks(b"a" * (2 * 1024 * 1024)), "big.txt")  # no declared size: counted
    with pytest.raises(AttachmentError, match="The limit is 1 MB"):
        await store.save_stream(_chunks(b"x"), "big.txt", declared=5 * 1024 * 1024)
    assert not any(p.is_file() for p in (tmp_path / "attachments").rglob("*"))


async def test_store_files_by_random_id_moves_and_prunes(tmp_path):
    store = AttachmentStore(tmp_path / "attachments", retention_days=30)
    att = await store.save_stream(_chunks(F.text_pdf([["hi"]])), "../../../evil.pdf")
    assert att.name == "evil.pdf" and att.folder == "unsent" and len(att.id) == 12
    assert store.path(att).parent == tmp_path / "attachments" / "unsent"
    assert store.get(att.id) == att and store.get("../../x") is None and store.get("zzzzzzzzzzzz") is None
    moved = store.move(att, "abc123def456")
    assert store.get(att.id).folder == "abc123def456" and store.path(moved).is_file()
    assert store.prune(now=att.created + 31 * 86400) == 1 and store.get(att.id) is None


async def test_unsupported_upload_is_refused_in_plain_words_and_not_kept(tmp_path):
    store = AttachmentStore(tmp_path / "attachments")
    with pytest.raises(AttachmentError, match="Excel"):
        await store.save_stream(_chunks(b"PK\x03\x04..."), "sheet.xlsx")
    assert not any(p.is_file() for p in (tmp_path / "attachments").rglob("*"))


def test_extract_cache_by_file_hash(tmp_path, monkeypatch):
    cache = ExtractCache(tmp_path / "cache", max_mb=1)
    ex = read_document(put(tmp_path, "a.pdf", F.text_pdf([["one"]])), "pdf", 5)
    cache.put("a" * 64, ex)
    assert cache.get("a" * 64).sections[0].text == "one" and cache.get("b" * 64) is None
    assert cache.get("../../etc") is None
    big = extract_mod.Extract("text", "part", 1, [extract_mod.Section(1, "", "x" * 700_000)])
    for i in range(3):
        cache.put(f"{i}" * 64, big)
    total = sum(p.stat().st_size for p in (tmp_path / "cache").glob("*.json"))
    assert total <= 1024 * 1024  # oldest dropped past max_mb
