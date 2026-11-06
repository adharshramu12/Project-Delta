"""Agent tools for perception: look_with_marks, click_mark, read_screen_text, click_text.

look_with_marks  a picture of the page or window with numbered boxes on clickable elements, sent with a
                 compact legend to the vision model; it answers with a mark number.
click_mark       clicks that mark through its handle (a page ref, a UI Automation element) or, only when it
                 has none, at the centre of its box. Resolved by the ToolExecutor into browser_click,
                 click_element or click_at, so every safety check judges the real action.
read_screen_text Windows OCR of a window as text lines with screen points: for apps with no accessibility
                 tree, so a model that cannot see pictures can still act.
click_text       clicks visible text found by OCR (resolved into click_at, aimed at the text's centre).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from delta.config import PerceptionConfig
from delta.core.state import CURRENT_REQUEST
from delta.llm import usage
from delta.llm.schemas import LLMError
from delta.perception import images
from delta.perception.cache import PerceptionCache
from delta.perception.marks import BOOK, Capture, mark_up, parse_answer
from delta.perception.ocr import WindowsOcr, find_text
from delta.perception.sources import (
    browser_capture,
    desktop_capture,
    ocr_candidates,
    ocr_listing,
)
from delta.system.logging import log
from delta.tools.base import Risk, Tool, ToolResult
from delta.tools.desktop import click_risk

CaptureFn = Callable[[str, str | None], Awaitable[Capture]]
MIN_UIA_MARKS = 2  # fewer clickable UI Automation elements than this: the app draws its own UI, try OCR

MARKS_PROMPT = """{question}

The picture shows {where} with numbered boxes on its clickable elements. Legend (number, kind, name):
{legend}

Reply with MARK <number> for the element that answers the question (the one to click or look at), then one
short sentence. If none fits, reply MARK NONE and say what you see instead."""


def task_id_now() -> str:
    req = CURRENT_REQUEST.get()
    return req.id if req is not None else ""


class Perception:
    """What the perception tools share: settings, the OCR engine, the cache, how to capture, and the router
    (whose meter counts pictures, marks and OCR calls)."""

    def __init__(self, cfg: PerceptionConfig, router: Any, ocr: WindowsOcr | None = None,
                 cache: PerceptionCache | None = None, hub: Any = None, ctl: Any = None, proof: Any = None,
                 governor: Any = None) -> None:
        self.cfg = cfg
        self.router = router
        self.ocr = ocr or WindowsOcr(cfg.ocr)
        self.cache = cache or PerceptionCache(cfg.cache)
        self.hub = hub        # BrowserHub (delta.tools.browser_switch): is Delta's own browser in use?
        self.ctl = ctl        # DesktopController: UI Automation calls run on its thread
        self.proof = proof
        self.governor = governor
        self.capture_override: CaptureFn | None = None  # tests and scripted evals only

    def can_see(self) -> bool:
        try:
            return bool(self.router.candidates("vision"))
        except Exception:
            return False

    def private_page(self) -> Any:
        """Delta's own browser manager when this task uses it and it has a page open, else None."""
        hub = self.hub
        if hub is None:
            return None
        mgr = getattr(hub, "mgr", None)
        kind = hub.kind_now() if callable(getattr(hub, "kind_now", None)) else None
        if mgr is None or kind != "private":
            return None
        return mgr if str(getattr(mgr, "current_url", "")).startswith(("http", "file")) else None

    async def _on_ui_thread(self, fn: Callable[..., Any], *args: Any) -> Any:
        if self.ctl is not None:
            return await self.ctl.call(fn, *args)
        return await asyncio.to_thread(fn, *args)

    async def capture(self, where: str, window: str | None) -> Capture:
        if self.capture_override is not None:
            return await self.capture_override(where, window)
        mgr = self.private_page() if where in ("auto", "browser") and not window else None
        if where == "browser" and mgr is None:
            raise LookupError("Delta's own browser has no page open in this task; use where='window'")
        if mgr is not None:
            return await browser_capture(mgr)
        if sys.platform != "win32":
            raise LookupError("looking at desktop windows needs Windows")
        cap: Capture = await self._on_ui_thread(desktop_capture, window)
        return cap

    async def with_ocr(self, cap: Capture) -> Capture:
        """A desktop capture whose UI Automation tree is (almost) empty gets OCR text elements instead."""
        if cap.source != "uia" or len(cap.candidates) >= MIN_UIA_MARKS or not self.ocr.available():
            return cap
        lines = await self.ocr.read(cap.image)
        usage.bump(self.router, "ocr_calls")
        if lines:
            cap.candidates, cap.source = ocr_candidates(lines), "ocr"
        return cap


class LookMarksArgs(BaseModel):
    question: str = Field(description="What to find, e.g. 'which element saves the file?'")
    window: str | None = Field(default=None, description="Part of a window title; default: Delta's browser page "
                                                         "if one is open, else the active window")
    where: Literal["auto", "browser", "window"] = "auto"


