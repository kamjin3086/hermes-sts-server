from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import httpx

if TYPE_CHECKING:
    from hermes_sts.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchHit:
    title: str = ""
    url: str = ""
    content: str = ""
    score: float = 0.0


class WebSearchProvider(Protocol):
    async def search(self, query: str, *, max_results: int = 3) -> list[SearchHit]:
        ...

    def description(self) -> str:
        ...


class NoopWebSearchProvider:
    async def search(self, query: str, *, max_results: int = 3) -> list[SearchHit]:
        return []

    def description(self) -> str:
        return "noop"


class TavilySearchProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

        depth = settings.tavily_search_depth
        if depth == "advanced":
            depth = "basic"
            logger.warning("tavily_search_depth 'advanced' is not supported, falling back to 'basic'")
        self._depth = depth

        timeout = settings.tavily_timeout_seconds
        if timeout > 3.0:
            timeout = 3.0
            logger.warning("tavily_timeout_seconds > 3.0 is clamped to 3.0")
        self._timeout = timeout

    async def search(self, query: str, *, max_results: int | None = None) -> list[SearchHit]:
        body = {
            "api_key": self.settings.tavily_api_key,
            "query": query,
            "search_depth": self._depth,
            "max_results": max_results or self.settings.tavily_max_results,
            "include_answer": False,
            "include_raw_content": False,
        }
        headers = {
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=2.0),
            ) as client:
                resp = await client.post(
                    f"{self.settings.tavily_base_url.rstrip('/')}/search",
                    json=body,
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Tavily search failed: %s", exc)
            return []

        hits: list[SearchHit] = []
        for item in data.get("results", []):
            hits.append(
                SearchHit(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    content=item.get("content", "")[:400],
                    score=item.get("score", 0.0),
                )
            )
        return hits

    def description(self) -> str:
        return f"tavily({self._depth})"


def build_websearch(settings: Settings) -> WebSearchProvider:
    if settings.web_search_enabled and settings.tavily_api_key:
        return TavilySearchProvider(settings)
    return NoopWebSearchProvider()
