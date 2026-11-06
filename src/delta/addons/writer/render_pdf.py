"""HTML -> PDF through Chrome or Edge, the browsers Delta already drives (Playwright, no download).

Every Windows 10/11 PC has Microsoft Edge, so this works on a fresh laptop with nothing extra
installed. The page is printed in a throwaway headless browser with its own empty profile:
  - JavaScript disabled,
  - every network request refused except data: URLs (the page is self-contained; nothing is fetched),
  - service workers blocked, downloads off, a hard timeout.
If no browser can be found, the caller keeps the HTML file and says so (open it, Ctrl+P, Save as PDF).
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from delta.addons.writer.document import Doc
from delta.addons.writer.render_html import footer_template, header_template, render_html

RENDER_TIMEOUT_S = 60
ENV_BROWSER = "DELTA_PDF_BROWSER"  # optional override: full path to a Chromium-based browser


class PdfUnavailable(RuntimeError):
    """No Chromium-based browser to print with."""


def find_pdf_browser(override: str = "") -> str | None:
    """Explicit path, then $DELTA_PDF_BROWSER, then the Chrome/Edge Delta uses for browsing, then
    Chromium on PATH (Linux/macOS developers)."""
    for cand in (override, os.environ.get(ENV_BROWSER, "")):
        if cand and Path(os.path.expandvars(cand)).is_file():
            return os.path.expandvars(cand)
    try:
        from delta.tools.browser import find_browser

        found = find_browser()
        if found:
            return found[1]
    except Exception:  # browser module needs optional desktop bits on some systems
        pass
    for name in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser", "chromium-headless-shell"):
        if path := shutil.which(name):
            return path
    return None


async def html_to_pdf(html: str, out: Path, *, header: str = "<span></span>", footer: str = "<span></span>",
                      browser_path: str = "", timeout_s: float = RENDER_TIMEOUT_S) -> Path:
    exe = find_pdf_browser(browser_path)
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:  # pragma: no cover - playwright is a core dependency
        raise PdfUnavailable("Playwright is not installed") from e
    if not exe:
        raise PdfUnavailable("no Chrome, Edge or Chromium found to print the PDF with")
    # Playwright launches with a fresh temporary profile and removes it afterwards.
    return await asyncio.wait_for(_print(async_playwright, exe, html, out, header, footer), timeout_s)


async def _print(async_playwright: object, exe: str, html: str, out: Path, header: str, footer: str) -> Path:
    async with async_playwright() as pw:  # type: ignore[operator]
        browser = await pw.chromium.launch(
            executable_path=exe, headless=True,
            args=["--disable-extensions", "--disable-background-networking", "--disable-sync",
                  "--no-first-run", "--disable-component-update", "--disable-default-apps"])
        try:
            context = await browser.new_context(java_script_enabled=False, offline=True, service_workers="block",
                                                accept_downloads=False)

            async def guard(route: object) -> None:
                url = route.request.url  # type: ignore[attr-defined]
                if url.startswith(("data:", "about:")):
                    await route.continue_()  # type: ignore[attr-defined]
                else:
                    await route.abort()  # type: ignore[attr-defined]

            await context.route("**/*", guard)
            page = await context.new_page()
            await page.set_content(html, wait_until="load")
            await page.emulate_media(media="print")
            out.parent.mkdir(parents=True, exist_ok=True)
            await page.pdf(path=str(out), prefer_css_page_size=True, print_background=True,
                           display_header_footer=True, header_template=header, footer_template=footer)
        finally:
            await browser.close()
    return out


async def render_pdf(doc: Doc, out: Path, browser_path: str = "") -> Path:
    return await html_to_pdf(render_html(doc), out, header=header_template(doc), footer=footer_template(doc),
                             browser_path=browser_path)