class LookWithMarks(Tool[LookMarksArgs]):
    name = "look_with_marks"
    description = ("Picture of the page or window with numbered boxes on its clickable elements, checked by a "
                   "vision model that answers with a mark number. Then click it with click_mark. Use when the "
                   "page listing or read_window cannot find the control.")
    risk = Risk.READ
    Args = LookMarksArgs

    def __init__(self, p: Perception) -> None:
        self.p = p

    def available(self) -> bool:
        return self.p.cfg.marks and self.p.can_see()

    async def run(self, args: LookMarksArgs) -> ToolResult:
        from delta.system.hardware_guard import Health

        if self.p.governor is not None and self.p.governor.health == Health.CRITICAL:
            return ToolResult(False, "screen vision paused to protect the laptop (hardware critical)")
        task_id = task_id_now()
        try:
            cap = await self.p.capture(args.where, args.window)
            cap = await self.p.with_ocr(cap)
        except LookupError as e:
            return ToolResult(False, str(e))
        except Exception as e:  # no display, locked screen, a browser that went away
            log.info("perception.capture_failed", error=str(e))
            return ToolResult(False, f"could not capture the screen ({type(e).__name__}); try read_window or "
                                     "browser_read")
        if not cap.candidates:
            return ToolResult(False, "found no clickable elements to mark here; use look_at_screen and click_at, "
                                     "or read_window", {"marks": 0, "source": cap.source})
        cfg = self.p.cfg
        picture, marks = mark_up(cap, cfg.max_side, cfg.max_marks)
        legend = marks.legend()
        view_key = f"{images.digest(picture)}|{images.text_digest(legend)}|{args.question.strip().lower()}"
        where = (f"the page '{cap.window}'" if cap.source == "dom" else
                 f"the window '{cap.window}'" if cap.window else "the screen")
        BOOK.put(task_id, marks)
        evidence: dict[str, Any] = {"marks": len(marks.marks), "source": cap.source, "window": cap.window,
                                    "picture": list(picture.size)}
        if (cached := self.p.cache.answer(task_id, view_key)) is not None:
            usage.bump(self.p.router, "perception_cache_hits")
            return ToolResult(True, cached + "\n(the view has not changed: same answer, no picture sent)",
                              {**evidence, "cache_hit": True, "mark": parse_answer(cached)})
        b64 = images.encode_jpeg(picture, cfg.jpeg_quality)
        tokens = images.image_tokens(*picture.size)
        try:
            resp = await self.p.router.complete([{"role": "user", "content": [
                {"type": "text", "text": MARKS_PROMPT.format(question=args.question, where=where, legend=legend)},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}], task="vision")
        except LLMError as e:
            return ToolResult(False, ("No AI model that can see images answered, so the marked picture could not be "
                                      f"checked. Use read_window or browser_read instead. ({str(e)[:120]})"),
                              {**evidence, "vision_available": False})
        usage.bump(self.p.router, "images_sent")
        usage.bump(self.p.router, "image_tokens_est", tokens)
        usage.bump(self.p.router, "marks_used", len(marks.marks))
        n = parse_answer(resp.content)
        answer = (resp.content or "").strip()[:600]
        if n is not None and n in marks.marks:
            text = (f"{answer}\n\nMark {n} is {marks.marks[n].line().split(' ', 1)[1]}. "
                    f"Click it with click_mark mark={n}.")
        else:
            n = None
            text = f"{answer}\n\nNo mark was chosen. Marks:\n{legend}"
        self.p.cache.keep_answer(task_id, view_key, text)
        if self.p.proof is not None and (saved := self.p.proof.save_image(b64, f"marks {cap.window or 'screen'}")):
            evidence["proof_image"] = saved
        return ToolResult(True, text, {**evidence, "mark": n, "image_tokens_est": tokens,
                                       "vision_provider": resp.provider})


class ClickMarkArgs(BaseModel):
    mark: int = Field(description="The mark number from the latest look_with_marks")


class ClickMark(Tool[ClickMarkArgs]):
    name = "click_mark"
    description = ("Click the element with this mark number from the latest look_with_marks (by its element "
                   "handle; asks first for Send, Buy, Delete and the like, as any click does).")
    risk = Risk.ACT
    Args = ClickMarkArgs
    is_alias = True  # the ToolExecutor resolves it (resolve_target) into the real click and checks that

    def __init__(self, p: Perception) -> None:
        self.p = p

    def available(self) -> bool:
        return self.p.cfg.marks and self.p.can_see()

    def risk_for(self, args: ClickMarkArgs) -> Risk:
        """The risk of the click the mark stands for (Send, Pay, Delete...), so self-heal and the task engine
        judge a click_mark like the click itself. The executor checks the real call again after resolving."""
        marks = BOOK.get(task_id_now())
        mark = marks.marks.get(args.mark) if marks is not None else None
        return click_risk(mark.label) if mark is not None else Risk.ACT

    def summary(self, args: ClickMarkArgs) -> str:
        marks = BOOK.get(task_id_now())
        mark = marks.marks.get(args.mark) if marks is not None else None
        return f"click mark {args.mark}" + (f" ('{mark.label[:60]}')" if mark is not None and mark.label else "")

    async def resolve_target(self, args: ClickMarkArgs) -> tuple[str, dict[str, Any]] | str:
        task_id = task_id_now()
        marks = BOOK.get(task_id)
        if marks is None:
            return "no marked picture in this task yet: call look_with_marks first"
        return marks.resolve(args.mark, task_id)

    async def run(self, args: ClickMarkArgs) -> ToolResult:
        return ToolResult(False, "click_mark runs through Delta's tool executor only")


class ScreenTextArgs(BaseModel):
    window: str | None = Field(default=None, description="Part of a window title; default: the active window")
    focus: str | None = Field(default=None, description="Only lines containing this text")


async def _ocr_window(p: Perception, window: str | None) -> tuple[Capture, list[Any]]:
    if p.capture_override is not None:
        cap = await p.capture_override("window", window)
    else:
        if sys.platform != "win32":
            raise LookupError("reading the screen with OCR needs Windows")
        cap = await p._on_ui_thread(_image_only, window)
    lines = await p.ocr.read(cap.image)
    usage.bump(p.router, "ocr_calls")
    return cap, lines


def _image_only(window: str | None) -> Capture:
    from delta.perception.sources import _window
    from delta.tools.screen import capture_window_image

    hwnd, name = _window(window)
    img, origin = capture_window_image(hwnd)
    return Capture(img, [], "ocr", name, origin=origin)


class ReadScreenText(Tool[ScreenTextArgs]):
    name = "read_screen_text"
    description = ("Read the text of a window with Windows' built-in OCR, with the screen point of each line. "
                   "For apps where read_window shows no controls. Click a line with click_text.")
    risk = Risk.READ
    Args = ScreenTextArgs

    def __init__(self, p: Perception) -> None:
        self.p = p

    def available(self) -> bool:
        return self.p.ocr.available()

    async def run(self, args: ScreenTextArgs) -> ToolResult:
        try:
            cap, lines = await _ocr_window(self.p, args.window)
        except LookupError as e:
            return ToolResult(False, str(e))
        except Exception as e:
            log.info("perception.ocr_failed", error=str(e))
            return ToolResult(False, f"could not read the screen ({type(e).__name__})")
        if not lines and not self.p.ocr.available():
            return ToolResult(False, "Windows OCR is not available on this PC (no OCR language installed); "
                                     "use read_window or look_at_screen", {"ocr_available": False})
        if args.focus:
            lines = [ln for ln in lines if args.focus.lower() in ln.text.lower()]
        body = ocr_listing(lines, cap.origin, cap.scale) or "(no text found)"
        return ToolResult(True, f"WINDOW TEXT (OCR): {cap.window}\n{body}", {"window": cap.window, "lines": len(lines)})


class ClickTextArgs(BaseModel):
    text: str = Field(description="The visible text to click, as read_screen_text shows it")
    window: str | None = None


class ClickText(Tool[ClickTextArgs]):
    name = "click_text"
    description = "Click visible text found by OCR (apps with no readable controls). Asks first for Send, Buy, Delete."
    risk = Risk.ACT
    Args = ClickTextArgs
    is_alias = True  # the ToolExecutor resolves it (resolve_target) into a click_at and checks that

    def __init__(self, p: Perception) -> None:
        self.p = p

    def available(self) -> bool:
        return self.p.ocr.available()

    def risk_for(self, args: ClickTextArgs) -> Risk:
        return click_risk(args.text)

    def summary(self, args: ClickTextArgs) -> str:
        return f"click the text '{args.text}'" + (f" in {args.window}" if args.window else "")

    async def resolve_target(self, args: ClickTextArgs) -> tuple[str, dict[str, Any]] | str:
        cap, lines = await _ocr_window(self.p, args.window)
        if not lines and not self.p.ocr.available():
            return "Windows OCR is not available on this PC; use click_element or look_at_screen"
        found = find_text(lines, args.text)
        if found is None:
            return f"no text '{args.text}' found on the screen by OCR; call read_screen_text to see what is there"
        seen, (x, y, w, h) = found
        sx = round(cap.origin[0] + (x + w / 2) * cap.scale)
        sy = round(cap.origin[1] + (y + h / 2) * cap.scale)
        expect = seen if args.text.lower() in seen.lower() else f"{args.text} {seen}"
        return "click_at", {"x": sx, "y": sy, "expect": expect}

    async def run(self, args: ClickTextArgs) -> ToolResult:
        return ToolResult(False, "click_text runs through Delta's tool executor only")


def perception_tools(p: Perception) -> list[Tool]:
    return [LookWithMarks(p), ClickMark(p), ReadScreenText(p), ClickText(p)]
