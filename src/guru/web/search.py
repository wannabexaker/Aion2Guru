"""Search providers for source discovery (D-11). SearxNG self-hosted: no API key, no per-query cost."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass(frozen=True)
class SearchHit:
    url: str
    title: str
    snippet: str


class SearchProvider(Protocol):
    async def search(self, query: str, *, language: str = "en", limit: int = 10) -> list[SearchHit]: ...


class SearxNG:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None, timeout_s: float = 15.0) -> None:
        self.base = base_url.rstrip("/")
        self.client = client or httpx.AsyncClient()
        self.timeout_s = timeout_s

    async def search(self, query: str, *, language: str = "en", limit: int = 10) -> list[SearchHit]:
        resp = await self.client.get(
            f"{self.base}/search",
            params={"q": query, "format": "json", "language": language, "safesearch": 1},
            timeout=self.timeout_s,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return [
            SearchHit(str(r.get("url", "")), str(r.get("title", "")), str(r.get("content", "")))
            for r in results[:limit]
            if r.get("url")
        ]
