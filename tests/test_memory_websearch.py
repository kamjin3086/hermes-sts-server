from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from hermes_sts.config import Settings
from hermes_sts.memory import (
    NoopMemoryProvider,
    OpenVikingMemoryProvider,
    SqliteMemoryProvider,
    _probe_openviking,
    build_memory,
)
from hermes_sts.websearch import (
    BraveSearchProvider,
    ChainedWebSearchProvider,
    DuckDuckGoSearchProvider,
    NoopWebSearchProvider,
    SearchHit,
    TavilySearchProvider,
    _DuckDuckGoHtmlParser,
    _decode_duckduckgo_url,
    build_websearch,
)


def _run(coro):
    return asyncio.run(coro)


class NoopMemoryProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = NoopMemoryProvider()

    def test_all_methods_return_empty_or_safe_defaults(self) -> None:
        self.assertEqual(_run(self.provider.recall("anything")), [])
        self.assertEqual(_run(self.provider.recall("")), [])
        _run(self.provider.record_turn("用户说", "助手答", session_id="sess_1"))
        self.assertEqual(_run(self.provider.list_memories()), [])
        self.assertEqual(_run(self.provider.list_memories(limit=10, offset=0, q="x")), [])
        self.assertIsNone(_run(self.provider.get_memory("mem_1")))
        _run(self.provider.update_memory("mem_1", content="新内容"))
        self.assertFalse(_run(self.provider.delete_memory("mem_1")))
        uri = _run(self.provider.add_memory(content="手动添加"))
        self.assertTrue(uri.startswith("noop://"))
        _run(self.provider.final_commit("sess_1"))

    def test_stats_shape(self) -> None:
        stats = self.provider.stats()
        self.assertIsInstance(stats, dict)
        self.assertEqual(set(stats.keys()), {"enabled", "provider", "count"})
        self.assertIs(stats["enabled"], False)
        self.assertEqual(stats["provider"], "noop")
        self.assertIsInstance(stats["count"], int)
        self.assertEqual(stats["count"], 0)


class SqliteMemoryProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "memory.sqlite3")
        self.settings = Settings(
            memory_enabled=True,
            memory_provider="sqlite",
            memory_extract_enabled=True,
            sqlite_memory_path=self.db_path,
        )
        self.provider = SqliteMemoryProvider(self.settings, llm=None)

    def tearDown(self) -> None:
        try:
            self.provider._conn.close()
        finally:
            self._tmp.cleanup()

    def test_add_and_recall_cjk(self) -> None:
        uri = _run(self.provider.add_memory(content="用户喜欢喝龙井茶", category="preferences"))
        hits = _run(self.provider.recall("龙井茶"))
        self.assertTrue(any(h.uri == uri for h in hits))
        self.assertEqual(hits[0].content, "用户喜欢喝龙井茶")

    def test_add_and_recall_english(self) -> None:
        uri = _run(self.provider.add_memory(content="The user lives in Hangzhou", category="facts"))
        hits = _run(self.provider.recall("Hangzhou user"))
        self.assertTrue(any(h.uri == uri for h in hits))
        self.assertEqual(hits[0].content, "The user lives in Hangzhou")

    def test_update_memory(self) -> None:
        uri = _run(self.provider.add_memory(content="旧内容"))
        _run(self.provider.update_memory(uri, content="新内容"))
        hit = _run(self.provider.get_memory(uri))
        self.assertIsNotNone(hit)
        self.assertEqual(hit.content, "新内容")

    def test_delete_memory(self) -> None:
        uri = _run(self.provider.add_memory(content="待删除"))
        self.assertTrue(_run(self.provider.delete_memory(uri)))
        self.assertIsNone(_run(self.provider.get_memory(uri)))

    def test_list_memories_pagination(self) -> None:
        for i in range(5):
            _run(self.provider.add_memory(content=f"条目 {i}"))
        page = _run(self.provider.list_memories(limit=2, offset=0))
        self.assertEqual(len(page), 2)
        full = _run(self.provider.list_memories(limit=50, offset=0))
        self.assertEqual(len(full), 5)

    def test_recall_no_results(self) -> None:
        for i in range(6):
            _run(self.provider.add_memory(content=f"记录 {i}"))
        hits = _run(self.provider.recall("", limit=5))
        self.assertEqual(len(hits), 5)

    def test_recall_no_match(self) -> None:
        _run(self.provider.add_memory(content="已有内容"))
        self.assertEqual(_run(self.provider.recall("完全不存在的查询词")), [])

    def test_stats_after_operations(self) -> None:
        for i in range(3):
            _run(self.provider.add_memory(content=f"事实 {i}"))
        self.assertEqual(self.provider.stats()["count"], 3)
        first_uri = _run(self.provider.list_memories(limit=1))[0].uri
        _run(self.provider.delete_memory(first_uri))
        self.assertEqual(self.provider.stats()["count"], 2)

    def test_record_turn_without_llm_is_noop(self) -> None:
        _run(self.provider.record_turn("用户说", "助手答", session_id="sess_a"))
        _run(asyncio.sleep(0))
        self.assertEqual(self.provider.stats()["count"], 0)

    def test_record_turn_with_failing_llm_does_not_crash(self) -> None:
        class FailingLlm:
            async def chat(self, *args, **kwargs):
                raise RuntimeError("llm unavailable")

        provider = SqliteMemoryProvider(
            Settings(
                memory_enabled=True,
                memory_provider="sqlite",
                memory_extract_enabled=True,
                sqlite_memory_path=self.db_path,
            ),
            llm=FailingLlm(),
        )
        try:
            _run(provider.record_turn("用户说", "助手答", session_id="sess_b"))
            if provider._bg_tasks:
                _run(asyncio.gather(*provider._bg_tasks, return_exceptions=True))
            count = provider.stats()["count"]
        finally:
            provider._conn.close()
        self.assertEqual(count, 0)


class OpenVikingMemoryProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            memory_enabled=True,
            memory_provider="openviking",
            openviking_api_key="test-key",
            openviking_base_url="http://127.0.0.1:1",
            openviking_timeout_seconds=0.2,
            openviking_commit_timeout_seconds=0.2,
        )
        self.provider = OpenVikingMemoryProvider(self.settings)

    def test_recall_unreachable_returns_empty(self) -> None:
        self.assertEqual(_run(self.provider.recall("查询")), [])

    def test_record_turn_unreachable_returns_silently(self) -> None:
        _run(self.provider.record_turn("用户说", "助手答", session_id="sess_ov"))

    def test_stats_unreachable_returns_dict(self) -> None:
        stats = self.provider.stats()
        self.assertIsInstance(stats, dict)
        self.assertEqual(stats.get("provider"), "openviking")

    def test_list_unreachable_returns_empty(self) -> None:
        self.assertEqual(_run(self.provider.list_memories()), [])

    def test_delete_unreachable_returns_false(self) -> None:
        self.assertFalse(_run(self.provider.delete_memory("viking://user/memories/x")))


class TavilySearchProviderTests(unittest.TestCase):
    def test_search_unreachable_returns_empty(self) -> None:
        settings = Settings(
            web_search_enabled=True,
            tavily_api_key="test-key",
            tavily_base_url="http://127.0.0.1:1",
            tavily_timeout_seconds=0.2,
        )
        provider = TavilySearchProvider(settings)
        self.assertEqual(_run(provider.search("anything")), [])

    def test_advanced_search_depth_downgraded(self) -> None:
        settings = Settings(
            web_search_enabled=True,
            tavily_api_key="test-key",
            tavily_search_depth="advanced",
        )
        provider = TavilySearchProvider(settings)
        self.assertEqual(provider._depth, "basic")

    def test_timeout_clamped(self) -> None:
        settings = Settings(
            web_search_enabled=True,
            tavily_api_key="test-key",
            tavily_timeout_seconds=10.0,
        )
        provider = TavilySearchProvider(settings)
        self.assertEqual(provider._timeout, 3.0)


