"""The dashboard's attachment routes: token required, streamed upload with a size limit, plain-words errors, a
preview, sending files with a message (they join that chat), and removal with the chat."""

from __future__ import annotations

import pytest
from engine_helpers import no_registry
from fastapi.testclient import TestClient

from delta.api.server import create_app, load_token
from delta.attachments.kinds import DEFAULT_REQUEST
from delta.bootstrap import build
from delta.config import AttachmentsConfig, Settings
from delta.evals import attachment_fixtures as F


@pytest.fixture
def app(tmp_path, monkeypatch):
    no_registry(monkeypatch, tmp_path)
    c = build(Settings(data_dir=tmp_path, attachments=AttachmentsConfig(max_mb=1, ocr="off")))
    token = load_token(tmp_path)
    with TestClient(create_app(c, token)) as tc:
        tc.headers["Authorization"] = f"Bearer {token}"
        yield tc, c


def upload(tc: TestClient, data: bytes, name: str):
    return tc.put(f"/api/attachments?name={name}", content=data,
                  headers={"Content-Type": "application/octet-stream"})


def test_upload_needs_the_token(app):
    tc, _ = app
    r = TestClient(tc.app).put("/api/attachments?name=a.png", content=F.png())
    assert r.status_code == 401


def test_upload_a_picture_and_a_pdf(app):
    tc, _ = app
    r = upload(tc, F.png(), "photo.png")
    assert r.status_code == 200, r.text
    att = r.json()["attachment"]
    assert att["kind"] == "image" and att["summary"] == "640x480" and "path" not in att
    assert r.json()["can_see_pictures"] is False  # no model set up in this test
    pdf = upload(tc, F.text_pdf([["a"], ["b"]]), "r.pdf").json()["attachment"]
    assert pdf["pages"] == 2 and pdf["summary"] == "2 pages"
    prev = tc.get(f"/api/attachments/{att['id']}/preview")
    assert prev.status_code == 200 and prev.headers["content-type"] == "image/jpeg"
    assert tc.get(f"/api/attachments/{pdf['id']}/preview").status_code == 404


def test_too_big_unsupported_and_damaged_files_get_plain_words(app):
    tc, c = app
    big = upload(tc, b"a" * (2 * 1024 * 1024), "big.txt")
    assert big.status_code == 413 and "The limit is 1 MB" in big.json()["detail"]

    def chunks():  # no Content-Length: the limit is counted while streaming
        for _ in range(40):
            yield b"b" * 65536

    streamed = tc.put("/api/attachments?name=big.txt", content=chunks())
    assert streamed.status_code == 413
    xlsx = upload(tc, b"PK\x03\x04....", "sheet.xlsx")
    assert xlsx.status_code == 400 and "csv" in xlsx.json()["detail"]
    bomb = upload(tc, F.docx_with_doctype_bomb(), "evil.docx")
    assert bomb.status_code == 400 and "DOCTYPE" in bomb.json()["detail"]
    # nothing that was refused is kept
    root = c.settings.data_dir / "attachments"
    assert not [p for p in root.rglob("*") if p.is_file() and ".cache" not in p.parts]


def test_sending_files_with_a_message_puts_them_in_that_chat(app):
    tc, c = app
    att = upload(tc, F.simple_pptx(), "deck.pptx").json()["attachment"]
    r = tc.post("/api/tasks", json={"text": "", "attachments": [att["id"]]})
    assert r.status_code == 200, r.text
    chat_id = r.json()["chat_id"]
    assert r.json()["text"] == DEFAULT_REQUEST
    first = tc.get(f"/api/chats/{chat_id}").json()["messages"][0]
    assert first["role"] == "user" and first["text"] == DEFAULT_REQUEST
    assert first["meta"]["attachments"][0]["name"] == "deck.pptx"
    svc = c.orchestrator.attachments
    assert svc.store.get(att["id"]).folder == chat_id
    listed = tc.get(f"/api/attachments?chat_id={chat_id}").json()
    assert [a["id"] for a in listed["attachments"]] == [att["id"]]
    # a file already sent in one chat cannot be attached in another one
    other = tc.post("/api/tasks", json={"text": "hi", "attachments": [att["id"]], "chat_id": ""})
    assert other.status_code == 400 and "Attach it again" in other.json()["detail"]
    # deleting the chat removes its files
    assert tc.delete(f"/api/chats/{chat_id}").json()["ok"]
    assert svc.store.get(att["id"]) is None


def test_message_rules(app):
    tc, _ = app
    assert tc.post("/api/tasks", json={"text": "  "}).status_code == 400
    assert tc.post("/api/tasks", json={"text": "x", "attachments": ["0123456789ab"]}).status_code == 400
    ids = [upload(tc, F.png(), f"p{i}.png").json()["attachment"]["id"] for i in range(6)]
    many = tc.post("/api/tasks", json={"text": "x", "attachments": ids})
    assert many.status_code == 400 and "at most 5" in many.json()["detail"]
    removed = tc.delete(f"/api/attachments/{ids[0]}")
    assert removed.json() == {"ok": True}


def test_attachments_switched_off(tmp_path, monkeypatch):
    no_registry(monkeypatch, tmp_path)
    c = build(Settings(data_dir=tmp_path, attachments=AttachmentsConfig(enabled=False)))
    token = load_token(tmp_path)
    with TestClient(create_app(c, token)) as tc:
        r = tc.put("/api/attachments?name=a.png", content=F.png(), headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 503 and "switched off" in r.json()["detail"]
        assert "read_attachment" not in c.registry.names()


def test_settings_pictures_choice_is_checked_and_applied(app):
    tc, c = app
    s = tc.get("/api/settings").json()["attachments"]
    assert s["vision"] == "auto" and "providers" in s
    bad = tc.put("/api/settings", json={"attachments": {"vision": "not-a-provider"}})
    assert bad.status_code == 400
    ok = tc.put("/api/settings", json={"attachments": {"vision": "off"}})
    assert ok.status_code == 200 and c.settings.attachments.vision == "off"
    assert c.orchestrator.attachments.cfg.vision == "off"  # the running service sees it at once
