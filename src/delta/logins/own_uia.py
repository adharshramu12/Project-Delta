"""The own-browser driver for saved logins: Delta's one window in the user's everyday Chrome or Edge, through Windows
UI Automation (delta.tools.user_browser.UserBrowser). Used by delta.logins.own_browser; see its docstring for the
rules. Windows only; every UI Automation call runs on the desktop controller's thread.

How the checks map onto UI Automation:
- the tab: the selected tab in Delta's window (its runtime id) and the window handle;
- the page's address: the Value of the tab's top Document element (the full URL, scheme included; the address bar
  is NOT used, because it hides "http://" and "https://");
- a frame's address: the Value of the nearest Document element above the box (frames are Documents of their own);
- a box: an Edit element; IsPassword marks password boxes; its name (label, placeholder) says what else it is;
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
        with contextlib.suppress(Exception):
            for t in w.descendants(control_type="TabItem"):
                with contextlib.suppress(Exception):
                    if t.iface_selection_item.CurrentIsSelected:
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
                         dll.UIA_IsPasswordPropertyId, dll.UIA_IsEnabledPropertyId, dll.UIA_ValueValuePropertyId):
                cr.AddProperty(prop)
            cr.TreeScope = dll.TreeScope_Subtree
            cr.TreeFilter = u.iuia.ControlViewCondition
            top = doc.element_info.element.BuildUpdatedCache(cr)
            edit, document = dll.UIA_EditControlTypeId, dll.UIA_DocumentControlTypeId
            out: list[FieldRef] = []
            plain: list[FieldRef] = []  # text boxes without a telling name, in page order
            stack: list[tuple[Any, str]] = [(top, str(top.CachedValueValue or ""))]
            seen = 0
            while stack and seen < MAX_NODES:
                el, url = stack.pop()
                seen += 1
                if el.CachedControlType == document and el is not top:
                    url = str(el.CachedValueValue or "")
                if el.CachedControlType == edit and not el.CachedIsOffscreen and el.CachedIsEnabled:
                    name = str(el.CachedName or "")
                    kind = classify(name, bool(el.CachedIsPassword))
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

    async def observe(self) -> str:
        """The page after signing in, as browser_read shows it (outside content)."""
        text, _ev = await self.ctl.call(self.ub.read, None)
        return str(text)


__all__ = ["UIAOwnDriver"]