class BraveSearchProviderTests(unittest.TestCase):
    def test_provider_parses_mocked_json_results(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.host, "api.search.brave.com")
            self.assertEqual(request.url.path, "/res/v1/web/search")
            self.assertEqual(request.url.params.get("q"), "hermes sts")
            self.assertEqual(request.url.params.get("count"), "2")
            self.assertEqual(request.headers.get("X-Subscription-Token"), "brave-key")
            return httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {"title": "First", "url": "https://example.com/1", "description": "Result one"},
                            {"title": "Second", "url": "https://example.com/2", "description": "Result two"},
                            {"title": "Third", "url": "https://example.com/3", "description": "Result three"},
                        ]
                    }
                },
            )

        transport = httpx.MockTransport(handler)
        real_async_client = httpx.AsyncClient

        def client_factory(*args, **kwargs):
            kwargs["transport"] = transport
            return real_async_client(*args, **kwargs)

        provider = BraveSearchProvider(Settings(web_search_enabled=True, brave_api_key="brave-key"))
        with patch("hermes_sts.websearch.httpx.AsyncClient", side_effect=client_factory):
            hits = _run(provider.search("hermes sts", max_results=2))

        self.assertEqual([hit.title for hit in hits], ["First", "Second"])
        self.assertEqual(hits[0].url, "https://example.com/1")
        self.assertEqual(hits[0].content, "Result one")

    def test_missing_api_key_returns_empty(self) -> None:
        provider = BraveSearchProvider(Settings(web_search_enabled=True, brave_api_key=""))
        self.assertEqual(_run(provider.search("anything")), [])


class NoopWebSearchProviderTests(unittest.TestCase):
    def test_search_returns_empty(self) -> None:
        provider = NoopWebSearchProvider()
        self.assertEqual(_run(provider.search("anything")), [])
        self.assertEqual(provider.description(), "noop")


class DuckDuckGoSearchProviderTests(unittest.TestCase):
    HTML = """
    <html><body>
      <div class="result">
        <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fx%3D1&rut=abc">Example Result</a>
        <a class="result__snippet">A compact snippet from DuckDuckGo.</a>
      </div>
      <div class="result">
        <a class="result-link" href="https://example.org/direct">Direct Result</a>
        <div class="result__snippet">Second snippet.</div>
      </div>
    </body></html>
    """

    def test_parser_decodes_duckduckgo_redirect_urls(self) -> None:
        parser = _DuckDuckGoHtmlParser()
        parser.feed(self.HTML)
        parser.close()

        self.assertEqual(len(parser.hits), 2)
        self.assertEqual(parser.hits[0].title, "Example Result")
        self.assertEqual(parser.hits[0].url, "https://example.com/a?x=1")
        self.assertIn("compact snippet", parser.hits[0].content)
        self.assertEqual(parser.hits[1].url, "https://example.org/direct")

    def test_decode_duckduckgo_url_leaves_direct_urls_alone(self) -> None:
        self.assertEqual(_decode_duckduckgo_url("https://example.org/x"), "https://example.org/x")

    def test_provider_parses_mocked_html_results(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.host, "html.duckduckgo.com")
            self.assertEqual(request.url.params.get("q"), "hermes sts")
            return httpx.Response(200, text=self.HTML)

        transport = httpx.MockTransport(handler)
        real_async_client = httpx.AsyncClient

        def client_factory(*args, **kwargs):
            kwargs["transport"] = transport
            return real_async_client(*args, **kwargs)

        provider = DuckDuckGoSearchProvider(Settings(web_search_enabled=True, duckduckgo_timeout_seconds=1.0))
        with patch("hermes_sts.websearch.httpx.AsyncClient", side_effect=client_factory):
            hits = _run(provider.search("hermes sts", max_results=1))

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].url, "https://example.com/a?x=1")


class FakeSearchProvider:
    def __init__(self, name: str, hits: list[SearchHit] | None = None):
        self.name = name
        self.hits = hits or []
        self.calls = 0

    async def search(self, query: str, *, max_results: int = 3) -> list[SearchHit]:
        self.calls += 1
        return self.hits[:max_results]

    def description(self) -> str:
        return self.name


class ChainedWebSearchProviderTests(unittest.TestCase):
    def test_falls_back_without_overriding_priority_order(self) -> None:
        empty = FakeSearchProvider("empty")
        good = FakeSearchProvider("good", [SearchHit(title="ok", url="https://example.test", content="hit")])
        provider = ChainedWebSearchProvider([empty, good], cooldown_seconds=0.0)

        first = _run(provider.search("anything"))
        second = _run(provider.search("anything else"))
        state = provider.state()

        self.assertEqual(first[0].title, "ok")
        self.assertEqual(second[0].title, "ok")
        self.assertEqual(empty.calls, 2)
        self.assertEqual(good.calls, 2)
        self.assertEqual(state["recent_success"], "good")
        self.assertEqual(state["providers"], ["empty", "good"])


