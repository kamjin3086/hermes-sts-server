from __future__ import annotations

import asyncio
import unittest

from hermes_sts.config import Settings
from hermes_sts.memory import MemoryHit
from hermes_sts.tools import ToolRegistry, register_default_local_tools
from hermes_sts.websearch import TavilySearchProvider
from tests.test_core import bare_session


class FakeMemoryProvider:
    def __init__(self, returns=None):
        self.returns = returns or []
        self.recorded_turns: list[tuple[str, str, str]] = []
        self.recall_calls: list[tuple[str, int, float]] = []

    async def recall(self, query: str, *, limit: int = 5, min_score: float = 0.0):
        self.recall_calls.append((query, limit, min_score))
        return self.returns

    async def record_turn(self, transcript: str, answer: str, *, session_id: str):
        self.recorded_turns.append((transcript, answer, session_id))

    async def final_commit(self, session_id: str):
        pass


class BadFake:
    async def recall(self, *args, **kwargs):
        raise AssertionError("BadFake.recall should not be called")


class RealtimeMemoryTests(unittest.TestCase):
    # ------------------------------------------------------------------
    # _inject_memory
    # ------------------------------------------------------------------

    def test_inject_memory_appends_hits_to_instructions(self):
        session = bare_session()
        session.settings = Settings(memory_enabled=True)
        hit = MemoryHit(uri="mem_1", content="content", abstract="abstract text")
        session.memory = FakeMemoryProvider(returns=[hit])

        result = asyncio.run(session._inject_memory("user query", "base instructions"))

        self.assertIn("base instructions", result)
        self.assertIn("abstract text", result)

    def test_inject_memory_skips_when_disabled(self):
        session = bare_session()
        session.settings = Settings(memory_enabled=False)
        session.memory = BadFake()

        result = asyncio.run(session._inject_memory("user query", "base instructions"))

        self.assertEqual(result, "base instructions")

    def test_inject_memory_skips_in_hermes_when_disabled(self):
        session = bare_session()
        session.settings = Settings(
            llm_provider="hermes_agent",
            memory_enabled=True,
            memory_remember_in_hermes=False,
        )
        session.memory = BadFake()

        result = asyncio.run(session._inject_memory("user query", "base instructions"))

        self.assertEqual(result, "base instructions")

    def test_inject_memory_budget_caps_block(self):
        session = bare_session()
        session.settings = Settings(
            memory_enabled=True,
            memory_injection_budget=500,
        )
        # 10 hits each with 100-char abstract => lines of ~102 chars ("- " + 100)
        hits = [
            MemoryHit(uri=f"mem_{i}", content="x" * 100, abstract="x" * 100)
            for i in range(10)
        ]
        session.memory = FakeMemoryProvider(returns=hits)

        result = asyncio.run(session._inject_memory("user query", "base instructions"))

        self.assertIn("base instructions", result)
        # Extract the appended block (everything after base instructions)
        block = result[len("base instructions"):]
        self.assertLessEqual(len(block), 700)

    # ------------------------------------------------------------------
    # _fire_record_turn
    # ------------------------------------------------------------------

    def test_record_turn_dispatched_in_openai_mode(self):
        memory = FakeMemoryProvider()

        async def run():
            session = bare_session()
            session.settings = Settings(
                llm_provider="openai_compatible",
                memory_enabled=True,
            )
            session.memory = memory
            session.session_id = "sess_record_test"
            session._fire_record_turn("user said hello", "hello there")
            # Yield control so the background task can execute
            await asyncio.sleep(0.01)

        asyncio.run(run())

        self.assertEqual(len(memory.recorded_turns), 1)
        transcript, answer, sid = memory.recorded_turns[0]
        self.assertEqual(transcript, "user said hello")
        self.assertEqual(answer, "hello there")

    def test_record_turn_skipped_in_hermes_mode(self):
        memory = FakeMemoryProvider()

        async def run():
            session = bare_session()
            session.settings = Settings(
                llm_provider="hermes_agent",
                memory_enabled=True,
            )
            session.memory = memory
            session._fire_record_turn("user said hello", "hello there")
            await asyncio.sleep(0.01)

        asyncio.run(run())

        self.assertEqual(len(memory.recorded_turns), 0)

    # ------------------------------------------------------------------
    # Tool registration
    # ------------------------------------------------------------------

    def test_web_search_tool_not_registered_in_hermes_mode(self):
        registry = ToolRegistry()
        settings = Settings(llm_provider="hermes_agent")
        register_default_local_tools(registry, settings)

        tool_names = [t["function"]["name"] for t in registry.openai_tools()]
        self.assertNotIn("web_search", tool_names)

    def test_register_default_local_tools_in_openai_mode(self):
        registry = ToolRegistry()
        settings = Settings(
            llm_provider="openai_compatible",
            web_search_enabled=True,
            tavily_api_key="test-key",
        )
        tavily = TavilySearchProvider(settings)
        register_default_local_tools(registry, settings, web_search_provider=tavily)

        tool_names = [t["function"]["name"] for t in registry.openai_tools()]
        self.assertIn("web_search", tool_names)

    def test_terminal_tool_is_explicitly_gated(self):
        hermes_registry = ToolRegistry()
        register_default_local_tools(
            hermes_registry,
            Settings(llm_provider="hermes_agent", terminal_tool_enabled=True),
        )
        self.assertNotIn("terminal_exec", [t["function"]["name"] for t in hermes_registry.openai_tools()])

        openai_registry = ToolRegistry()
        register_default_local_tools(
            openai_registry,
            Settings(llm_provider="openai_compatible", terminal_tool_enabled=True),
        )
        self.assertIn("terminal_exec", [t["function"]["name"] for t in openai_registry.openai_tools()])


if __name__ == "__main__":
    unittest.main()
