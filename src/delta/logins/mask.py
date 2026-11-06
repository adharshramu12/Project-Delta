"""Masking password boxes in pictures of the user's OWN browser.

In Delta's private browser a password box is masked with CSS before any picture is taken (signin.mask_secret_fields).
The user's own browser cannot be styled from outside, so after Delta fills a password there it remembers where the
box is, relative to its window, and every screenshot taken afterwards (delta.tools.screen: one window, or the whole
screen) has that area painted over before it is saved or sent to a model. The browser itself already shows dots; this
covers a "show password" button or a page that turns the box into plain text.

Pure Python apart from the window position lookup, so the painting is tested on any OS.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from typing import Any

PAD = 6  # pixels around the box, so a caret or a "show" icon is covered too
KEEP_S = 30 * 60  # a box is masked this long after Delta filled it (the page usually moved on long before)

Box = tuple[int, int, int, int]  # left, top, right, bottom, relative to the window's top-left corner

_lock = threading.Lock()
_boxes: dict[int, list[tuple[Box, float]]] = {}  # window handle -> boxes and when they were remembered


def remember(hwnd: int, box_on_screen: Box, window_on_screen: tuple[int, int]) -> None:
    """Remember a password box Delta filled, in window coordinates (the window may move later)."""
    left, top, right, bottom = box_on_screen
    wx, wy = window_on_screen
    rel = (left - wx - PAD, top - wy - PAD, right - wx + PAD, bottom - wy + PAD)
    with _lock:
        _boxes.setdefault(int(hwnd), []).append((rel, time.monotonic()))


def forget_all() -> None:
    with _lock:
        _boxes.clear()


def active(now: float | None = None) -> dict[int, list[Box]]:
    """The boxes still masked, per window (old ones dropped)."""
    now = time.monotonic() if now is None else now
    with _lock:
        for hwnd in list(_boxes):
            _boxes[hwnd] = [(b, t) for b, t in _boxes[hwnd] if now - t < KEEP_S]
            if not _boxes[hwnd]:
                del _boxes[hwnd]
        return {h: [b for b, _ in items] for h, items in _boxes.items()}


def paint(img: Any, boxes: list[Box], offset: tuple[int, int] = (0, 0)) -> int:
    """Paint `boxes` (shifted by `offset`) black on the PIL picture `img`. Returns how many were painted."""
    if not boxes:
        return 0
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    ox, oy = offset
    done = 0
    for left, top, right, bottom in boxes:
        x0, y0 = max(0, left + ox), max(0, top + oy)
        x1, y1 = min(img.width, right + ox), min(img.height, bottom + oy)
        if x1 > x0 and y1 > y0:
            draw.rectangle((x0, y0, x1, y1), fill=(0, 0, 0))
            done += 1
    return done


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    with contextlib.suppress(Exception):
        import win32gui

        if not win32gui.IsWindow(hwnd):
            return None
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        return int(left), int(top), int(right), int(bottom)
    return None


def mask_window(img: Any, hwnd: int, rect: Callable[[int], tuple[int, int, int, int] | None] = _window_rect) -> int:
    """A picture of one window (top-left of the picture = top-left of the window)."""
    boxes = active().get(int(hwnd), [])
    if not boxes or rect(int(hwnd)) is None:
        return 0
    return paint(img, boxes)


def mask_screen(img: Any, screen_origin: tuple[int, int] = (0, 0),
                rect: Callable[[int], tuple[int, int, int, int] | None] = _window_rect) -> int:
    """A picture of a monitor whose top-left is `screen_origin` in screen coordinates."""
    done = 0
    for hwnd, boxes in active().items():
        where = rect(hwnd)
        if where is None:
            continue
        done += paint(img, boxes, (where[0] - screen_origin[0], where[1] - screen_origin[1]))
    return done


__all__ = ["active", "forget_all", "mask_screen", "mask_window", "paint", "remember"]
