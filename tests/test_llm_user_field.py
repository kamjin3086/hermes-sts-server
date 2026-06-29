"""Tests that LLM request body includes conversation_id as user field."""
from __future__ import annotations

import asyncio
import unittest

import httpx

from hermes_sts.config import Settings
from hermes_sts.llm import BaseOpenAIChatProvider


class _ConcreteProvider(BaseOpenAIChatProvider):
    """Concrete subclass to avoid abstract property errors."""

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:1/v1"

    @property
    def model(self) -> str:
        return "dummy"

    @property
    def api_key(self) -> str:
        return ""

    @property
    def max_tokens(self) -> int:
        return 16

    @property
    def timeout(self) -> float:
        return 1.0


class TestUserField(unittest.TestCase):

    def _make_fake_client(self, captured: dict) -> type:
        """Build a fake httpx.AsyncClient subclass that captures POST bodies."""

        class FakeResponse:
            def json(self):
                return {"choices": [{"message": {"content": "ok"}}]}

            def raise_for_status(self):
                pass

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def post(self, url, *, json=None, **kwargs):
                captured["body"] = json
                return FakeResponse()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        return FakeClient

    def test_chat_includes_user_in_body_when_conversation_id_set(self):
        """When conversation_id is set on provider, body['user'] matches it."""
        captured: dict = {}
        orig_client = httpx.AsyncClient
        try:
            httpx.AsyncClient = self._make_fake_client(captured)  # type: ignore[misc]
            provider = _ConcreteProvider(Settings())
            provider.conversation_id = "conv_test123"
            asyncio.run(provider.chat("hello"))
        finally:
            httpx.AsyncClient = orig_client

        body = captured.get("body", {})
        self.assertEqual(body.get("user"), "conv_test123")

    def test_chat_omits_user_when_none(self):
        """When conversation_id is None, body should NOT contain user field."""
        captured: dict = {}
        orig_client = httpx.AsyncClient
        try:
            httpx.AsyncClient = self._make_fake_client(captured)  # type: ignore[misc]
            provider = _ConcreteProvider(Settings())
            provider.conversation_id = None
            asyncio.run(provider.chat("hello"))
        finally:
            httpx.AsyncClient = orig_client

        body = captured.get("body", {})
        self.assertNotIn("user", body)

    def test_chat_enables_llama_prompt_cache_by_default(self):
        """llama.cpp prompt cache options should be present on text requests."""
        captured: dict = {}
        orig_client = httpx.AsyncClient
        try:
            httpx.AsyncClient = self._make_fake_client(captured)  # type: ignore[misc]
            provider = _ConcreteProvider(Settings())
            asyncio.run(provider.chat("hello"))
        finally:
            httpx.AsyncClient = orig_client

        body = captured.get("body", {})
        self.assertIs(body.get("cache_prompt"), True)
        self.assertNotIn("id_slot", body)

    def test_chat_can_pin_llama_prompt_cache_slot(self):
        """Dedicated llama.cpp deployments can pin a known-safe slot."""
        captured: dict = {}
        orig_client = httpx.AsyncClient
        try:
            httpx.AsyncClient = self._make_fake_client(captured)  # type: ignore[misc]
            provider = _ConcreteProvider(Settings(llm_cache_slot=3))
            asyncio.run(provider.chat("hello"))
        finally:
            httpx.AsyncClient = orig_client

        body = captured.get("body", {})
        self.assertIs(body.get("cache_prompt"), True)
        self.assertEqual(body.get("id_slot"), 3)

    def test_chat_can_disable_llama_prompt_cache_options(self):
        """Non-llama OpenAI-compatible backends can opt out of extra fields."""
        captured: dict = {}
        orig_client = httpx.AsyncClient
        try:
            httpx.AsyncClient = self._make_fake_client(captured)  # type: ignore[misc]
            provider = _ConcreteProvider(Settings(llm_cache_prompt=False, llm_cache_slot=-1))
            asyncio.run(provider.chat("hello"))
        finally:
            httpx.AsyncClient = orig_client

        body = captured.get("body", {})
        self.assertNotIn("cache_prompt", body)
        self.assertNotIn("id_slot", body)


if __name__ == "__main__":
    unittest.main()
