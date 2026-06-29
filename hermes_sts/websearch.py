from __future__ import annotations

import logging
from dataclasses import dataclass
from html.parser import HTMLParser
import time
from typing import TYPE_CHECKING, Protocol
from urllib.parse import parse_qs, unquote, urlparse

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

    def state(self) -> dict:
        return {"provider": "noop", "providers": [], "recent_success": None, "cooldowns": {}, "last_error": None}


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

    def state(self) -> dict:
        return {"provider": self.description(), "providers": [self.description()], "recent_success": None, "cooldowns": {}, "last_error": None}


class BraveSearchProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._timeout = max(0.2, min(settings.brave_timeout_seconds, 5.0))

    async def search(self, query: str, *, max_results: int = 3) -> list[SearchHit]:
        if not self.settings.brave_api_key:
            return []
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout, connect=2.0)) as client:
                resp = await client.get(
                    f"{self.settings.brave_base_url.rstrip('/')}/web/search",
                    params={"q": query, "count": max(1, min(max_results, 10))},
                    headers={
                        "Accept": "application/json",
                        "X-Subscription-Token": self.settings.brave_api_key,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Brave search failed: %s", exc)
            return []

        hits: list[SearchHit] = []
        for item in data.get("web", {}).get("results", []):
            hits.append(
                SearchHit(
                    title=str(item.get("title") or ""),
                    url=str(item.get("url") or ""),
                    content=str(item.get("description") or item.get("content") or "")[:400],
                    score=float(item.get("score") or 0.0),
                )
            )
            if len(hits) >= max_results:
                break
        return hits

    def description(self) -> str:
        return "brave"

    def state(self) -> dict:
        return {"provider": self.description(), "providers": [self.description()], "recent_success": None, "cooldowns": {}, "last_error": None}


class DuckDuckGoSearchProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._timeout = max(0.2, min(settings.duckduckgo_timeout_seconds, 5.0))

    async def search(self, query: str, *, max_results: int = 3) -> list[SearchHit]:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=2.0),
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 HermesSTS/0.1",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            ) as client:
                resp = await client.get("https://html.duckduckgo.com/html/", params={"q": query})
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("DuckDuckGo search failed: %s", exc)
            return []
        parser = _DuckDuckGoHtmlParser()
        parser.feed(resp.text)
        parser.close()
        return parser.hits[:max_results]

    def description(self) -> str:
        return "duckduckgo"

    def state(self) -> dict:
        return {"provider": self.description(), "providers": [self.description()], "recent_success": None, "cooldowns": {}, "last_error": None}


class SearxngSearchProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._timeout = max(0.2, min(settings.searxng_timeout_seconds, 8.0))

    async def search(self, query: str, *, max_results: int = 3) -> list[SearchHit]:
        if not self.settings.searxng_base_url:
            return []
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout, connect=2.0)) as client:
                resp = await client.get(
                    f"{self.settings.searxng_base_url.rstrip('/')}/search",
                    params={"q": query, "format": "json", "language": "auto"},
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("SearXNG search failed: %s", exc)
            return []
        hits: list[SearchHit] = []
        for item in data.get("results", []):
            hits.append(
                SearchHit(
                    title=str(item.get("title") or ""),
                    url=str(item.get("url") or ""),
                    content=str(item.get("content") or "")[:400],
                    score=float(item.get("score") or 0.0),
                )
            )
            if len(hits) >= max_results:
                break
        return hits

    def description(self) -> str:
        return f"searxng({self.settings.searxng_base_url.rstrip('/')})" if self.settings.searxng_base_url else "searxng(disabled)"

    def state(self) -> dict:
        return {"provider": self.description(), "providers": [self.description()], "recent_success": None, "cooldowns": {}, "last_error": None}


class ChainedWebSearchProvider:
    def __init__(self, providers: list[WebSearchProvider], *, cooldown_seconds: float = 20.0) -> None:
        self.providers = providers
        self.cooldown_seconds = cooldown_seconds
        self._last_success: str | None = None
        self._failed_until: dict[str, float] = {}
        self._last_error: str | None = None

    async def search(self, query: str, *, max_results: int = 3) -> list[SearchHit]:
        now = time.monotonic()
        for provider in self._ordered_providers(now):
            name = provider.description()
            try:
                hits = await provider.search(query, max_results=max_results)
            except Exception as exc:
                logger.warning("Search provider %s failed: %s", name, exc)
                self._last_error = f"{name}: {exc}"
                hits = []
            if hits:
                self._last_success = name
                self._last_error = None
                self._failed_until.pop(name, None)
                return hits
            self._last_error = f"{name}: no results"
            self._failed_until[name] = now + self.cooldown_seconds
        return []

    def description(self) -> str:
        names = ",".join(provider.description() for provider in self.providers)
        return f"chain({names})" if names else "noop"

    def state(self) -> dict:
        now = time.monotonic()
        return {
            "provider": self.description(),
            "providers": [provider.description() for provider in self.providers],
            "recent_success": self._last_success,
            "cooldowns": {
                name: max(0.0, until - now)
                for name, until in self._failed_until.items()
                if until > now
            },
            "last_error": self._last_error,
        }

    def _ordered_providers(self, now: float) -> list[WebSearchProvider]:
        active = [p for p in self.providers if self._failed_until.get(p.description(), 0.0) <= now]
        cooling = [p for p in self.providers if self._failed_until.get(p.description(), 0.0) > now]
        return active + cooling


class _DuckDuckGoHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hits: list[SearchHit] = []
        self._in_title = False
        self._in_snippet = False
        self._current_title = ""
        self._current_url = ""
        self._current_snippet = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        classes = set(attrs_dict.get("class", "").split())
        if tag == "a" and ("result__a" in classes or "result-link" in classes):
            self._flush()
            self._in_title = True
            self._current_url = attrs_dict.get("href", "")
        elif "result__snippet" in classes:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
        if tag in {"a", "div"} and self._in_snippet:
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title:
            self._current_title = f"{self._current_title} {text}".strip()
        elif self._in_snippet:
            self._current_snippet = f"{self._current_snippet} {text}".strip()

    def close(self) -> None:
        self._flush()
        super().close()

    def _flush(self) -> None:
        url = _decode_duckduckgo_url(self._current_url)
        if self._current_title and url:
            self.hits.append(
                SearchHit(
                    title=self._current_title,
                    url=url,
                    content=self._current_snippet[:400],
                )
            )
        self._current_title = ""
        self._current_url = ""
        self._current_snippet = ""


def _decode_duckduckgo_url(raw_url: str) -> str:
    url = (raw_url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = f"https:{url}"
    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        uddg = parse_qs(parsed.query).get("uddg", [""])[0]
        if uddg:
            return unquote(uddg)
    return url


def build_websearch(settings: Settings) -> WebSearchProvider:
    if not settings.web_search_enabled:
        return NoopWebSearchProvider()
    providers: list[WebSearchProvider] = []
    enabled = [item.strip().lower() for item in settings.web_search_providers.split(",") if item.strip()]
    enabled_set = set(enabled)

    for name in enabled:
        if name == "tavily" and settings.tavily_api_key:
            providers.append(TavilySearchProvider(settings))
            break
        if name in {"brave", "brava"} and settings.brave_api_key:
            providers.append(BraveSearchProvider(settings))
            break

    if "searxng" in enabled_set and settings.searxng_base_url:
        providers.append(SearxngSearchProvider(settings))
    if {"duckduckgo", "ddg"} & enabled_set:
        providers.append(DuckDuckGoSearchProvider(settings))
    if len(providers) == 1:
        return providers[0]
    if providers:
        return ChainedWebSearchProvider(providers)
    return NoopWebSearchProvider()
