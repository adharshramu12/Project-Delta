"""Playback proof from Windows' media controls instead of screenshots."""

import json

from delta.tools import media
from delta.tools.media import ControlArgs, MediaControl, MediaStatus, NoArgs, describe

SITA = [{"app": "Chrome", "title": "Inthandham Video Song - Sita Ramam (Telugu)", "artist": "Sony Music South",
         "status": "Playing", "position_s": 38, "length_s": 241}]


def test_describe_is_short_and_exact():
    assert describe(SITA) == ('PLAYING: "Inthandham Video Song - Sita Ramam (Telugu)" by Sony Music South '
                              "at 0:38 of 4:01 (in Chrome)")
    assert describe([]).startswith("Nothing is playing")
    just_started = [{**SITA[0], "position_s": 0}]
    assert "position not reported yet" in describe(just_started)  # browsers report late


async def test_status_reports_what_windows_says(monkeypatch):
    async def ps(script, timeout_s=20):
        return json.dumps(SITA[0])  # PowerShell gives a bare object when there is one session
    monkeypatch.setattr(media, "_powershell", ps)
    r = await MediaStatus().run(NoArgs())
    assert r.ok and r.evidence["playing"] == ["Inthandham Video Song - Sita Ramam (Telugu)"]


async def test_control_rejects_unknown_actions_and_reports_new_state(monkeypatch):
    calls = []

    async def ps(script, timeout_s=20):
        calls.append(script)
        return "ok" if "TryPause" in script else json.dumps(SITA)
    monkeypatch.setattr(media, "_powershell", ps)
    monkeypatch.setattr(media.asyncio, "sleep", lambda s: __import__("asyncio").sleep(0) if False else _noop())
    assert not (await MediaControl().run(ControlArgs(action="explode"))).ok
    r = await MediaControl().run(ControlArgs(action="pause"))
    assert r.ok and "TryPauseAsync" in calls[0] and "PLAYING" in r.output


async def _noop():
    return None


def test_youtube_results_are_read_from_the_page_data():
    from delta.tools.media import parse_results

    data = {"contents": {"x": [
        {"adSlotRenderer": {}},
        {"videoRenderer": {"videoId": "jwY0wDCQt4s", "title": {"runs": [{"text": "Kaanunna Kalyanam Video"}]},
                           "ownerText": {"runs": [{"text": "Sony Music South"}]},
                           "lengthText": {"simpleText": "3:27"}}},
        {"reelShelfRenderer": {}}]}}
    html = "<script>var ytInitialData = " + json.dumps(data) + ";</script>"
    assert parse_results(html) == [{"id": "jwY0wDCQt4s", "title": "Kaanunna Kalyanam Video",
                                    "channel": "Sony Music South", "length": "3:27"}]
    assert parse_results("<html>consent page</html>") == []
