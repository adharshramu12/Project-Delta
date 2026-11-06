"""What is playing on this PC, straight from Windows' media controls (the panel behind the volume
icon). Any app that shows there works: YouTube in Chrome/Edge, Spotify, VLC, Windows Media Player.

This is exact and free: no screenshot, no AI call. "Is the song playing?" is answered by Windows
itself (Playing / Paused, title, position), which a still screenshot cannot prove.
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
from typing import Any

from pydantic import BaseModel, Field

from delta.system import proctree
from delta.tools.base import Risk, Tool, ToolResult

# Windows PowerShell 5.1 can call the WinRT media API directly, so nothing extra is installed.
_PRELUDE = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
    $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result
}
""" + ("[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, "
       "ContentType = WindowsRuntime] | Out-Null") + r"""
$mgr = Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) `
             ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
"""

STATUS_SCRIPT = _PRELUDE + r"""
$out = @(foreach ($s in $mgr.GetSessions()) {
    $p = Await ($s.TryGetMediaPropertiesAsync()) `
               ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
    $t = $s.GetTimelineProperties()
    [pscustomobject]@{ app = $s.SourceAppUserModelId; title = $p.Title; artist = $p.Artist
        status = $s.GetPlaybackInfo().PlaybackStatus.ToString()
        position_s = [int]$t.Position.TotalSeconds; length_s = [int]$t.EndTime.TotalSeconds }
})
ConvertTo-Json -InputObject $out -Compress
"""

CONTROL_SCRIPT = _PRELUDE + r"""
$s = $mgr.GetCurrentSession()
if ($null -eq $s) { 'none' } else { [void](Await ($s.{action}Async()) ([bool])); 'ok' }
"""

ACTIONS = {"play": "TryPlay", "pause": "TryPause", "toggle": "TryTogglePlayPause", "next": "TrySkipNext",
           "previous": "TrySkipPrevious"}


