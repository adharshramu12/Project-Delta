"""The own-browser driver for saved logins: Delta's one window in the user's everyday Chrome or Edge, through Windows
UI Automation (delta.tools.user_browser.UserBrowser). Used by delta.logins.own_browser; see its docstring for the
rules. Windows only; every UI Automation call runs on the desktop controller's thread.

How the checks map onto UI Automation:
- the tab: the selected tab in Delta's window (its runtime id) and the window handle;
- the page's address: the Value of the tab's top Document element (the full URL, scheme included; the address bar
  is NOT used, because it hides "http://" and "https://");
- a frame's address: the Value of the nearest Document element above the box (frames are Documents of their own);
- a box: an Edit element; IsPassword marks password boxes; its name (label, placeholder) and its AutomationId (Chrome
  and Edge report the HTML id there) say what else it is ("loginFormCaptcha" is a captcha box);
- a captcha picture: the Image element of the same document nearest to the captcha box (or any element whose id says
  captcha, so a <canvas> captcha counts), captured from the screen (only that rectangle, with Delta's window in
  front) for Windows OCR or for the user to read. Rectangles and the capture both run per-monitor DPI aware on the
  UI Automation thread, so 125 % / 150 % display scaling and a second monitor give the right pixels. When no picture
  element can be found, the user still gets a picture: a band of the page around the captcha box with every password
  box painted over (captcha_region_image; never read by OCR or a model);
- filling: the box's ValuePattern.SetValue (straight into that box, no keyboard, nothing on the clipboard). When a
  page ignores it, Delta types the value into the box instead, a few characters at a time, checking before each
  burst that the box still has the keyboard focus in Delta's window (the user's mouse or keyboard stops it).
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from delta.logins import mask
from delta.logins.own_browser import CHALLENGE_WORDS, DriverError, FieldRef, TabBinding, TabChangedError, classify

TYPE_CHUNK = 6  # characters typed between focus checks in the keyboard fallback
MAX_NODES = 2500
LABELS = {"chrome": "Google Chrome", "edge": "Microsoft Edge"}


def _wrap(el: Any) -> Any:
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo

    return UIAWrapper(UIAElementInfo(el))


@contextlib.contextmanager
def _per_monitor_dpi() -> Any:
    """Run the enclosed UI Automation reads and the screen capture per-monitor DPI aware (physical pixels), whatever
    the process was set to, then put the thread back. A no-op where the call does not exist (before Windows 10 1607)."""
    old = None
    user32 = None
    with contextlib.suppress(Exception):
        import ctypes

        user32 = ctypes.windll.user32
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        old = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    try:
        yield
    finally:
        if old and user32 is not None:
            with contextlib.suppress(Exception):
                import ctypes

                user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))


def _grab(left: int, top: int, width: int, height: int) -> Any:
    """A PIL picture of that screen rectangle (physical pixels)."""
    import mss
    from PIL import Image

    with mss.mss() as shot:
        raw = shot.grab({"left": int(left), "top": int(top), "width": max(1, int(width)),
                         "height": max(1, int(height))})
    return Image.frombytes("RGB", raw.size, raw.rgb)


def _blank(img: Any) -> bool:
    """One flat colour: the capture missed the picture (wrong coordinates, covered window)."""
    lo, hi = img.convert("L").getextrema()
    return bool(hi - lo < 12)


def _same(a: Any, b: Any) -> bool:
    from pywinauto.uia_defines import IUIA

    with contextlib.suppress(Exception):
        return bool(IUIA().iuia.CompareElements(a, b))
    return False


class UIAOwnDriver:
    """OwnBrowserDriver over Delta's window in the user's browser (delta.tools.user_browser.UserBrowser)."""

    def __init__(self, ub: Any) -> None:
        self.ub = ub
        self.ctl = ub.ctl
        self.browser_key = ub.name or "chrome"
        self.browser_label = LABELS.get(self.browser_key, "your browser")

    # --- helpers (UIA thread) -----------------------------------------------------------------------------
    def _w(self) -> Any:
        w = self.ub._window()
        if w is None:
            raise TabChangedError("Delta's browser window was closed")
        return w

    def _top_doc(self, w: Any) -> Any | None:
        with contextlib.suppress(Exception):
            docs = list(w.descendants(control_type="Document"))
            return docs[0] if docs else None
        return None

    def _selected_tab(self, w: Any) -> str:
        from delta.tools.user_browser_actions import _in_page

        with contextlib.suppress(Exception):
            for t in w.descendants(control_type="TabItem"):
                with contextlib.suppress(Exception):
                    if t.iface_selection_item.CurrentIsSelected and not _in_page(t):  # the browser's tab strip
                        return ".".join(str(x) for x in t.element_info.runtime_id)
        return ""  # no tab strip seen: the window binding and the origin checks still hold

    def _guard(self) -> None:
        from delta.tools.desktop import UserOverrideError

        try:
            self.ctl.guard()
        except UserOverrideError as e:
            raise TabChangedError(f"you used the mouse or keyboard while Delta was signing in ({e})") from e

    # --- OwnBrowserDriver (each runs its body on the UIA thread) ---------------------------------------------
    async def ensure_window(self, url: str) -> bool:
        def go() -> bool:
            if self.ub._window() is None:
                self.ub._new_window(url)
            return self.ub._window() is not None

        try:
            with self.ctl.acting():
                return bool(await self.ctl.call(go))
        except LookupError as e:
            raise DriverError(str(e)) from e

    async def binding(self) -> TabBinding | None:
        def go() -> TabBinding | None:
            w = self.ub._window()
            if w is None:
                return None
            return TabBinding(str(self.ub.handle), self._selected_tab(w))

        return await self.ctl.call(go)

    async def top_url(self) -> str:
        def go() -> str:
            doc = self._top_doc(self._w())
            url = ""
            with contextlib.suppress(Exception):
                url = str(doc.iface_value.CurrentValue) if doc is not None else ""
            return url if "://" in url else ""  # no scheme = not trusted (http vs https unknown)

        return await self.ctl.call(go)

    async def navigate(self, url: str) -> None:
        def go() -> None:
            self._guard()
            self.ub._navigate(self._w(), url)

        try:
            with self.ctl.acting():
                await self.ctl.call(go)
        except LookupError as e:
            raise DriverError(str(e)) from e

    async def fields(self) -> list[FieldRef]:
        def go() -> list[FieldRef]:
            from pywinauto.uia_defines import IUIA

            w = self._w()
            doc = self._top_doc(w)
            if doc is None:
                return []
            u = IUIA()
            dll = u.UIA_dll
            cr = u.iuia.CreateCacheRequest()
            for prop in (dll.UIA_NamePropertyId, dll.UIA_ControlTypePropertyId, dll.UIA_IsOffscreenPropertyId,
                         dll.UIA_IsPasswordPropertyId, dll.UIA_IsEnabledPropertyId, dll.UIA_ValueValuePropertyId,
                         dll.UIA_AutomationIdPropertyId):
                cr.AddProperty(prop)
            cr.TreeScope = dll.TreeScope_Subtree
            cr.TreeFilter = u.iuia.ControlViewCondition
            top = doc.element_info.element.BuildUpdatedCache(cr)
            edit, document = dll.UIA_EditControlTypeId, dll.UIA_DocumentControlTypeId
            value_id = dll.UIA_ValueValuePropertyId

            def value(el: Any) -> str:
                with contextlib.suppress(Exception):
                    return str(el.GetCachedPropertyValue(value_id) or "")
                return ""

            out: list[FieldRef] = []
            plain: list[FieldRef] = []  # text boxes without a telling name, in page order
            stack: list[tuple[Any, str]] = [(top, value(top))]
            seen = 0
            while stack and seen < MAX_NODES:
                el, url = stack.pop()
                seen += 1
                if el.CachedControlType == document and el is not top:
                    url = value(el)
                if el.CachedControlType == edit and not el.CachedIsOffscreen and el.CachedIsEnabled:
                    name = str(el.CachedName or "")
                    auto_id = ""
                    with contextlib.suppress(Exception):
                        auto_id = str(el.CachedAutomationId or "")
                    kind = classify(name, bool(el.CachedIsPassword), auto_id)
                    ref = FieldRef(kind or "text", url, el, name[:80])
                    (out if kind else plain).append(ref)
                    if kind == "password" and not any(f.kind == "user" and f.frame_url == url for f in out):
                        before = [f for f in plain if f.frame_url == url]
                        if before:  # the text box right before the password box is the user name
                            before[-1].kind = "user"
                            out.append(before[-1])
                kids = el.GetCachedChildren()
                children = [kids.GetElement(i) for i in range(kids.Length)] if kids else []
                for child in reversed(children):
                    stack.append((child, url))
            return [f for f in out if f.kind != "text"]

        return await self.ctl.call(go)

    async def frame_url(self, f: FieldRef) -> str:
        def go() -> str:
            from pywinauto.uia_element_info import UIAElementInfo

            info: Any = UIAElementInfo(f.handle)
            for _ in range(80):
                info = info.parent
                if info is None:
                    return ""
                if info.control_type == "Document":
                    with contextlib.suppress(Exception):
                        return str(_wrap(info.element).iface_value.CurrentValue)
                    return ""
            return ""

        try:
            return await self.ctl.call(go)
        except Exception:
            return ""  # gone or unreadable: the caller refuses

    def _length(self, f: FieldRef) -> int | None:
        with contextlib.suppress(Exception):
            return len(str(_wrap(f.handle).iface_value.CurrentValue or ""))
        return None

    def _type_into(self, f: FieldRef, value: str, secret: bool) -> None:
        """The keyboard fallback: only while the box has the focus in Delta's window, a burst at a time."""
        from pywinauto.uia_defines import IUIA

        from delta.tools.desktop import _pg, _send_unicode

        w = self._w()
        box = _wrap(f.handle)

        def focused() -> bool:
            el = IUIA().iuia.GetFocusedElement()
            return _same(el, f.handle) and (not secret or bool(el.CurrentIsPassword))

        self._guard()
        self.ub._front(w)
        box.set_focus()
        time.sleep(0.15)
        if not focused():
            raise TabChangedError("the box Delta was filling lost the keyboard focus")
        _pg().hotkey("ctrl", "a")
        _pg().press("delete")
        for i in range(0, len(value), TYPE_CHUNK):
            self._guard()
            if not focused():
                raise TabChangedError("the keyboard focus moved away while Delta was typing")
            _send_unicode(value[i:i + TYPE_CHUNK])
        self.ctl.mark()

    async def set_value(self, f: FieldRef, value: str, secret: bool) -> None:
        def go() -> None:
            with contextlib.suppress(Exception):  # some pages ignore it: the keyboard fallback below
                _wrap(f.handle).iface_value.SetValue(value)
            if self._length(f) != len(value):
                self._type_into(f, value, secret)
            if secret and value:
                self._remember(f)

        try:
            with self.ctl.acting():
                await self.ctl.call(go)
        except (TabChangedError, DriverError):
            raise
        except Exception as e:
            raise DriverError(f"the browser did not let Delta fill the box ({type(e).__name__})") from e

    async def value_length(self, f: FieldRef) -> int | None:
        return await self.ctl.call(self._length, f)

    async def submit(self, f: FieldRef) -> None:
        def go() -> None:
            from pywinauto.uia_defines import IUIA

            from delta.tools.desktop import _pg

            w = self._w()
            self._guard()
            self.ub._front(w)
            _wrap(f.handle).set_focus()
            time.sleep(0.15)
            if not _same(IUIA().iuia.GetFocusedElement(), f.handle):
                raise TabChangedError("the sign-in box lost the keyboard focus before Delta could submit")
            _pg().press("enter")
            self.ctl.mark()

        try:
            with self.ctl.acting():
                await self.ctl.call(go)
        except (TabChangedError, DriverError):
            raise
        except LookupError as e:
            raise DriverError(str(e)) from e

    async def text(self) -> str:
        def go() -> str:
            doc = self._top_doc(self._w())
            if doc is None:
                return ""
            names = [n for n, _t, _off, _el in self.ub._nodes(doc, 1500) if n]
            return "\n".join(names)[:4000]

        return await self.ctl.call(go)

    async def challenge(self) -> str | None:
        found = CHALLENGE_WORDS.search(await self.text())
        if not found:
            return None
        word = found.group(0).lower()
        return ("hCaptcha" if "hcaptcha" in word else "Cloudflare Turnstile" if word in ("turnstile", "cloudflare",
                "checking your browser") else "reCAPTCHA")

    async def settle(self) -> None:
        def go() -> None:
            end, last, same = time.monotonic() + 10, "", 0
            while time.monotonic() < end:
                time.sleep(0.4)
                url = ""
                with contextlib.suppress(Exception):
                    doc = self._top_doc(self._w())
                    url = str(doc.iface_value.CurrentValue) if doc is not None else ""
                same = same + 1 if url and url == last else 0
                last = url
                if same >= 2:
                    return

        await self.ctl.call(go)

    def remember_secret_box(self, f: FieldRef) -> None:
        """Done in set_value, on the UI Automation thread (the box's position is read there)."""

    def _remember(self, f: FieldRef) -> None:
        with contextlib.suppress(Exception):
            import win32gui

            hwnd = int(self.ub.handle)
            r = _wrap(f.handle).rectangle()
            left, top, _right, _bottom = win32gui.GetWindowRect(hwnd)
            mask.remember(hwnd, (int(r.left), int(r.top), int(r.right), int(r.bottom)), (int(left), int(top)))

    def _doc_of(self, f: FieldRef) -> Any | None:
        from pywinauto.uia_element_info import UIAElementInfo

        info: Any = UIAElementInfo(f.handle)
        for _ in range(80):
            info = info.parent
            if info is None:
                return None
            if info.control_type == "Document":
                return _wrap(info.element)
        return None

    def _captcha_picture(self, f: FieldRef) -> Any | None:
        """The picture of the box's document nearest to the box: an Image element, or any element whose id says
        captcha (a <canvas> captcha). One whose id says captcha wins."""
        box = _wrap(f.handle).rectangle()
        doc = self._doc_of(f)
        if doc is None:
            return None
        found: list[Any] = []
        for kind in ("Image", "Custom", "Group", "Pane"):
            with contextlib.suppress(Exception):
                items = list(doc.descendants(control_type=kind))
                found += items if kind == "Image" else [
                    x for x in items if any(w in str(x.element_info.automation_id or "").lower()
                                            for w in ("captcha", "verifycode"))]
        best, best_d = None, 1e9
        for img in found:
            with contextlib.suppress(Exception):
                r = img.rectangle()
                w, h = r.right - r.left, r.bottom - r.top
                if w < 30 or h < 12 or w > 600 or h > 250:
                    continue  # icons and banners are not captchas
                d = abs((r.top + r.bottom) / 2 - (box.top + box.bottom) / 2) + abs(r.left - box.left) / 4
                if any(word in str(img.element_info.automation_id or "").lower() for word in ("captcha", "verifycode")):
                    d -= 10_000
                if d < best_d and d < 400:
                    best, best_d = img, d
        return best

    async def captcha_image(self, f: FieldRef) -> bytes | None:
        """A JPEG of the captcha picture next to box f, taken from the screen with Delta's window in front; None when
        there is no such picture. Only that rectangle is captured (never a password box)."""
        def go() -> bytes | None:
            import io

            with _per_monitor_dpi():
                img = self._captcha_picture(f)
                if img is None:
                    return None
                self._guard()
                self.ub._front(self._w())
                time.sleep(0.2)
                r = img.rectangle()
                pic = _grab(r.left, r.top, r.right - r.left, r.bottom - r.top)
            if _blank(pic):
                return None
            out = io.BytesIO()
            pic.save(out, "JPEG", quality=90)
            return out.getvalue()

        try:
            with self.ctl.acting():
                return await self.ctl.call(go)
        except TabChangedError:
            raise
        except Exception:
            return None

    async def captcha_region_image(self, f: FieldRef) -> bytes | None:
        """For the USER only (never OCR or a model): a band of the page around captcha box f, with every password box
        in it painted over, when no captcha picture element can be found (a picture drawn some other way)."""
        def go() -> bytes | None:
            import io

            from PIL import ImageDraw

            with _per_monitor_dpi():
                doc = self._doc_of(f)
                b = _wrap(f.handle).rectangle()
                d = doc.rectangle() if doc is not None else None
                h = max(20, b.bottom - b.top)
                left = max(d.left, b.left - 360) if d is not None else b.left - 360
                right = min(d.right, b.right + 360) if d is not None else b.right + 360
                top, bottom = b.top - round(2.5 * h), b.bottom + round(1.5 * h)
                secrets = []
                if doc is not None:
                    with contextlib.suppress(Exception):
                        for e in doc.descendants(control_type="Edit"):
                            with contextlib.suppress(Exception):
                                if e.element_info.element.CurrentIsPassword:
                                    secrets.append(e.rectangle())
                self._guard()
                self.ub._front(self._w())
                time.sleep(0.2)
                pic = _grab(left, top, right - left, bottom - top)
            draw = ImageDraw.Draw(pic)
            for r in secrets:
                draw.rectangle((r.left - left - 6, r.top - top - 6, r.right - left + 6, r.bottom - top + 6),
                               fill=(40, 40, 40))
            if _blank(pic):
                return None
            out = io.BytesIO()
            pic.save(out, "JPEG", quality=85)
            return out.getvalue()

        try:
            with self.ctl.acting():
                return await self.ctl.call(go)
        except TabChangedError:
            raise
        except Exception:
            return None

    async def refresh_captcha(self, f: FieldRef) -> bool:
        """A new captcha picture: invoke the picture (many simple captchas reload when clicked). False if it can't."""
        def go() -> bool:
            img = self._captcha_picture(f)
            if img is None:
                return False
            self._guard()
            with contextlib.suppress(Exception):
                img.invoke()
                time.sleep(0.7)
                return True
            return False

        try:
            with self.ctl.acting():
                return bool(await self.ctl.call(go))
        except Exception:
            return False

    async def observe(self) -> str:
        """The page after signing in, as browser_read shows it (outside content)."""
        text, _ev = await self.ctl.call(self.ub.read, None)
        return str(text)


__all__ = ["UIAOwnDriver"]
