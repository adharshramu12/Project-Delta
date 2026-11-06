"""Agent tools for parallel read-only helpers.

parallel_research  the agent hands 2 to N independent questions to helpers that run at the same time
                   (only when helpers are turned on in settings; see supervisor.py)
helper_read_page   what helpers use to read a page that needs JavaScript: a fresh headless browser context of
                   the Chrome or Edge already installed, with no profile, cookies or sign-ins, closed right
                   after. Never the user's own browser and never Delta's signed-in profile. Offered to helpers
                   only; plain fetch_page is used when no browser or Playwright is available.
"""

from __future__ import annotations

import contextlib
from typing import Any

from pydantic import BaseModel, Field

from delta.core.state import CURRENT_REQUEST
from delta.helpers.readonly import IN_HELPER
from delta.helpers.supervisor import HelperSupervisor, merge
from delta.tools.base import Risk, Tool, ToolResult

PAGE_CHARS = 15000
PAGE_TIMEOUT_MS = 20000


class ResearchArgs(BaseModel):
    questions: list[str] = Field(description="2 to 4 independent, self-contained questions to look up at once")


class ParallelResearch(Tool[ResearchArgs]):
    name = "parallel_research"
    description = ("Look up several INDEPENDENT questions at the same time with read-only helpers (web search, "
                   "reading pages and files in allowed folders). Returns each answer with its sources.")
    risk = Risk.READ
    Args = ResearchArgs

    def __init__(self, supervisor: HelperSupervisor) -> None:
        self.supervisor = supervisor

    @property
    def timeout_s(self) -> float:  # the watchdog's limit for this call: helpers run side by side
        return float(self.supervisor.cfg.timeout_s) + 15

    def available(self) -> bool:
        return self.supervisor.enabled

    def summary(self, args: ResearchArgs) -> str:
        return f"look up {len(args.questions)} questions in parallel (read-only helpers)"

    async def run(self, args: ResearchArgs) -> ToolResult:
        if not self.supervisor.enabled:
            return ToolResult(False, "parallel helpers are turned off in settings (helpers.enabled)")
        req = CURRENT_REQUEST.get()
        results = await self.supervisor.run(req.id if req is not None else "", args.questions)
        if not results:
            return ToolResult(False, "no questions to look up")
        return ToolResult(any(r.ok for r in results), merge(results),
                          {"helpers": [r.to_dict() for r in results],
                           "helper_tokens": sum(r.tokens for r in results)})


class ReadPageArgs(BaseModel):
    url: str


def _web_url(url: str) -> str:
    u = url.strip()
    if "://" not in u:
        u = "https://" + u
    return u if u.lower().startswith(("http://", "https://")) else ""


class HelperReadPage(Tool[ReadPageArgs]):
    name = "helper_read_page"
    description = "Read a web page's text in a fresh headless browser (no cookies, no sign-ins)."
    risk = Risk.READ
    Args = ReadPageArgs

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def available(self) -> bool:
        return self.enabled and IN_HELPER.get()

    def domains(self, args: ReadPageArgs) -> list[str]:
        return [args.url]

    async def run(self, args: ReadPageArgs) -> ToolResult:
        url = _web_url(args.url)
        if not url:
            return ToolResult(False, "helpers read only http and https pages")
        try:
            return await self._headless(url)
        except Exception as e:  # no browser, no Playwright, a page that would not load: plain download instead
            from delta.tools.web_search import FetchArgs, FetchPage

            got = await FetchPage().run(FetchArgs(url=url))
            if got.ok:
                got.evidence["headless"] = False
                got.evidence["note"] = f"read without a browser ({type(e).__name__})"
            return got

    async def _headless(self, url: str) -> ToolResult:
        from playwright.async_api import async_playwright

        from delta.tools.browser import find_browser

        found = find_browser("auto")
        if not found:
            raise RuntimeError("no Chrome or Edge")
        _, exe = found
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(executable_path=exe, headless=True,
                                               args=["--no-first-run", "--disable-extensions", "--disable-sync"])
            try:
                ctx: Any = await browser.new_context(accept_downloads=False, service_workers="block")
                page = await ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("networkidle", timeout=3000)
                title = await page.title()
                text = await page.inner_text("body")
                final = page.url
            finally:
                await browser.close()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        body = "\n".join(lines)[:PAGE_CHARS]
        return ToolResult(True, f"{title} - {final}\n\n{body}", {"url": final, "headless": True})


def helper_tools(supervisor: HelperSupervisor, read_page: bool = True) -> list[Tool]:
    return [ParallelResearch(supervisor), HelperReadPage(read_page)]
