"""send_file: a file or folder from the PC arrives in the chat the user asked from."""

import zipfile

from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.tools.deliver import SendFile, SendFileArgs


class FakeTelegram:
    def __init__(self):
        self.sent = []

    async def send_file(self, user, path, caption=""):
        self.sent.append((user, path, caption))


def asked_from(channel, user="6057519957"):
    return CURRENT_REQUEST.set(TaskRequest("send me it", channel=channel, user=user))


async def test_a_file_goes_to_the_person_who_asked(tmp_path):
    deck = tmp_path / "Arva pitch.pptx"
    deck.write_bytes(b"x" * 1000)
    tg = FakeTelegram()
    token = asked_from("telegram")
    try:
        r = await SendFile({"telegram": tg}).run(SendFileArgs(path=str(deck)))
    finally:
        CURRENT_REQUEST.reset(token)
    assert r.ok and r.evidence["delivered"] and tg.sent == [("6057519957", deck, "Arva pitch.pptx")]


async def test_a_folder_is_zipped_first(tmp_path):
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos" / "a.jpg").write_bytes(b"1")
    (tmp_path / "photos" / "b.jpg").write_bytes(b"2")
    tg = FakeTelegram()
    token = asked_from("telegram")
    try:
        r = await SendFile({"telegram": tg}).run(SendFileArgs(path=str(tmp_path / "photos")))
    finally:
        CURRENT_REQUEST.reset(token)
    sent = tg.sent[0][1]
    assert r.ok and sent.suffix == ".zip" and sorted(zipfile.ZipFile(sent).namelist()) == ["a.jpg", "b.jpg"]


async def test_too_big_for_telegram_is_explained_not_failed_silently(tmp_path, monkeypatch):
    from delta.tools import deliver

    monkeypatch.setattr(deliver, "LIMIT_MB", {"telegram": 0.001})
    big = tmp_path / "video.mp4"
    big.write_bytes(b"x" * 5000)
    tg = FakeTelegram()
    token = asked_from("telegram")
    try:
        r = await SendFile({"telegram": tg}).run(SendFileArgs(path=str(big)))
    finally:
        CURRENT_REQUEST.reset(token)
    assert not r.ok and "accepts up to" in r.output and tg.sent == []


async def test_asked_on_the_pc_it_just_says_where_it_is(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hi")
    token = asked_from("api")
    try:
        r = await SendFile({}).run(SendFileArgs(path=str(f)))
    finally:
        CURRENT_REQUEST.reset(token)
    assert r.ok and not r.evidence["delivered"] and str(f) in r.output
