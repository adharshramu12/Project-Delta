"""Where set-of-marks pictures and their clickable elements come from.

    browser  Delta's own browser (Playwright): a viewport screenshot, and the page's numbered elements with
             their boxes. Each mark is the element's ref, the same number browser_click takes.
    desktop  a window (PrintWindow, works even when covered) and its Windows UI Automation tree: each mark
             is the UI Automation element itself.
    ocr      the same window read by Windows OCR, for apps whose UI Automation tree is empty: marks have no
             handle, only a box, so a click on them is a coordinate click aimed at the box's centre.

The user's own browser is a desktop window here: Delta reads it through UI Automation like any app.
"""

from __future__ import annotations

from typing import Any

from delta.perception.marks import Candidate, Capture
from delta.perception.ocr import OcrLine

# UI Automation control types a person would click or type into.
CLICKABLE = {"Button", "MenuItem", "Hyperlink", "Edit", "ComboBox", "CheckBox", "RadioButton", "TabItem",
             "ListItem", "TreeItem", "SplitButton", "MenuBar", "Document", "DataItem", "Slider", "Spinner"}
MAX_UIA_NODES = 600  # descendants looked at per window: a huge list view must not stall the look

BOXES_JS = """() => Array.from(document.querySelectorAll('[data-delta-ref]')).map((el) => {
  const r = el.getBoundingClientRect();
  return {ref: Number(el.getAttribute('data-delta-ref')), x: r.left, y: r.top, w: r.width, h: r.height};
})"""


async def browser_capture(mgr: Any) -> Capture:
    """The page in Delta's browser, with a candidate per numbered element (refs stay as the agent knows them)."""
    import io

    from PIL import Image

    from delta.tools.browser import _evidence, list_refs

    page = await mgr.page()
    elements = await list_refs(page, mgr)
    boxes = {int(b["ref"]): b for b in await page.evaluate(BOXES_JS) if b.get("ref")}
    ratio = float(await page.evaluate("() => window.devicePixelRatio || 1") or 1)
    png = await page.screenshot(type="png")
    img = Image.open(io.BytesIO(png))
    img.load()
    ev = await _evidence(page, mgr)
    out: list[Candidate] = []
    for e in elements:
        b = boxes.get(e.ref)
        if not b:
            continue
        box = (round(b["x"] * ratio), round(b["y"] * ratio), round(b["w"] * ratio), round(b["h"] * ratio))
        out.append(Candidate(e.name, e.role, box, "dom", e.ref, e.in_view))
    return Capture(img, out, "dom", str(ev.get("title") or ""), str(ev.get("url") or ""))


def _window(title: str | None) -> tuple[int, str]:
    import win32gui

    from delta.tools.screen import find_window_handle

    hwnd = find_window_handle(title) if title else win32gui.GetForegroundWindow()
    if not hwnd:
        raise LookupError(f"no visible window titled like '{title}' (see list_windows)")
    return int(hwnd), str(win32gui.GetWindowText(hwnd))


def uia_candidates(wrapper: Any, origin: tuple[int, int], limit: int = MAX_UIA_NODES) -> list[Candidate]:
    """Clickable UI Automation elements of a window, boxes relative to the window's top-left."""
    out: list[Candidate] = []
    for i, el in enumerate(wrapper.descendants()):
        if i >= limit:
            break
        try:
            info = el.element_info
            kind = str(info.control_type or "")
            if kind not in CLICKABLE or not el.is_visible():
                continue
            r = el.rectangle()
            w, h = int(r.right - r.left), int(r.bottom - r.top)
            if w <= 0 or h <= 0:
                continue
            out.append(Candidate(str(info.name or "").strip(), kind, (int(r.left) - origin[0], int(r.top) - origin[1],
                                                                       w, h), "uia", el))
        except Exception:  # an element that vanished while we walked: skip it
            continue
    return out


def desktop_capture(title: str | None) -> Capture:
    """A window (the active one when no title) and its UI Automation elements. Windows only; blocking."""
    from pywinauto import Desktop

    from delta.tools.screen import capture_window_image

    hwnd, name = _window(title)
    img, origin = capture_window_image(hwnd)
    try:
        wrapper = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
        candidates = uia_candidates(wrapper, origin)
    except Exception:  # no accessibility tree at all: OCR may still read it
        candidates = []
    return Capture(img, candidates, "uia", name, origin=origin)


def ocr_candidates(lines: list[OcrLine]) -> list[Candidate]:
    """OCR lines as text elements (no handle: a click goes to the centre of the box)."""
    return [Candidate(line.text, "text", line.box, "xy") for line in lines]


def ocr_listing(lines: list[OcrLine], origin: tuple[int, int] = (0, 0), scale: float = 1.0, limit: int = 120) -> str:
    """The OCR text for a model that cannot see: one line per text line with the screen point at its centre."""
    rows = []
    for line in lines[:limit]:
        x, y, w, h = line.box
        cx, cy = round(origin[0] + (x + w / 2) * scale), round(origin[1] + (y + h / 2) * scale)
        rows.append(f'"{line.text[:80]}" at ({cx},{cy})')
    more = len(lines) - len(rows)
    if more > 0:
        rows.append(f"…[{more} more lines]")
    return "\n".join(rows)
