"""The Library (everything Delta made, with the chat it came from) and the screen co-pilot (Ctrl+Alt+D)."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from delta.library import Library, LibraryError


class Chats:
    def get(self, chat_id: str) -> Any:
        if chat_id != "c1":
            return None
        return SimpleNamespace(public=lambda: {"title": "Solar deck for class"})


def make(tmp_path: Path) -> tuple[Library, Path]:
    docs = tmp_path / "Delta"
    (docs / "Pictures").mkdir(parents=True)
    (docs / "Solar Power.pptx").write_bytes(b"pk")
    (docs / "Budget.xlsx").write_bytes(b"pk")
    (docs / "notes.txt").write_text("not Delta's")  # not a document type Delta makes
    (docs / "Pictures" / "openverse-0123456789ab.jpg").write_bytes(b"\xff\xd8\xff")
    (docs / "Pictures" / "holiday.jpg").write_bytes(b"\xff\xd8\xff")  # not a name find_pictures makes
    return Library(tmp_path / "data", lambda: docs, Chats(), None), docs


def test_lists_only_what_delta_made_with_the_chat_it_came_from(tmp_path: Path) -> None:
    lib, _ = make(tmp_path)
    asyncio.run(lib.on_event("document.created", {"files": ["Solar Power.pptx"], "chat_id": "c1", "title": "Solar"}))
    asyncio.run(lib.on_event("pictures.found", {"pictures": [{"name": "openverse-0123456789ab.jpg"}], "chat_id": "c1",
                                                "query": "solar farm"}))
    items = {d["name"]: d for d in lib.items()}
    assert set(items) == {"Solar Power.pptx", "Budget.xlsx", "openverse-0123456789ab.jpg"}
    assert items["Solar Power.pptx"]["kind"] == "presentation"
    assert items["Solar Power.pptx"]["chat_title"] == "Solar deck for class"
    assert items["Budget.xlsx"]["kind"] == "spreadsheet" and items["Budget.xlsx"]["chat_id"] == ""
    assert items["openverse-0123456789ab.jpg"]["kind"] == "picture"
    assert items["openverse-0123456789ab.jpg"]["what"] == "solar farm"


def test_rename_keeps_the_extension_and_the_chat_link(tmp_path: Path) -> None:
    lib, docs = make(tmp_path)
    lib.note(["Solar Power.pptx"], "c1", "Solar")
    new = lib.rename("doc:Solar Power.pptx", "Solar for Class 10")
    assert new == "doc:Solar for Class 10.pptx" and (docs / "Solar for Class 10.pptx").is_file()
    assert {d["name"]: d for d in lib.items()}["Solar for Class 10.pptx"]["chat_id"] == "c1"
    assert lib.rename("doc:Budget.xlsx", "Budget 2026.xlsx") == "doc:Budget 2026.xlsx"  # typed extension not doubled


@pytest.mark.parametrize("bad", ["../evil", "a/b", "C:\\x", "", "   ", "dots.", "x" * 101])
def test_rename_refuses_paths_and_odd_names(tmp_path: Path, bad: str) -> None:
    lib, _ = make(tmp_path)
    with pytest.raises(LibraryError):
        lib.rename("doc:Budget.xlsx", bad)


def test_rename_refuses_pictures_taken_names_and_unknown_ids(tmp_path: Path) -> None:
    lib, _ = make(tmp_path)
    with pytest.raises(LibraryError):
        lib.rename("pic:openverse-0123456789ab.jpg", "nice")
    (lib._folder() / "Old Budget.xlsx").write_bytes(b"pk")
    with pytest.raises(LibraryError, match="already a file"):
        lib.rename("doc:Budget.xlsx", "Old Budget")
    with pytest.raises(LibraryError):
        lib.path_of("doc:notes.txt")  # not listed, so not reachable
    with pytest.raises(LibraryError):
        lib.path_of("doc:../../secret.pdf")


def test_delete_goes_to_the_recycle_bin_and_projects_are_never_deleted(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    lib, docs = make(tmp_path)
    binned: list[str] = []
    import send2trash

    monkeypatch.setattr(send2trash, "send2trash", lambda p: (binned.append(p), Path(p).unlink()))
    lib.note(["Budget.xlsx"], "c1", "Budget")
    lib.delete("doc:Budget.xlsx")
    assert binned == [str(docs / "Budget.xlsx")]
    assert "Budget.xlsx" not in lib._origins()
    with pytest.raises(LibraryError):
        lib.delete("proj:abc")


def test_build_projects_are_listed(tmp_path: Path) -> None:
    lib, _ = make(tmp_path)
    proj = SimpleNamespace(id="p1", name="Space Dodge", path="C:/x", opened=2e9, last_run=0)
    lib.studio = SimpleNamespace(store=SimpleNamespace(projects=lambda: [proj]))
    first = lib.items()[0]
    assert first["id"] == "proj:p1" and first["kind"] == "project"


# --- screen co-pilot ------------------------------------------------------------------------------------------


def test_copilot_puts_the_screen_in_a_new_chat_and_sends_nothing(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    from delta.system import copilot
    from delta.tools import screen

    saved: list[tuple] = []
    events: list[tuple] = []
    shown: list[bool] = []
    att = SimpleNamespace(public=lambda: {"id": "a1", "name": "Screen.jpg", "kind": "image"})

    def save_bytes(data: bytes, name: str, chat_id: str, source: str) -> Any:
        saved.append((data, name, chat_id, source))
        return att

    class Events:
        async def publish(self, name: str, **data: Any) -> None:
            events.append((name, data))

    audit: list[str] = []
    c = SimpleNamespace(orchestrator=SimpleNamespace(attachments=SimpleNamespace(store=SimpleNamespace(
        save_bytes=save_bytes))), events=Events(), audit=SimpleNamespace(record=lambda e, **k: audit.append(e)))
    monkeypatch.setattr(screen, "capture_jpeg", lambda w: (base64.b64encode(b"\xff\xd8\xffjpeg").decode(), (1, 1),
                                                           (1, 1)))
    out = asyncio.run(copilot.capture_to_chat(c, lambda: shown.append(True)))
    assert out == {"id": "a1", "name": "Screen.jpg", "kind": "image"}
    assert saved[0][1:] == ("Screen.jpg", "", "copilot")  # unsent: no chat yet
    assert events == [("copilot.screen", {"attachment": out})] and shown == [True]
    assert audit == ["copilot.screen_taken"]


def test_copilot_does_nothing_without_attachments() -> None:
    from delta.system import copilot

    assert asyncio.run(copilot.capture_to_chat(SimpleNamespace(orchestrator=None), lambda: None)) is None


def test_an_empty_message_with_the_screen_asks_about_the_screen() -> None:
    from delta.api.server import SCREEN_REQUEST

    assert "my screen" in SCREEN_REQUEST and "what I should do next" in SCREEN_REQUEST
