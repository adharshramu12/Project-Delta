"""For evals and tests ONLY: a stand-in for the user's own browser, driving a CDP-attached Chrome or Edge (Playwright)
through the same OwnBrowserDriver interface that the real driver (delta.logins.own_uia, Windows UI Automation on the
user's everyday browser) implements. It lets the own-browser sign-in rules (one-time consent, tab binding, origin and
frame checks, fill check, abort on a tab switch) run against the fake portal (delta.evals.portal_fixture) in a real
browser on any CI machine. It is never used for a real user.

The "tab" is the page that is active in the attached browser (BrowserManager.active): opening or switching to
another page mid-fill is a tab switch.
"""

from __future__ import annotations

import contextlib
from typing import Any

from delta.logins.own_browser import FieldRef, TabBinding
from delta.logins.signin import OTP_SEL, PASSWORD_SEL, USER_SEL, _challenge, _visible, mask_secret_fields

LOCATION_JS = "() => location.href"


class PlaywrightOwnDriver:
    browser_key = "chrome"
    browser_label = "Google Chrome"

    def __init__(self, mgr: Any, settle: Any = None, key: str = "chrome", label: str = "Google Chrome") -> None:
        self.mgr = mgr
        self._settle = settle
        self.browser_key = key
        self.browser_label = label

    async def _page(self) -> Any:
        return await self.mgr.page()

    async def ensure_window(self, url: str) -> bool:
        page = await self._page()
        if page.url in ("", "about:blank"):
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        return True

    async def binding(self) -> TabBinding | None:
        page = await self._page()
        return TabBinding("attached-browser", str(id(page)))

    async def top_url(self) -> str:
        page = await self._page()
        with contextlib.suppress(Exception):
            return str(await page.main_frame.evaluate(LOCATION_JS))
        return str(page.url)

    async def navigate(self, url: str) -> None:
        page = await self._page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

    async def fields(self) -> list[FieldRef]:
        from delta.tools.captcha import AUTO_FIELD

        page = await self._page()
        out: list[FieldRef] = []
        frames = [page.main_frame, *[f for f in page.frames if f is not page.main_frame]]
        for frame in frames:
            url = ""
            with contextlib.suppress(Exception):
                url = str(await frame.evaluate(LOCATION_JS))
            for kind, sel in (("password", PASSWORD_SEL), ("captcha", AUTO_FIELD), ("otp", OTP_SEL),
                              ("user", USER_SEL)):
                box = await _visible(frame, sel)
                if box is not None and not any(f.kind == kind and f.frame_url == url for f in out):
                    if kind == "user" and any(f.kind in ("otp", "captcha") and f.frame_url == url for f in out):
                        continue  # an OTP or captcha box can also match the generic user-name selector
                    out.append(FieldRef(kind, url, (frame, box), kind))
        return out

    async def frame_url(self, f: FieldRef) -> str:
        frame, _box = f.handle
        with contextlib.suppress(Exception):
            return str(await frame.evaluate(LOCATION_JS))
        return ""

    async def set_value(self, f: FieldRef, value: str, secret: bool) -> None:
        _frame, box = f.handle
        await box.fill(value, timeout=8000)
        if secret and value:
            with contextlib.suppress(Exception):
                await box.evaluate("e => e.setAttribute('data-delta-secret', '1')")
            await mask_secret_fields(await self._page())

    async def value_length(self, f: FieldRef) -> int | None:
        _frame, box = f.handle
        with contextlib.suppress(Exception):
            return len(await box.input_value(timeout=3000))
        return None

    async def submit(self, f: FieldRef) -> None:
        _frame, box = f.handle
        await box.press("Enter")

    async def text(self) -> str:
        page = await self._page()
        with contextlib.suppress(Exception):
            return str(await page.inner_text("body"))[:4000]
        return ""

    async def challenge(self) -> str | None:
        return await _challenge(await self._page())

    async def settle(self) -> None:
        page = await self._page()
        if self._settle is not None:
            await self._settle(page)
        else:
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)

    def remember_secret_box(self, f: FieldRef) -> None:
        """Masked with CSS in set_value (this stand-in is a Playwright page)."""

    async def observe(self) -> str:
        return (await self.text())[:3000]


__all__ = ["PlaywrightOwnDriver"]
