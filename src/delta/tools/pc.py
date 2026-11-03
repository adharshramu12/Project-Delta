"""PC controls as agent tools, so plain words and voice work too: "turn the volume down", "make the
screen brighter", "who is using my laptop?" (camera_photo, then send_file), "show me the screen".
The Telegram /volume /brightness /apps /screen /camera commands use the same functions (pc_control)."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from delta.core.state import CURRENT_REQUEST
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


class _Picture(Tool[NoArgs]):
    """Takes a picture; for a user writing from their phone, sends it straight into their chat (through
    send_file, so the PIN check applies), because they cannot look at this PC's screen."""

    Args = NoArgs
    deliver: Any = None  # the SendFile tool, set at start-up
    caption = ""

    async def _take(self) -> Any:
        raise NotImplementedError

    async def run(self, args: NoArgs) -> ToolResult:
        try:
            path = await self._take()
        except PCControlError as e:
            return ToolResult(False, str(e))
        req = CURRENT_REQUEST.get()
        if self.deliver is not None and req is not None and req.channel in ("telegram", "slack"):
            from delta.tools.deliver import SendFileArgs

            sent = await self.deliver.run(SendFileArgs(path=str(path), caption=self.caption))
            path.unlink(missing_ok=True)  # it is in their chat now; nothing stays on the PC
            return sent
        return ToolResult(True, f"saved {path}", {"path": str(path)})


class TakeScreenshot(_Picture):
    name = "take_screenshot"
    description = ("Take a screenshot of the whole PC screen. A user on their phone gets it in their chat "
                   "automatically; otherwise it returns the file's path.")
    risk = Risk.READ
    caption = "Screen right now"

    async def _take(self) -> Any:
        return await asyncio.to_thread(pc.screenshot)


class CameraPhoto(_Picture):
    name = "camera_photo"
    description = ("Take one photo with the PC's webcam (its light comes on). Only when the user asks who is at "
                   "the PC or for a camera photo. A user on their phone gets it in their chat automatically.")
    risk = Risk.ACT
    caption = "Camera right now"

    async def _take(self) -> Any:
        return await asyncio.to_thread(pc.camera_photo)


def pc_tools(deliver: Any = None) -> list[Tool]:
    shot, cam = TakeScreenshot(), CameraPhoto()
    shot.deliver = cam.deliver = deliver
    return [SetVolume(), SetBrightness(), shot, cam]
