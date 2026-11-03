"""PC controls as agent tools, so plain words and voice work too: "turn the volume down", "make the
screen brighter", "who is using my laptop?" (camera_photo, then send_file), "show me the screen".
The Telegram /volume /brightness /apps /screen /camera commands use the same functions (pc_control)."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from delta.system import pc_control as pc
from delta.system.pc_control import PCControlError
from delta.tools.base import Risk, Tool, ToolResult


class LevelArgs(BaseModel):
    level: int | None = Field(default=None, ge=0, le=100, description="Exact level 0-100")
    change: int = Field(default=0, ge=-100, le=100, description="Or move by this much, e.g. -20 or +10")
    mute: bool | None = Field(default=None, description="Volume only: true mutes, false unmutes")


class SetVolume(Tool[LevelArgs]):
    name = "set_volume"
    description = "Set the PC's volume (exact level or louder/quieter) or mute/unmute it. Returns the new level."
    risk = Risk.ACT
    Args = LevelArgs

    async def run(self, args: LevelArgs) -> ToolResult:
        try:
            level, muted = await asyncio.to_thread(pc.set_volume, args.level, args.change, args.mute)
        except PCControlError as e:
            return ToolResult(False, str(e))
        return ToolResult(True, f"volume {level}%{' (muted)' if muted else ''}",
                          {"volume": level, "muted": muted, "verified": True})


class SetBrightness(Tool[LevelArgs]):
    name = "set_brightness"
    description = "Set the screen brightness (exact level or brighter/dimmer). Laptop screens only."
    risk = Risk.ACT
    Args = LevelArgs

    async def run(self, args: LevelArgs) -> ToolResult:
        try:
            level = await asyncio.to_thread(pc.set_brightness, args.level, args.change)
        except PCControlError as e:
            return ToolResult(False, str(e))
        return ToolResult(True, f"brightness {level}%", {"brightness": level, "verified": True})


class NoArgs(BaseModel):
    pass


class TakeScreenshot(Tool[NoArgs]):
    name = "take_screenshot"
    description = ("Save a screenshot of the whole PC screen and return its path; send_file it to a user who "
                   "asked from their phone.")
    risk = Risk.READ
    Args = NoArgs

    async def run(self, args: NoArgs) -> ToolResult:
        path = await asyncio.to_thread(pc.screenshot)
        return ToolResult(True, f"saved {path}", {"path": str(path)})


class CameraPhoto(Tool[NoArgs]):
    name = "camera_photo"
    description = ("Take one photo with the PC's webcam (its light comes on) and return its path; send_file it "
                   "to the user. Only when the user asks who is at the PC or for a camera photo.")
    risk = Risk.ACT
    Args = NoArgs

    async def run(self, args: NoArgs) -> ToolResult:
        try:
            path = await asyncio.to_thread(pc.camera_photo)
        except PCControlError as e:
            return ToolResult(False, str(e))
        return ToolResult(True, f"saved {path}", {"path": str(path)})


def pc_tools() -> list[Tool]:
    return [SetVolume(), SetBrightness(), TakeScreenshot(), CameraPhoto()]
