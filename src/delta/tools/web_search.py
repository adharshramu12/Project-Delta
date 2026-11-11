"""Web search with provider fallback (Tavily -> SerpAPI -> DuckDuckGo) and a page reader.

Paid providers that run out of credits (401/402/403/429) are skipped for a cool-down so we do
not burn time retrying them; DuckDuckGo needs no key and is always last.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import httpx

from pydantic import BaseModel, Field

from delta.system.logging import log
from delta.system.secrets import get_secret
from delta.tools.base import Risk, Tool, ToolResult


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str


class SearchError(RuntimeError):
    pass


class SearchProvider(Protocol):
    name: str

    def available(self) -> bool: ...
    async def search(self, query: str, n: int) -> list[SearchHit]: ...


def _raise_for(resp: httpx.Response, name: str) -> None:
    if resp.status_code >= 400:
        raise SearchError(f"{name}: HTTP {resp.status_code}")


class Tavily:
    name = "tavily"

    def available(self) -> bool:
        return bool(get_secret("tavily_api_key"))

    async def search(self, query: str, n: int) -> list[SearchHit]:
        import httpx

        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post("https://api.tavily.com/search",
                             json={"api_key": get_secret("tavily_api_key"), "query": query, "max_results": n})
        _raise_for(r, self.name)
        return [SearchHit(x.get("title", ""), x.get("url", ""), x.get("content", ""))
                for x in r.json().get("results", [])]


class SerpApi:
    name = "serpapi"

    def available(self) -> bool:
        return bool(get_secret("serpapi_api_key"))

    async def search(self, query: str, n: int) -> list[SearchHit]:
        import httpx

        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get("https://serpapi.com/search.json",
                            params={"engine": "google", "q": query, "num": n,
                                    "api_key": get_secret("serpapi_api_key")})
        _raise_for(r, self.name)
        data = r.json()
        if "error" in data:
            raise SearchError(f"serpapi: {data['error']}")
        return [SearchHit(x.get("title", ""), x.get("link", ""), x.get("snippet", ""))
                for x in data.get("organic_results", [])[:n]]


class DuckDuckGo:
    name = "duckduckgo"

    def available(self) -> bool:
        return True

    async def search(self, query: str, n: int) -> list[SearchHit]:
        from ddgs import DDGS  # lazy: only imported when actually needed

        def run() -> list[dict]:
            return list(DDGS().text(query, max_results=n))

        try:
            rows = await asyncio.to_thread(run)
        except Exception as e:
            raise SearchError(f"duckduckgo: {e}") from e
        return [SearchHit(x.get("title", ""), x.get("href", ""), x.get("body", "")) for x in rows]


class SearchRouter:
    COOLDOWN_S = 3600      # a refused key or a used-up plan: no point asking again for an hour
    SHORT_COOLDOWN_S = 60  # anything else (a slow network, a busy service): try again in a minute
    _LONG = ("401", "402", "403", "quota", "credit", "invalid api key", "unauthorized", "payment")
    _EMPTY = ("no results", "no result")

    def __init__(self, providers: list[SearchProvider]) -> None:
        self.providers = providers
        self._cooldown: dict[str, float] = {}

    @classmethod
    def from_order(cls, order: list[str]) -> SearchRouter:
        known: dict[str, SearchProvider] = {"tavily": Tavily(), "serpapi": SerpApi(), "duckduckgo": DuckDuckGo()}
        return cls([known[n] for n in order if n in known])

    async def search(self, query: str, n: int = 5) -> tuple[str, list[SearchHit]]:
        import httpx

        errors: list[str] = []
        empty = False
        usable = [p for p in self.providers if p.available()]
        rested = [p for p in usable if self._cooldown.get(p.name, 0) <= time.monotonic()]
        # Every provider resting is not a reason to answer nothing: ask the last one anyway (the free one).
        for p in rested or usable[-1:]:
            try:
                hits = await p.search(query, n)
            except (SearchError, httpx.HTTPError) as e:
                text = str(e)
                if any(w in text.lower() for w in self._EMPTY):
                    empty = True  # the search worked and found nothing: that is an answer, not a broken provider
                    continue
                long = any(w in text.lower() for w in self._LONG)
                self._cooldown[p.name] = time.monotonic() + (self.COOLDOWN_S if long else self.SHORT_COOLDOWN_S)
                errors.append(text)
                log.warning("search.fallback", provider=p.name, error=text)
                continue
            if hits:
                return p.name, hits
            empty = True
        if empty and not errors:
            raise SearchError(f"No results for '{query[:80]}'. Try fewer or different words.")
        why = "; ".join(errors) or "no search provider is set up"
        raise SearchError(f"Web search is not working right now ({why}). "
                          "Use browser_search to search in the browser instead.")


class SearchArgs(BaseModel):
    query: str
    max_results: int = Field(default=5, le=10)


class WebSearch(Tool[SearchArgs]):
    name = "web_search"
    description = "Search the internet. Use whenever you are unsure, or for anything recent or unfamiliar."
    risk = Risk.READ
    Args = SearchArgs

    def __init__(self, router: SearchRouter) -> None:
        self.router = router

    async def run(self, args: SearchArgs) -> ToolResult:
        try:
            provider, hits = await self.router.search(args.query, args.max_results)
        except SearchError as e:
            return ToolResult(False, str(e))
        text = "\n\n".join(f"[{i + 1}] {h.title}\n{h.url}\n{h.snippet}" for i, h in enumerate(hits))
        return ToolResult(True, text, {"provider": provider, "urls": [h.url for h in hits]})


class FetchArgs(BaseModel):
    url: str


class FetchPage(Tool[FetchArgs]):
    name = "fetch_page"
    description = "Download a web page and return its main readable text."
    risk = Risk.READ
    Args = FetchArgs

    def domains(self, args: FetchArgs) -> list[str]:
        return [args.url]

    async def run(self, args: FetchArgs) -> ToolResult:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                         headers={"User-Agent": "Mozilla/5.0 DeltaAgent"}) as c:
                r = await c.get(args.url)
        except httpx.HTTPError as e:
            return ToolResult(False, f"fetch failed: {e}")
        if r.status_code >= 400:
            return ToolResult(False, f"HTTP {r.status_code}")
        import trafilatura

        text = await asyncio.to_thread(trafilatura.extract, r.text) or r.text[:5000]
        return ToolResult(True, text[:15000], {"url": str(r.url), "status": r.status_code})
