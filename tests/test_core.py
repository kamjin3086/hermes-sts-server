from __future__ import annotations

import asyncio
import base64
import math
import types
import unittest

from hermes_sts.config import Settings
from hermes_sts.llm import BaseOpenAIChatProvider, LLMResponse, ToolCall
from hermes_sts.realtime import RealtimeSession
from hermes_sts.tools import ToolRegistry
from hermes_sts.vad import EnergyVad, build_vad


class DummyChatProvider(BaseOpenAIChatProvider):
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


def pcm_tone(sample_rate: int, duration_s: float, amplitude: float = 0.2) -> bytes:
    frames = int(sample_rate * duration_s)
    out = bytearray()
    for i in range(frames):
        sample = int(math.sin(2 * math.pi * 440 * i / sample_rate) * amplitude * 32767)
        out.extend(sample.to_bytes(2, "little", signed=True))
    return bytes(out)


def pcm_silence(sample_rate: int, duration_s: float) -> bytes:
    return b"\x00\x00" * int(sample_rate * duration_s)


def bare_session() -> RealtimeSession:
    session = object.__new__(RealtimeSession)
    session.settings = Settings(vad_provider="energy")
    session.tools = ToolRegistry()
    session.instructions = ""
    session.session_id = "sess_test"
    session.send_lock = asyncio.Lock()
    session.pending_text_inputs = []
    session.pending_tool_results = []
    session.pending_tool_context = None
    session.next_response_instructions = ""
    return session