async def _powershell(script: str, timeout_s: float = 20) -> str:
    # -EncodedCommand (UTF-16LE base64): PowerShell drops multi-line scripts piped to stdin.
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    proc = await asyncio.create_subprocess_exec(
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    proctree.track(getattr(proc, "pid", None))  # ended with its children if the watchdog stops this call
    out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
    return out.decode("utf-8", "replace").strip()


def describe(sessions: list[dict[str, Any]]) -> str:
    if not sessions:
        return "Nothing is playing (no app is showing media controls)."
    rows = []
    for s in sessions:
        # Browsers send the position to Windows only now and then: 0:00 while Playing means
        # "just started / not reported yet", not "stuck".
        pos = f" at {s['position_s'] // 60}:{s['position_s'] % 60:02d}" if s.get("position_s") else (
            " (position not reported yet)" if s.get("status") == "Playing" else "")
        length = f" of {s['length_s'] // 60}:{s['length_s'] % 60:02d}" if s.get("length_s") else ""
        by = f" by {s['artist']}" if s.get("artist") else ""
        rows.append(f"{s['status'].upper()}: \"{s['title']}\"{by}{pos}{length} (in {s['app']})")
    return "\n".join(rows)


class NoArgs(BaseModel):
    pass


class MediaStatus(Tool[NoArgs]):
    name = "media_status"
    description = ("What song/video is playing on this PC and whether it is Playing or Paused, from "
                   "Windows' own media controls (YouTube in the browser, Spotify, VLC...). Exact and "
                   "instant: use it to prove playback instead of screenshots of the player.")
    risk = Risk.READ
    Args = NoArgs

    async def run(self, args: NoArgs) -> ToolResult:
        try:
            raw = await _powershell(STATUS_SCRIPT)
            data = json.loads(raw or "[]")
        except (OSError, TimeoutError, ValueError) as e:
            return ToolResult(False, f"could not read Windows media status: {e}")
        sessions = data if isinstance(data, list) else [data]
        playing = [s for s in sessions if s.get("status") == "Playing"]
        return ToolResult(True, describe(sessions), {"media": sessions, "playing": [s["title"] for s in playing],
                                                     "verified": True})  # a read: what it says is the answer


class ControlArgs(BaseModel):
    action: str = Field(description="play, pause, toggle, next or previous")


class MediaControl(Tool[ControlArgs]):
    name = "media_control"
    description = ("Play, pause, skip to next/previous on whatever is playing (same as the keyboard "
                   "media keys). Works without focusing the player window.")
    risk = Risk.ACT
    Args = ControlArgs

    async def run(self, args: ControlArgs) -> ToolResult:
        method = ACTIONS.get(args.action.strip().lower())
        if method is None:
            return ToolResult(False, f"unknown action '{args.action}': use {', '.join(ACTIONS)}")
        try:
            out = await _powershell(CONTROL_SCRIPT.replace("{action}", method))
            await asyncio.sleep(0.8)  # let the player react, then report the new state
            status = await MediaStatus().run(NoArgs())
        except (OSError, TimeoutError) as e:
            return ToolResult(False, f"could not control media: {e}")
        if out == "none":
            return ToolResult(False, "Nothing is playing, so there is nothing to control.")
        states = {s.get("status") for s in status.evidence.get("media", [])}
        want = "Paused" if args.action.lower() == "pause" else "Playing"
        if args.action.lower() == "toggle":
            want = next(iter(states), "")
        return ToolResult(True, f"{args.action}: done.\nNow: {status.output}",
                          {**status.evidence, "verified": want in states})


# --- play_youtube: the whole "play X on YouTube" job without the AI driving a browser ------------
# An agent doing this by clicking costs ~14 AI calls and ~25k tokens (screenshots, ads, checks).
# Here: one HTTPS search, open the video in the user's own browser, wait out any ad, and prove
# playback from Windows' media status. Zero AI tokens, ~5-30 s depending on the ad.

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")


def youtube_search(query: str, limit: int = 5) -> list[dict[str, str]]:
    """Top video results (no Shorts, no ads, no channels) for a YouTube search."""
    import httpx

    r = httpx.get("https://www.youtube.com/results", params={"search_query": query},
                  headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                  cookies={"CONSENT": "YES+1"}, timeout=12, follow_redirects=True)
    r.raise_for_status()
    return parse_results(r.text, limit)


def parse_results(html: str, limit: int = 5) -> list[dict[str, str]]:
    start = html.find("ytInitialData = ")
    if start == -1:
        return []
    data, _ = json.JSONDecoder().raw_decode(html, start + len("ytInitialData = "))
    found: list[dict[str, str]] = []

    def walk(node: Any) -> None:
        if len(found) >= limit:
            return
        if isinstance(node, dict):
            v = node.get("videoRenderer")
            if isinstance(v, dict) and v.get("videoId"):
                runs = (v.get("title") or {}).get("runs") or [{}]
                found.append({"id": v["videoId"], "title": "".join(x.get("text", "") for x in runs),
                              "channel": ((v.get("ownerText") or {}).get("runs") or [{}])[0].get("text", ""),
                              "length": (v.get("lengthText") or {}).get("simpleText", "")})
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return found


def press_skip_ad() -> bool:
    """Press YouTube's 'Skip' ad button through Windows accessibility (Chrome/Edge expose page
    buttons by name). No screenshot, no AI, no mouse movement. False when there is none yet."""
    try:
        import pythoncom
        from pywinauto import Desktop

        pythoncom.CoInitialize()  # UI Automation from a worker thread
        for w in Desktop(backend="uia").windows():
            if "youtube" not in w.window_text().lower():
                continue
            for el in w.descendants(control_type="Button"):
                name = (el.element_info.name or "").strip().lower()
                if name.startswith("skip") and el.is_visible():
                    try:
                        el.invoke()
                    except Exception:
                        el.click_input()
                    return True
    except Exception:  # no desktop extras, window closed mid-search: just keep waiting
        return False
    return False


def _same_video(media_title: str, video_title: str) -> bool:
    a, b = media_title.lower(), video_title.lower()
    return bool(a) and (a[:25] in b or b[:25] in a)


class PlayArgs(BaseModel):
    query: str = Field(description="What to play, e.g. 'Kaanunna Kalyanam Sita Ramam song'")
    pick: int = Field(default=1, ge=1, le=5, description="Which search result to play (1 = top)")


class PlayYouTube(Tool[PlayArgs]):
    name = "play_youtube"
    description = ("Play a song/video on YouTube in the user's own browser, in ONE step: searches YouTube, "
                   "opens the top video, waits through any ad and confirms it is PLAYING via Windows' "
                   "media status. Use it for any 'play <song/video> on YouTube' request instead of "
                   "clicking through the website.")
    risk = Risk.ACT
    Args = PlayArgs

    def domains(self, args: PlayArgs) -> list[str]:
        return ["https://www.youtube.com"]

    def summary(self, args: PlayArgs) -> str:
        return f"play '{args.query}' on YouTube"

    async def run(self, args: PlayArgs) -> ToolResult:
        import os

        try:
            results = await asyncio.to_thread(youtube_search, args.query)
        except Exception as e:  # network down, YouTube changed its page
            return ToolResult(False, f"YouTube search failed ({type(e).__name__}: {str(e)[:120]}); "
                                     "open the site and play it by clicking instead.")
        if len(results) < args.pick:
            return ToolResult(False, f"YouTube found no video for '{args.query}'.")
        video = results[args.pick - 1]
        url = f"https://www.youtube.com/watch?v={video['id']}"
        await asyncio.to_thread(os.startfile, url)  # the user's default browser, signed in as usual
        evidence: dict[str, Any] = {"url": url, "video": video, "results": results}
        # Poll Windows' media status: ads show their own title first, then the video. Ads are handled
        # here too, without AI: a paused player gets Play, and a "Skip" button is pressed by its
        # accessible name (no screenshot, no mouse).
        deadline, last, skipped = asyncio.get_running_loop().time() + 60, "nothing yet", 0
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(2)
            status = await MediaStatus().run(NoArgs())
            sessions = status.evidence.get("media", [])
            mine = [s for s in sessions if _same_video(s.get("title", ""), video["title"])]
            if mine and mine[0].get("status") == "Playing":
                evidence.update(media=sessions, verified=True, ads_skipped=skipped)
                ads = f" Skipped {skipped} ad(s)." if skipped else ""
                return ToolResult(True, f"PLAYING on YouTube: \"{video['title']}\" by {video['channel']} "
                                        f"({video['length']}). Confirmed by Windows media status.{ads}", evidence)
            last = status.output
            if any(s.get("status") == "Paused" for s in sessions):
                await _powershell(CONTROL_SCRIPT.replace("{action}", "TryPlay"))
            if sessions and not mine and await asyncio.to_thread(press_skip_ad):
                skipped += 1
        evidence["media_last"] = last
        return ToolResult(False, f"Opened \"{video['title']}\" ({url}) but Windows does not report it playing "
                                 f"yet (last: {last}). An ad may still be running or autoplay is blocked: "
                                 "look at the browser to skip it or press play.", evidence)


def media_tools() -> list[Tool]:
    return [MediaStatus(), MediaControl(), PlayYouTube()] if sys.platform == "win32" else []
