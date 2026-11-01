"""Web search with provider fallback (Tavily -> SerpAPI -> DuckDuckGo) and a page reader.

Paid providers that run out of credits (401/402/403/429) are skipped for a cool-down so we do
not burn time retrying them; DuckDuckGo needs no key and is always last.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol

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
    COOLDOWN_S = 3600

    def __init__(self, providers: list[SearchProvider]) -> None:
        self.providers = providers
        self._cooldown: dict[str, float] = {}

    @classmethod
    def from_order(cls, order: list[str]) -> SearchRouter:
        known: dict[str, SearchProvider] = {"tavily": Tavily(), "serpapi": SerpApi(), "duckduckgo": DuckDuckGo()}
        return cls([known[n] for n in order if n in known])

    async def search(self, query: str, n: int = 5) -> tuple[str, list[SearchHit]]:
        errors = []
        for p in self.providers:
            if not p.available() or self._cooldown.get(p.name, 0) > time.monotonic():
                continue
            try:
                hits = await p.search(query, n)
            except (SearchError, httpx.HTTPError) as e:
                self._cooldown[p.name] = time.monotonic() + self.COOLDOWN_S
                errors.append(str(e))
                log.warning("search.fallback", provider=p.name, error=str(e))
                continue
            if hits:
                return p.name, hits
        raise SearchError("all search providers failed: " + "; ".join(errors))


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