class CoreTests(unittest.TestCase):
    def test_energy_vad_detects_turn(self) -> None:
        settings = Settings(vad_provider="energy")
        vad = EnergyVad(settings)
        event, utterance = vad.accept(pcm_tone(settings.sample_rate, 0.4))
        self.assertEqual(event, "speech_started")
        self.assertIsNone(utterance)

        event, utterance = vad.accept(pcm_silence(settings.sample_rate, 0.8))
        self.assertEqual(event, "speech_stopped")
        self.assertIsNotNone(utterance)
        self.assertGreaterEqual(utterance.duration_ms, settings.vad_min_utterance_ms)

    def test_build_energy_vad(self) -> None:
        settings = Settings(vad_provider="energy")
        self.assertIsInstance(build_vad(settings), EnergyVad)

    def test_parse_tool_calls(self) -> None:
        calls = BaseOpenAIChatProvider._parse_tool_calls(
            [
                {
                    "id": "call_1",
                    "function": {"name": "current_time", "arguments": "{}"},
                }
            ]
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "current_time")

    def test_llm_history_resets_after_idle_limit(self) -> None:
        provider = DummyChatProvider(Settings(hermes_history_idle_reset_seconds=10))
        provider.history = [
            {"role": "user", "content": "第一句"},
            {"role": "assistant", "content": "第一答"},
        ]
        provider.last_llm_call_started_at = 5.0

        provider._reset_history_if_idle(14.9)
        self.assertEqual(len(provider.history), 2)

        provider._reset_history_if_idle(15.0)
        self.assertEqual(provider.history, [])

    def test_tool_registry_executes_registered_tool(self) -> None:
        result = asyncio.run(ToolRegistry().execute("noop", "{}"))
        self.assertEqual(result.result, "noop completed")
        self.assertFalse(result.forwarded)

    def test_tool_registry_accepts_realtime_client_tool(self) -> None:
        registry = ToolRegistry()
        registry.set_client_tools(
            [
                {
                    "type": "function",
                    "name": "dance",
                    "description": "Play a selected Reachy Mini dance.",
                    "parameters": {
                        "type": "object",
                        "properties": {"dance": {"type": "string", "enum": ["happy"]}},
                        "required": ["dance"],
                        "additionalProperties": False,
                    },
                }
            ]
        )
        tool_names = [tool["function"]["name"] for tool in registry.openai_tools()]
        self.assertIn("dance", tool_names)

        result = asyncio.run(registry.execute("dance", '{"dance":"happy"}'))
        self.assertTrue(result.forwarded)
        self.assertEqual(result.mode, "client")
        self.assertEqual(result.arguments, {"dance": "happy"})

    def test_realtime_session_extracts_text_item(self) -> None:
        item = {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "你好"},
                {"type": "text", "text": "请点头"},
            ],
        }
        self.assertEqual(RealtimeSession._extract_text_from_item(item), "你好\n请点头")

    def test_realtime_turn_gate_serializes_sessions(self) -> None:
        async def run_test() -> list[str]:
            gate = asyncio.Lock()
            first = bare_session()
            second = bare_session()
            first.turn_gate = gate
            second.turn_gate = gate
            events: list[str] = []

            async def factory(name: str) -> None:
                events.append(f"{name}:start")
                await asyncio.sleep(0.01)
                events.append(f"{name}:end")

            await asyncio.gather(
                first._run_serialized_turn(lambda: factory("first")),
                second._run_serialized_turn(lambda: factory("second")),
            )
            return events

        self.assertEqual(
            asyncio.run(run_test()),
            ["first:start", "first:end", "second:start", "second:end"],
        )

    def test_decode_audio_append_rejects_invalid_chunks(self) -> None:
        session = bare_session()
        events = []

        async def fake_send(self, event):
            events.append(event)

        session._send = types.MethodType(fake_send, session)

        self.assertIsNone(asyncio.run(session._decode_audio_append({"audio": "not base64"})))
        self.assertEqual(events[-1]["error"]["code"], "invalid_audio_base64")

        odd = base64.b64encode(b"\x00").decode("ascii")
        self.assertIsNone(asyncio.run(session._decode_audio_append({"audio": odd})))
        self.assertEqual(events[-1]["error"]["code"], "invalid_audio_format")

        valid = base64.b64encode(b"\x00\x00").decode("ascii")
        self.assertEqual(asyncio.run(session._decode_audio_append({"audio": valid})), b"\x00\x00")

    def test_session_forwards_client_tool_call_event(self) -> None:
        class FakeLlm:
            async def chat(self, *args, **kwargs):
                return LLMResponse(
                    tool_calls=[ToolCall(id="call_1", name="dance", arguments='{"dance":"happy"}')]
                )

        session = bare_session()
        session.llm = FakeLlm()
        session.tools.set_client_tools(
            [
                {
                    "type": "function",
                    "name": "dance",
                    "description": "Queue a Reachy Mini dance.",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                }
            ]
        )
        events = []

        async def fake_send(self, event):
            events.append(event)

        session._send = types.MethodType(fake_send, session)
        answer = asyncio.run(
            session._ask_llm_with_tools("跳舞", response_id="resp_1", item_id="item_1")
        )

        self.assertEqual(answer, "")
        self.assertIsNotNone(session.pending_tool_context)
        self.assertEqual(events[0]["type"], "response.function_call_arguments.done")
        self.assertEqual(events[0]["name"], "dance")
        self.assertEqual(events[0]["call_id"], "call_1")
        self.assertEqual(events[0]["arguments"], '{"dance": "happy"}')

    def test_tool_result_turn_uses_returned_tool_output(self) -> None:
        class FakeLlm:
            def __init__(self) -> None:
                self.messages = None

            async def chat(self, *args, **kwargs):
                self.messages = kwargs["messages"]
                return LLMResponse(text="好了，已经开始跳舞。")

        session = bare_session()
        llm = FakeLlm()
        session.llm = llm
        session.pending_tool_context = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "跳舞"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "dance", "arguments": '{"dance":"happy"}'},
                    }
                ],
            },
        ]
        session.pending_tool_results = [
            {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'}
        ]
        sent = []

        async def fake_send_response(self, text, *, transcript, metrics=None):
            sent.append((text, transcript))

        session._send_response = types.MethodType(fake_send_response, session)
        asyncio.run(session._process_tool_result_turn())

        self.assertEqual(sent, [("好了，已经开始跳舞。", "好了，已经开始跳舞。")])
        self.assertEqual(llm.messages[-1]["role"], "tool")
        self.assertEqual(llm.messages[-1]["content"], '{"ok": true}')
        self.assertEqual(session.pending_tool_results, [])
        self.assertIsNone(session.pending_tool_context)

    def test_tts_text_is_split_on_punctuation(self) -> None:
        session = object.__new__(RealtimeSession)
        session.settings = Settings(tts_segment_min_chars=8, tts_segment_max_chars=14)
        segments = session._split_tts_segments("你好，这是第一句。然后继续第二句，还可以。")
        self.assertGreater(len(segments), 1)
        self.assertEqual("".join(segments), "你好，这是第一句。然后继续第二句，还可以。")

    def test_tts_text_strips_bracketed_expression_cues(self) -> None:
        session = object.__new__(RealtimeSession)
        session.settings = Settings(tts_strip_bracketed_cues=True)
        self.assertEqual(session._sanitize_tts_text("你好[呲牙]，我在。"), "你好，我在。")
        self.assertEqual(session._sanitize_tts_text("查询（杭州）的天气。"), "查询（杭州）的天气。")


if __name__ == "__main__":
    unittest.main()
