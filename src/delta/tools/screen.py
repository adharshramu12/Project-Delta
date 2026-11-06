"""Seeing the screen. Expensive compared with read_window, so it is the fallback:
the screenshot is downscaled (smaller when the laptop is under strain), JPEG-compressed and
sent to a vision model. During a task it is also kept as proof of what Delta saw (Delta's data
folder, runs/<task id>/), so the user can check the run afterwards."""

from __future__ import annotations

import asyncio
import base64
import io
from typing import Any

from pydantic import BaseModel, Field

from delta.core.proof import ProofRecorder
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

    from delta.system.agent_cursor import capture_guard

    with capture_guard(), mss.mss() as sct:  # the in-control ring stays out of what Delta sees
        mon = sct.monitors[1]
        shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    _mask_screen(img, (int(mon.get("left", 0)), int(mon.get("top", 0))))
    screen = img.size
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70, optimize=True)
    return base64.b64encode(buf.getvalue()).decode(), screen, img.size


def _mask_screen(img: Any, origin: tuple[int, int]) -> None:
    """Paint over password boxes Delta filled in the user's own browser (delta.logins.mask)."""
    from delta.logins import mask

    if mask.active():
        mask.mask_screen(img, origin)


def find_window_handle(title: str) -> int | None:
    """Visible top-level window whose title contains `title` (case-insensitive)."""
    import win32gui

    want, found = title.lower(), []

    def visit(hwnd: int, _: object) -> None:
        if win32gui.IsWindowVisible(hwnd) and want in win32gui.GetWindowText(hwnd).lower():
            found.append(hwnd)

    win32gui.EnumWindows(visit, None)
    return found[0] if found else None


def capture_window_image(hwnd: int) -> tuple[Any, tuple[int, int]]:
    """One window -> (PIL picture at full size, window's top-left on screen).

    PrintWindow asks the app to draw itself, so this works even when other windows cover it:
    Delta can look at Notepad while the user keeps working in front of it."""
    import ctypes

    import win32gui
    import win32ui
    from PIL import Image

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top
    hdc = win32gui.GetWindowDC(hwnd)
    src = win32ui.CreateDCFromHandle(hdc)
    mem = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    try:
        bmp.CreateCompatibleBitmap(src, w, h)
        mem.SelectObject(bmp)
        ctypes.windll.user32.PrintWindow(hwnd, mem.GetSafeHdc(), 2)  # 2 = PW_RENDERFULLCONTENT
        img = Image.frombuffer("RGB", (w, h), bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        mem.DeleteDC()
        src.DeleteDC()
        win32gui.ReleaseDC(hwnd, hdc)
    from delta.logins import mask

    if mask.active():
        mask.mask_window(img, hwnd)  # password boxes Delta filled in the user's own browser
    return img, (left, top)


def capture_screen_image() -> Any:
    """The primary monitor as a PIL picture at full size."""
    import mss
    from PIL import Image

    from delta.system.agent_cursor import capture_guard

    with capture_guard(), mss.mss() as sct:  # the in-control ring stays out of what Delta sees
        mon = sct.monitors[1]
        shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    _mask_screen(img, (int(mon.get("left", 0)), int(mon.get("top", 0))))
    return img


def capture_window_jpeg(hwnd: int, max_width: int) -> tuple[str, tuple[int, int], tuple[int, int], tuple[int, int]]:
    """One window -> (base64 jpeg, window size, image size, window's top-left on screen)."""
    from PIL import Image

    img, origin = capture_window_image(hwnd)
    size = img.size
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70, optimize=True)
    return base64.b64encode(buf.getvalue()).decode(), size, img.size, origin


class LookArgs(BaseModel):
    question: str = Field(description="What you need to know, e.g. 'where is the Save button?'")
    window: str | None = Field(default=None, description=(
        "Part of a window title to look at just that window, even if it is behind others "
        "(recommended). Default: the whole screen."))


class LookAtScreen(Tool[LookArgs]):
    name = "look_at_screen"
    description = ("Take a screenshot and ask a vision model about it: to check what an app shows, "
                   "or to find where to click_at. Pass window='Notepad' to see one window even when "
                   "it is covered. Prefer read_window when it can see what you need (cheaper).")
    risk = Risk.READ
    Args = LookArgs

    def __init__(self, router: ModelRouter, governor: Governor | None = None,
                 proof: ProofRecorder | None = None) -> None:
        self.router = router
        self.governor = governor
        self.proof = proof

    def available(self) -> bool:
        """Only offered when some model can actually see images."""
        return bool(self.router.candidates("vision"))

    async def run(self, args: LookArgs) -> ToolResult:
        health = self.governor.health if self.governor else Health.NORMAL
        if health == Health.CRITICAL:
            return ToolResult(False, "screen vision paused to protect the laptop (hardware critical)")
        width = WIDTH.get(health, 960)
        hwnd = await asyncio.to_thread(find_window_handle, args.window) if args.window else None
        if args.window and hwnd is None:
            return ToolResult(False, f"no visible window titled like '{args.window}' (see list_windows)")
        if hwnd is not None:
            b64, (sw, sh), (iw, ih), (ox, oy) = await asyncio.to_thread(capture_window_jpeg, hwnd, width)
            where = (f"The image shows only the window '{args.window}' ({sw}x{sh}, top-left at screen "
                     f"{ox},{oy}), scaled to {iw}x{ih}. Give any positions in SCREEN pixels: "
                     f"x = {ox} + image_x*{sw / iw:.3f}, y = {oy} + image_y*{sh / ih:.3f}.")
        else:
            b64, (sw, sh), (iw, ih) = await asyncio.to_thread(capture_jpeg, width)
            where = (f"The image is {iw}x{ih}, scaled from a {sw}x{sh} screen. Give any positions in SCREEN "
                     f"pixels (multiply image coords by {sw / iw:.3f}).")
        prompt = f"{args.question}\n{where} Be concise."
        try:
            resp = await self.router.complete([{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
                task="vision")
        except LLMError as e:
            return ToolResult(False, (
                "No AI model that can see images is set up, so I cannot look at the screenshot. "
                "Use list_windows / read_window (desktop apps) or browser_read (web pages) instead: they "
                "read the screen as text. To enable vision, add a free NVIDIA key in Setup, or install "
                f"Ollama with a vision model. ({str(e)[:120]})"), {"vision_available": False})
        from delta.llm.usage import bump
        from delta.perception.images import image_tokens

        bump(self.router, "images_sent")
        bump(self.router, "image_tokens_est", image_tokens(iw, ih))
        evidence: dict = {"vision_provider": resp.provider, "screen": [sw, sh], "window": args.window}
        if self.proof and (name := self.proof.save_image(b64, args.window or "screen")):
            evidence["proof_image"] = name
        return ToolResult(True, resp.content, evidence)


def screen_tools(router: ModelRouter, governor: Governor | None, proof: ProofRecorder | None = None) -> list[Tool]:
    return [LookAtScreen(router, governor, proof)]
