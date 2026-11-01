"""Seeing the screen. Expensive compared with read_window, so it is the fallback:
the screenshot is downscaled (smaller when the laptop is under strain), JPEG-compressed,
sent to a vision model, and never stored on disk."""

from __future__ import annotations

import asyncio
import base64
import io

from pydantic import BaseModel, Field

from delta.llm.router import ModelRouter
from delta.llm.schemas import LLMError
from delta.system.governor import Governor
from delta.system.hardware_guard import Health
from delta.tools.base import Risk, Tool, ToolResult

WIDTH = {Health.NORMAL: 1280, Health.THROTTLE: 960}


def capture_jpeg(max_width: int) -> tuple[str, tuple[int, int], tuple[int, int]]:
    """Primary monitor -> (base64 jpeg, screen size, image size)."""
    import mss
    from PIL import Image

    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    screen = img.size
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70, optimize=True)
    return base64.b64encode(buf.getvalue()).decode(), screen, img.size


class LookArgs(BaseModel):
    question: str = Field(description="What you need to know, e.g. 'where is the Save button?'")


class LookAtScreen(Tool[LookArgs]):
    name = "look_at_screen"
    description = ("Take a screenshot and ask a vision model about it. Use only when read_window "
                   "cannot see what you need (games, canvases, images, remote desktops).")
    risk = Risk.READ
    Args = LookArgs

    def __init__(self, router: ModelRouter, governor: Governor | None = None) -> None:
        self.router = router
        self.governor = governor

    async def run(self, args: LookArgs) -> ToolResult:
        health = self.governor.health if self.governor else Health.NORMAL
        if health == Health.CRITICAL:
            return ToolResult(False, "screen vision paused to protect the laptop (hardware critical)")
        b64, (sw, sh), (iw, ih) = await asyncio.to_thread(capture_jpeg, WIDTH.get(health, 960))
        prompt = (f"{args.question}\nThe image is {iw}x{ih}, scaled from a {sw}x{sh} screen. "
                  f"Give any positions in SCREEN pixels (multiply image coords by {sw / iw:.3f}). Be concise.")
        try:
            resp = await self.router.complete([{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
                task="vision")
        except LLMError as e:
            return ToolResult(False, f"no vision model available: {e}")
        return ToolResult(True, resp.content, {"vision_provider": resp.provider, "screen": [sw, sh]})


def screen_tools(router: ModelRouter, governor: Governor | None) -> list[Tool]:
    return [LookAtScreen(router, governor)]