class BuildWebSearchTests(unittest.TestCase):
    def test_prefers_one_configured_online_api_then_searxng_then_duckduckgo(self) -> None:
        settings = Settings(
            web_search_enabled=True,
            web_search_providers="duckduckgo,searxng,brave,tavily",
            tavily_api_key="tavily-key",
            brave_api_key="brave-key",
            searxng_base_url="http://searx.local",
        )

        provider = build_websearch(settings)

        self.assertIsInstance(provider, ChainedWebSearchProvider)
        self.assertEqual(
            provider.state()["providers"],
            ["brave", "searxng(http://searx.local)", "duckduckgo"],
        )

    def test_skips_unconfigured_online_and_searxng_providers(self) -> None:
        settings = Settings(
            web_search_enabled=True,
            web_search_providers="tavily,brave,searxng,duckduckgo",
            tavily_api_key="",
            brave_api_key="",
            searxng_base_url="",
        )

        provider = build_websearch(settings)

        self.assertIsInstance(provider, DuckDuckGoSearchProvider)

    def test_brava_alias_maps_to_brave_for_compatibility(self) -> None:
        settings = Settings(
            web_search_enabled=True,
            web_search_providers="brava",
            brave_api_key="brave-key",
        )

        provider = build_websearch(settings)

        self.assertIsInstance(provider, BraveSearchProvider)


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class ProbeOpenVikingTests(unittest.TestCase):
    def _settings(self, **overrides) -> Settings:
        defaults = dict(
            memory_enabled=True,
            memory_provider="openviking",
            openviking_api_key="k",
            openviking_base_url="http://ov.example.test",
        )
        defaults.update(overrides)
        return Settings(**defaults)

    def test_transport_error_returns_false(self) -> None:
        def raise_conn(_url, **_kwargs):
            raise ConnectionError("refused")

        with patch("hermes_sts.memory.httpx.get", side_effect=raise_conn):
            self.assertFalse(_probe_openviking(self._settings(), timeout=0.1))

    def test_any_http_response_returns_true(self) -> None:
        for status in (200, 401, 404, 500):
            with patch("hermes_sts.memory.httpx.get", return_value=_FakeResponse(status)):
                self.assertTrue(_probe_openviking(self._settings(), timeout=0.1), status)


class BuildMemoryProbeTests(unittest.TestCase):
    def _settings(self, **overrides) -> Settings:
        defaults = dict(
            memory_enabled=True,
            memory_provider="openviking",
            openviking_api_key="k",
            openviking_base_url="http://ov.example.test",
            sqlite_memory_path=":memory:",
        )
        defaults.update(overrides)
        return Settings(**defaults)

    def test_disabled_returns_noop(self) -> None:
        self.assertIsInstance(build_memory(Settings(memory_enabled=False)), NoopMemoryProvider)

    def test_explicit_noop_provider(self) -> None:
        s = Settings(memory_enabled=True, memory_provider="noop")
        self.assertIsInstance(build_memory(s), NoopMemoryProvider)

    def test_explicit_sqlite_provider(self) -> None:
        s = Settings(memory_enabled=True, memory_provider="sqlite", sqlite_memory_path=":memory:")
        self.assertIsInstance(build_memory(s), SqliteMemoryProvider)

    def test_openviking_missing_apikey_falls_back_to_sqlite(self) -> None:
        s = self._settings(openviking_api_key="")
        self.assertIsInstance(build_memory(s), SqliteMemoryProvider)

    def test_openviking_probe_unreachable_falls_back_to_sqlite(self) -> None:
        s = self._settings()
        def raise_conn(_url, **_kwargs):
            raise ConnectionError("refused")
        with patch("hermes_sts.memory.httpx.get", side_effect=raise_conn):
            self.assertIsInstance(build_memory(s), SqliteMemoryProvider)

    def test_openviking_probe_ok_uses_openviking(self) -> None:
        s = self._settings()
        with patch("hermes_sts.memory.httpx.get", return_value=_FakeResponse(200)):
            self.assertIsInstance(build_memory(s), OpenVikingMemoryProvider)

    def test_unknown_provider_falls_back_to_noop(self) -> None:
        s = Settings(memory_enabled=True, memory_provider="weird")
        self.assertIsInstance(build_memory(s), NoopMemoryProvider)


if __name__ == "__main__":
    unittest.main()
