from __future__ import annotations

import asyncio
import base64
import math
import os
import stat
import tempfile
import time
import types
import unittest
from pathlib import Path

from fastapi import HTTPException

from hermes_sts.admin import (
    PreviewRequest,
    _conversation_payload,
    _diagnostics_payload,
    _preview_voice,
    _requires_rebuild,
    _settings_for_voice_profile,
    _settings_payload,
    _validate_settings_patch,
    _web_search_payload,
)
from hermes_sts.config import Settings
from hermes_sts.config_store import ConfigStore
from hermes_sts.conversation_store import ConversationStore
from hermes_sts.llm import BaseOpenAIChatProvider, HermesAgentProvider, LLMResponse, LLMToolCallDetected, ToolCall
from hermes_sts.realtime import RealtimeSession, TurnMetrics
from hermes_sts.tts import OmniVoiceEngine, QwenTtsCpp, TtsVoice, _stdin_text, build_tts
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
    session.session_voice = None
    session.next_response_voice = None
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

    def test_parse_legacy_function_call(self) -> None:
        calls = BaseOpenAIChatProvider._parse_message_tool_calls(
            {
                "content": None,
                "function_call": {
                    "name": "current_time",
                    "arguments": "{}",
                },
            }
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].id, "call_0")
        self.assertEqual(calls[0].name, "current_time")
        self.assertEqual(calls[0].arguments, "{}")

    def test_llm_system_prompt_uses_persona_label(self) -> None:
        prompt = BaseOpenAIChatProvider._system_prompt("你是端庄新闻播报员。")
        self.assertIn("当前人格和表达风格", prompt)
        self.assertIn("端庄新闻播报员", prompt)

    def test_llm_system_prompt_keeps_voice_description_out_of_spoken_text(self) -> None:
        prompt = BaseOpenAIChatProvider._system_prompt("保持温柔。")

        self.assertIn("不要输出 emoji", prompt)
        self.assertIn("不要写音色", prompt)
        self.assertIn("音色描述由 TTS 声音配置单独控制", prompt)

    def test_hermes_voice_no_think_prefixes_last_user_message(self) -> None:
        provider = HermesAgentProvider(Settings(hermes_voice_no_think=True))
        messages = [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": "你好"},
        ]

        prepared = provider._prepare_messages(messages)

        self.assertEqual(messages[-1]["content"], "你好")
        self.assertEqual(prepared[-1]["content"], "/no_think\n你好")

    def test_hermes_voice_no_think_can_be_disabled(self) -> None:
        provider = HermesAgentProvider(Settings(hermes_voice_no_think=False))
        messages = [{"role": "user", "content": "你好"}]

        self.assertEqual(provider._prepare_messages(messages), messages)

    def test_hermes_voice_no_think_does_not_duplicate_prefix(self) -> None:
        provider = HermesAgentProvider(Settings(hermes_voice_no_think=True))
        messages = [{"role": "user", "content": "/no_think\n你好"}]

        self.assertEqual(provider._prepare_messages(messages)[0]["content"], "/no_think\n你好")

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

    def test_tool_registry_openai_tools_are_canonical(self) -> None:
        first = ToolRegistry()
        first.set_client_tools(
            [
                {
                    "type": "function",
                    "name": "zeta",
                    "description": "Second by name.",
                    "parameters": {
                        "required": ["b", "a"],
                        "properties": {
                            "b": {"description": "B", "type": "string"},
                            "a": {"type": "string", "description": "A"},
                        },
                        "type": "object",
                    },
                },
                {
                    "type": "function",
                    "name": "alpha",
                    "description": "First by name.",
                    "parameters": {
                        "properties": {"x": {"type": "string", "description": "X"}},
                        "type": "object",
                    },
                },
            ]
        )
        second = ToolRegistry()
        second.set_client_tools(
            [
                {
                    "type": "function",
                    "name": "alpha",
                    "description": "First by name.",
                    "parameters": {
                        "type": "object",
                        "properties": {"x": {"description": "X", "type": "string"}},
                    },
                },
                {
                    "type": "function",
                    "name": "zeta",
                    "description": "Second by name.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "a": {"description": "A", "type": "string"},
                            "b": {"type": "string", "description": "B"},
                        },
                        "required": ["b", "a"],
                    },
                },
            ]
        )

        self.assertEqual(first.openai_tools(), second.openai_tools())
        self.assertEqual(
            [tool["function"]["name"] for tool in first.openai_tools()],
            ["alpha", "current_time", "noop", "zeta"],
        )

    def test_tool_registry_snapshot_groups_local_and_client_tools(self) -> None:
        registry = ToolRegistry()
        registry.set_client_tools(
            [
                {
                    "type": "function",
                    "name": "dance",
                    "description": "Queue a dance.",
                    "parameters": {
                        "type": "object",
                        "properties": {"dance": {"type": "string"}},
                        "required": ["dance"],
                    },
                }
            ]
        )

        snapshot = registry.snapshot()

        self.assertIn("current_time", [tool["name"] for tool in snapshot["local"]])
        self.assertEqual(snapshot["client"][0]["name"], "dance")
        self.assertEqual(snapshot["client"][0]["parameters_count"], 1)
        self.assertIs(snapshot["client"][0]["injected"], True)
        self.assertIsNone(snapshot["client"][0]["last_called_at"])

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

            async def ensure_active_conversation(self) -> str:
                return ""

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

            async def ensure_active_conversation(self) -> str:
                return ""

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

        async def fake_send_response(self, text, *, transcript, metrics=None, voice=None):
            sent.append((text, transcript))

        session._send_response = types.MethodType(fake_send_response, session)
        asyncio.run(session._process_tool_result_turn())

        self.assertEqual(sent, [("好了，已经开始跳舞。", "好了，已经开始跳舞。")])
        self.assertEqual(llm.messages[-1]["role"], "tool")
        self.assertEqual(llm.messages[-1]["content"], '{"ok": true}')
        self.assertEqual(session.pending_tool_results, [])
        self.assertIsNone(session.pending_tool_context)

    def test_mixed_local_and_client_tool_calls_keep_local_result_for_followup(self) -> None:
        class FakeLlm:
            async def chat(self, *args, **kwargs):
                return LLMResponse(
                    tool_calls=[
                        ToolCall(id="call_local", name="current_time", arguments="{}"),
                        ToolCall(id="call_client", name="dance", arguments='{"dance":"happy"}'),
                    ]
                )

            async def ensure_active_conversation(self) -> str:
                return ""

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
        answer = asyncio.run(session._ask_llm_with_tools("报时并跳舞", response_id="resp_1", item_id="item_1"))

        self.assertEqual(answer, "")
        self.assertEqual(events[0]["name"], "dance")
        self.assertIsNotNone(session.pending_tool_context)
        tool_messages = [msg for msg in session.pending_tool_context or [] if msg.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0]["tool_call_id"], "call_local")

    def test_agent_wait_does_not_synthesize_filler_when_disabled(self) -> None:
        class SlowLlm:
            async def chat(self, *args, **kwargs):
                await asyncio.sleep(0.02)
                return LLMResponse(text="最终回答")

            async def ensure_active_conversation(self) -> str:
                return ""

        async def run() -> tuple[list[str], list[str]]:
            session = bare_session()
            session.settings = Settings(
                hermes_max_fillers=0,
                hermes_first_filler_delay_seconds=0.001,
                hermes_filler_interval_seconds=0.001,
                hermes_agent_max_wait_seconds=1.0,
            )
            session.llm = SlowLlm()
            synthesized: list[str] = []
            sent_segments: list[str] = []

            async def fake_synthesize(self, text, *, metrics=None, voice=None):
                synthesized.append(text)
                return b"\x00\x00"

            async def fake_send_audio_segment(self, text, *, response_id, item_id, metrics=None, voice=None):
                sent_segments.append(text)

            async def fake_send_pcm_segment(self, pcm16, *, response_id, item_id, metrics=None):
                sent_segments.append("pcm")

            async def fake_send(self, event):
                pass

            session._synthesize_tts = types.MethodType(fake_synthesize, session)
            session._send_audio_segment = types.MethodType(fake_send_audio_segment, session)
            session._send_pcm_segment = types.MethodType(fake_send_pcm_segment, session)
            session._send = types.MethodType(fake_send, session)

            await session._respond_with_agent_wait("你好")
            return synthesized, sent_segments

        synthesized, sent_segments = asyncio.run(run())

        self.assertEqual(synthesized, ["最终回答"])
        self.assertFalse(any(text.startswith("我想") or "稍等" in text for text in synthesized))
        self.assertEqual(sent_segments, ["pcm"])

    def test_send_audio_segment_uses_streaming_tts_when_available(self) -> None:
        class StreamingTts:
            supports_streaming = True

            def __init__(self):
                self.synthesize_calls = 0
                self.stream_voices: list[str] = []

            async def synthesize(self, text, *, voice=None):
                self.synthesize_calls += 1
                return b"\x00\x00"

            async def stream_pcm(self, text, *, voice=None):
                self.stream_voices.append(voice.speaker)
                yield b"\x01\x00" * 10
                yield b"\x02\x00" * 10

        async def run() -> tuple[int, list[str], list[int]]:
            session = bare_session()
            session.settings = Settings(qwentts_cpp_speaker="ui_voice")
            tts = StreamingTts()
            session.tts = tts
            session.active_response_id = "resp"
            sent: list[int] = []

            async def fake_send_pcm_segment(self, pcm16, *, response_id, item_id, metrics=None):
                sent.append(len(pcm16))

            session._send_pcm_segment = types.MethodType(fake_send_pcm_segment, session)
            await session._send_audio_segment("你好", response_id="resp", item_id="item")
            return tts.synthesize_calls, tts.stream_voices, sent

        synthesize_calls, voices, sent = asyncio.run(run())

        self.assertEqual(synthesize_calls, 0)
        self.assertEqual(voices, ["ui_voice"])
        self.assertEqual(sent, [20, 20])

    def test_llm_streaming_sends_complete_sentences_to_tts(self) -> None:
        class StreamingLlm:
            async def ensure_active_conversation(self) -> str:
                return "conv"

            async def stream_text(self, *args, **kwargs):
                yield "第一句"
                yield "。第二句。"

        async def run() -> tuple[list[str], list[str]]:
            session = bare_session()
            session.settings = Settings(llm_streaming_enabled=True, qwentts_cpp_speaker="ui_voice")
            session.llm = StreamingLlm()
            session.active_response_id = None
            sent_segments: list[str] = []
            done: list[str] = []

            async def fake_send_tts_segment(self, text, *, response_id, item_id, metrics=None, voice=None):
                sent_segments.append(text)

            async def fake_send_response_created(self, response_id, item_id, metrics=None):
                self.active_response_id = response_id

            async def fake_send_response_done(self, *, response_id, item_id, transcript):
                done.append(transcript)

            session._send_tts_segment = types.MethodType(fake_send_tts_segment, session)
            session._send_response_created = types.MethodType(fake_send_response_created, session)
            session._send_response_done = types.MethodType(fake_send_response_done, session)

            handled = await session._respond_with_llm_stream("你好", voice=TtsVoice(speaker="ui_voice"))
            return sent_segments if handled else [], done

        sent_segments, done = asyncio.run(run())

        self.assertEqual(sent_segments, ["第一句。", "第二句。"])
        self.assertEqual(done, ["第一句。第二句。"])

    def test_llm_streaming_tool_call_falls_back_before_speech(self) -> None:
        class ToolStreamingLlm:
            async def ensure_active_conversation(self) -> str:
                return "conv"

            async def stream_text(self, *args, **kwargs):
                raise LLMToolCallDetected("tool")
                yield ""

        async def run() -> bool:
            session = bare_session()
            session.settings = Settings(llm_streaming_enabled=True)
            session.llm = ToolStreamingLlm()
            return await session._respond_with_llm_stream("开灯")

        self.assertFalse(asyncio.run(run()))

    def test_streaming_tool_call_fallback_executes_tools(self) -> None:
        class ToolFallbackLlm:
            def __init__(self) -> None:
                self.chat_calls = 0
                self.tool_result = ""

            async def ensure_active_conversation(self) -> str:
                return "conv"

            async def stream_text(self, *args, **kwargs):
                raise LLMToolCallDetected("tool")
                yield ""

            async def chat(self, *args, **kwargs):
                self.chat_calls += 1
                if self.chat_calls == 1:
                    return LLMResponse(tool_calls=[ToolCall(id="call_1", name="noop", arguments="{}")])
                messages = kwargs["messages"]
                self.tool_result = messages[-1]["content"]
                return LLMResponse(text="工具已执行。")

        async def run() -> tuple[list[str], ToolFallbackLlm]:
            session = bare_session()
            llm = ToolFallbackLlm()
            session.settings = Settings(llm_streaming_enabled=True, hermes_first_filler_delay_seconds=0.05)
            session.llm = llm
            sent: list[str] = []

            async def fake_send_text_segments(self, text, *, response_id, item_id, metrics=None, voice=None):
                sent.append(text)

            async def fake_send_response_created(self, response_id, item_id, metrics=None):
                self.active_response_id = response_id

            async def fake_send_response_done(self, *, response_id, item_id, transcript):
                pass

            session._send_text_segments = types.MethodType(fake_send_text_segments, session)
            session._send_response_created = types.MethodType(fake_send_response_created, session)
            session._send_response_done = types.MethodType(fake_send_response_done, session)
            await session._respond_with_agent_wait("测试工具")
            return sent, llm

        sent, llm = asyncio.run(run())

        self.assertEqual(sent, ["工具已执行。"])
        self.assertEqual(llm.chat_calls, 2)
        self.assertEqual(llm.tool_result, "noop completed")

    def test_tool_system_prompt_keeps_voice_description_out_of_spoken_text(self) -> None:
        session = object.__new__(RealtimeSession)
        session.settings = Settings()
        session.instructions = ""
        session.next_response_instructions = ""

        prompt = session._tool_system_prompt(instructions="保持温柔。")

        self.assertIn("不要输出 emoji", prompt)
        self.assertIn("不要写音色", prompt)
        self.assertIn("音色描述由 TTS 声音配置单独控制", prompt)

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
        self.assertEqual(session._sanitize_tts_text("答对啦！😊✨"), "答对啦！")

    def test_tts_audio_is_trimmed_when_qwen_runs_long(self) -> None:
        session = bare_session()
        session.settings = Settings(tts_max_audio_seconds=18.0)
        pcm16 = b"\x00\x00" * session.settings.sample_rate * 30

        trimmed = session._limit_tts_audio("短句", pcm16, voice=TtsVoice.from_settings(session.settings))

        self.assertEqual(len(trimmed), session.settings.sample_rate * 8 * 2)

    def test_tts_split_flushes_first_sentence_for_fast_first_audio(self) -> None:
        session = object.__new__(RealtimeSession)
        session.settings = Settings(tts_segment_min_chars=8, tts_segment_max_chars=48)

        segments = session._split_tts_segments("哈哈，答错啦！正确答案是水喔。要不要再来一题？")

        self.assertEqual(segments[0], "哈哈，答错啦！")
        self.assertGreater(len(segments), 1)

    def test_build_qwentts_cpp_requires_runtime_paths(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "QWENTTS_CPP_BIN"):
            build_tts(
                Settings(
                    tts_provider="qwen3tts",
                    qwentts_cpp_bin="",
                    qwentts_cpp_model="",
                    qwentts_cpp_codec="",
                )
            )

    def test_qwentts_cpp_subprocess_returns_resampled_pcm16(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            marker_path = tmp_path / "marker.txt"
            fake_bin = tmp_path / "fake-qwen-tts"
            fake_bin.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import os, sys, wave",
                        "out = sys.argv[sys.argv.index('-o') + 1]",
                        "text = sys.stdin.buffer.read().decode('utf-8')",
                        f"open({str(marker_path)!r}, 'w').write(os.environ.get('GGML_BACKEND', '') + '|' + text)",
                        "with wave.open(out, 'wb') as wf:",
                        "    wf.setnchannels(1)",
                        "    wf.setsampwidth(2)",
                        "    wf.setframerate(24000)",
                        "    wf.writeframes((b'\\x01\\x00') * 2400)",
                    ]
                ),
                encoding="utf-8",
            )
            fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IXUSR)
            model = tmp_path / "talker.gguf"
            codec = tmp_path / "codec.gguf"
            model.touch()
            codec.touch()

            settings = Settings(
                sample_rate=16000,
                qwentts_cpp_bin=str(fake_bin),
                qwentts_cpp_model=str(model),
                qwentts_cpp_base_model=str(model),
                qwentts_cpp_customvoice_model=str(model),
                qwentts_cpp_codec=str(codec),
                qwentts_cpp_backend="Vulkan0",
                qwentts_cpp_max_new_frames=384,
            )
            pcm16 = QwenTtsCpp(settings)._synthesize_sync("你好")

            self.assertEqual(marker_path.read_text(encoding="utf-8"), "Vulkan0|你好\n")
            self.assertGreater(len(pcm16), 0)
            self.assertEqual(len(pcm16) % 2, 0)
            self.assertLess(len(pcm16), 2400 * 2)
            self.assertIn("--max-new", QwenTtsCpp(settings)._command(tmp_path / "out.wav"))
            self.assertIn("384", QwenTtsCpp(settings)._command(tmp_path / "out.wav"))

    def test_qwentts_cpp_streams_stdout_wav_as_resampled_pcm16(self) -> None:
        async def collect(provider: QwenTtsCpp) -> bytes:
            chunks: list[bytes] = []
            async for chunk in provider.stream_pcm("你好", voice=TtsVoice.from_settings(provider.settings)):
                chunks.append(chunk)
            return b"".join(chunks)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "fake-qwen-tts"
            fake_bin.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import struct, sys, wave",
                        "sys.stdin.buffer.read()",
                        "out = sys.argv[sys.argv.index('-o') + 1]",
                        "raw = (b'\\x01\\x00') * 4800",
                        "if out == '-':",
                        "    w = sys.stdout.buffer",
                        "    w.write(b'RIFF' + struct.pack('<I', 0x7fffffff) + b'WAVE')",
                        "    w.write(b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, 24000, 48000, 2, 16))",
                        "    w.write(b'data' + struct.pack('<I', 0x7fffffff))",
                        "    w.write(raw)",
                        "    w.flush()",
                        "else:",
                        "    with wave.open(out, 'wb') as wf:",
                        "        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(24000); wf.writeframes(raw)",
                    ]
                ),
                encoding="utf-8",
            )
            fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IXUSR)
            model = tmp_path / "talker.gguf"
            codec = tmp_path / "codec.gguf"
            model.touch()
            codec.touch()
            settings = Settings(
                sample_rate=16000,
                qwentts_cpp_bin=str(fake_bin),
                qwentts_cpp_model=str(model),
                qwentts_cpp_codec=str(codec),
            )

            pcm16 = asyncio.run(collect(QwenTtsCpp(settings)))

        self.assertGreater(len(pcm16), 0)
        self.assertEqual(len(pcm16) % 2, 0)
        self.assertLess(len(pcm16), 4800 * 2)

    def test_effective_tts_voice_uses_settings_as_single_source(self) -> None:
        session = bare_session()
        session.settings = Settings(
            tts_voice_source="settings",
            qwentts_cpp_speaker="ui_voice",
        )
        session.session_voice = TtsVoice(speaker="ws_voice")
        self.assertEqual(session._effective_tts_voice().speaker, "ui_voice")
        self.assertEqual(
            session._effective_tts_voice(TtsVoice(speaker="response_voice")).speaker,
            "ui_voice",
        )

        session.settings = Settings(
            tts_voice_source="ws",
            qwentts_cpp_speaker="ui_voice",
        )
        self.assertEqual(session._effective_tts_voice().speaker, "ui_voice")
        self.assertEqual(
            session._effective_tts_voice(TtsVoice(speaker="response_voice")).speaker,
            "ui_voice",
        )

    def test_realtime_persona_source_switch_selects_settings_or_ws(self) -> None:
        session = bare_session()
        session.instructions = "WS profile persona"

        session.settings = Settings(
            sts_persona_source="settings",
            sts_persona_preset="night_copilot",
        )
        self.assertIn("夜航副驾", session._effective_instructions())
        self.assertNotIn("WS profile", session._effective_instructions("temporary"))

        session.settings = Settings(
            sts_persona_source="ws",
            sts_persona_preset="operator",
        )
        self.assertEqual(session._effective_instructions(), "WS profile persona")
        self.assertIn("Response-specific instructions", session._effective_instructions("one turn"))

        session.instructions = ""
        self.assertIn("可靠", session._effective_instructions())

    def test_synthesize_tts_ignores_ws_voice(self) -> None:
        class FakeTts:
            def __init__(self) -> None:
                self.voices = []

            async def synthesize(self, text, *, voice=None):
                self.voices.append(voice.speaker)
                return b"\x00\x00"

        session = bare_session()
        session.settings = Settings(
            tts_voice_source="ws",
            qwentts_cpp_speaker="ui_voice",
        )
        session.tts = FakeTts()

        pcm16 = asyncio.run(session._synthesize_tts("你好", voice=TtsVoice(speaker="bad_ws")))
        self.assertEqual(pcm16, b"\x00\x00")
        self.assertEqual(session.tts.voices, ["ui_voice"])

    def test_realtime_turn_voice_stays_on_settings_voice(self) -> None:
        class FakeTts:
            def __init__(self) -> None:
                self.voices = []

            async def synthesize(self, text, *, voice=None):
                self.voices.append(voice.speaker)
                return b"\x00\x00"

        session = bare_session()
        session.settings = Settings(
            tts_voice_source="ws",
            qwentts_cpp_speaker="ui_voice",
        )
        session.tts = FakeTts()
        voice = session._turn_tts_voice(TtsVoice(speaker="response_voice"))
        asyncio.run(session._synthesize_tts("等待", voice=voice))
        session.session_voice = TtsVoice(speaker="changed_ws_voice")
        asyncio.run(session._synthesize_tts("正式回答", voice=voice))

        self.assertEqual(session.tts.voices, ["ui_voice", "ui_voice"])

    def test_qwen3tts_command_accepts_voice_clone_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "qwen-tts"
            model = tmp_path / "talker.gguf"
            codec = tmp_path / "codec.gguf"
            fake_bin.touch()
            model.touch()
            codec.touch()
            settings = Settings(
                qwentts_cpp_bin=str(fake_bin),
                qwentts_cpp_model=str(model),
                qwentts_cpp_codec=str(codec),
                qwentts_cpp_seed=42,
            )
            provider = QwenTtsCpp(settings)
            speaker_cmd = provider._command(
                tmp_path / "speaker.wav",
                voice=TtsVoice(speaker="vivian"),
            )
            clone_cmd = provider._command(
                tmp_path / "out.wav",
                voice=TtsVoice(
                    speaker="vivian",
                    instruct="warm tone",
                    ref_wav="/tmp/ref.wav",
                    ref_text="/tmp/ref.txt",
                    ref_spk="/tmp/ref.spk",
                    ref_rvq="/tmp/ref.rvq",
                ),
            )
            ref_wav_cmd = provider._command(
                tmp_path / "ref_wav.wav",
                voice=TtsVoice(ref_wav="/tmp/ref.wav"),
            )

        self.assertIn("--speaker", speaker_cmd)
        self.assertIn("vivian", speaker_cmd)
        self.assertIn("--seed", speaker_cmd)
        self.assertIn("42", speaker_cmd)
        self.assertNotIn("--speaker", clone_cmd)
        self.assertIn("--instruct", clone_cmd)
        self.assertIn("warm tone", clone_cmd)
        self.assertNotIn("--ref-wav", clone_cmd)
        self.assertNotIn("/tmp/ref.wav", clone_cmd)
        self.assertIn("--ref-text", clone_cmd)
        self.assertIn("/tmp/ref.txt", clone_cmd)
        self.assertIn("--ref-spk", clone_cmd)
        self.assertIn("/tmp/ref.spk", clone_cmd)
        self.assertIn("--ref-rvq", clone_cmd)
        self.assertIn("/tmp/ref.rvq", clone_cmd)
        self.assertIn("--ref-wav", ref_wav_cmd)
        self.assertIn("/tmp/ref.wav", ref_wav_cmd)

    def test_omnivoice_command_supports_auto_design_and_encoded_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "omnivoice-tts"
            model = tmp_path / "omnivoice-base-Q8_0.gguf"
            codec = tmp_path / "omnivoice-tokenizer-F32.gguf"
            for path in (fake_bin, model, codec):
                path.touch()
            settings = Settings(
                tts_provider="omnivoice",
                omnivoice_bin=str(fake_bin),
                omnivoice_model=str(model),
                omnivoice_codec=str(codec),
                omnivoice_lang="Chinese",
                omnivoice_seed=77,
                omnivoice_chunk_duration_seconds=12.5,
                omnivoice_chunk_threshold_seconds=25.0,
            )
            provider = OmniVoiceEngine(settings)

            auto_cmd = provider._command(tmp_path / "auto.wav", voice=TtsVoice.from_settings(settings))
            design_cmd = provider._command(
                tmp_path / "design.wav",
                voice=TtsVoice(engine="omnivoice", mode="design", instruct="warm clear voice"),
            )
            clone_cmd = provider._command(
                tmp_path / "clone.wav",
                voice=TtsVoice(
                    engine="omnivoice",
                    mode="clone",
                    ref_wav="/tmp/ref.wav",
                    ref_text="/tmp/ref.txt",
                    omnivoice_ref_rvq="/tmp/ref.rvq",
                ),
            )

        self.assertIn("--lang", auto_cmd)
        self.assertIn("Chinese", auto_cmd)
        self.assertIn("--seed", auto_cmd)
        self.assertIn("77", auto_cmd)
        self.assertIn("--chunk-duration", auto_cmd)
        self.assertIn("12.5", auto_cmd)
        self.assertIn("--instruct", design_cmd)
        self.assertIn("warm clear voice", design_cmd)
        self.assertIn("--ref-rvq", clone_cmd)
        self.assertIn("/tmp/ref.rvq", clone_cmd)
        self.assertIn("--ref-text", clone_cmd)
        self.assertIn("/tmp/ref.txt", clone_cmd)
        self.assertNotIn("--ref-wav", clone_cmd)
        self.assertNotIn("/tmp/ref.wav", clone_cmd)

    def test_qwen3tts_stdin_text_is_newline_terminated(self) -> None:
        self.assertEqual(_stdin_text("你好"), "你好\n".encode("utf-8"))
        self.assertEqual(_stdin_text("你好\n"), "你好\n".encode("utf-8"))

    def test_qwen3tts_command_uses_voice_snapshot_for_deterministic_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "qwen-tts"
            model = tmp_path / "talker.gguf"
            next_model = tmp_path / "next-talker.gguf"
            codec = tmp_path / "codec.gguf"
            next_codec = tmp_path / "next-codec.gguf"
            for path in (fake_bin, model, next_model, codec, next_codec):
                path.touch()
            settings = Settings(
                qwentts_cpp_bin=str(fake_bin),
                qwentts_cpp_model=str(model),
                qwentts_cpp_codec=str(codec),
                qwentts_cpp_seed=123,
            )
            provider = QwenTtsCpp(settings)
            voice = TtsVoice.from_settings(settings)
            object.__setattr__(settings, "qwentts_cpp_model", str(next_model))
            object.__setattr__(settings, "qwentts_cpp_codec", str(next_codec))
            object.__setattr__(settings, "qwentts_cpp_seed", 999)

            cmd = provider._command(tmp_path / "snapshot.wav", voice=voice)

        self.assertIn(str(model), cmd)
        self.assertNotIn(str(next_model), cmd)
        self.assertIn(str(codec), cmd)
        self.assertNotIn(str(next_codec), cmd)
        seed_index = cmd.index("--seed") + 1
        self.assertEqual(cmd[seed_index], "123")

    def test_qwen3tts_command_reads_hot_settings_without_provider_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "qwen-tts"
            model = tmp_path / "talker.gguf"
            next_model = tmp_path / "next-talker.gguf"
            codec = tmp_path / "codec.gguf"
            next_codec = tmp_path / "next-codec.gguf"
            for path in (fake_bin, model, next_model, codec, next_codec):
                path.touch()
            settings = Settings(
                qwentts_cpp_bin=str(fake_bin),
                qwentts_cpp_model=str(model),
                qwentts_cpp_codec=str(codec),
                qwentts_cpp_seed=123,
                qwentts_cpp_extra_args="",
            )
            provider = QwenTtsCpp(settings)
            object.__setattr__(settings, "qwentts_cpp_model", str(next_model))
            object.__setattr__(settings, "qwentts_cpp_codec", str(next_codec))
            object.__setattr__(settings, "qwentts_cpp_seed", 999)

            cmd = provider._command(tmp_path / "hot.wav")

        self.assertIn(str(next_model), cmd)
        self.assertNotIn(str(model), cmd)
        self.assertIn(str(next_codec), cmd)
        self.assertNotIn(str(codec), cmd)
        self.assertEqual(cmd[cmd.index("--seed") + 1], "999")

    def test_config_store_sqlite_overrides_env_and_maps_qwen_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "settings.sqlite3"
            custom_model = Path(tmp) / "customvoice.gguf"
            base_model = Path(tmp) / "base.gguf"
            codec = Path(tmp) / "codec.gguf"
            for path in (custom_model, base_model, codec):
                path.touch()

            store = ConfigStore(db)
            store.set_settings(
                {
                    "tts_provider": "qwen3tts",
                    "qwentts_cpp_voice_mode": "preset",
                    "qwentts_cpp_voice_preset": "vivian",
                    "qwentts_cpp_base_model": str(base_model),
                    "qwentts_cpp_customvoice_model": str(custom_model),
                    "qwentts_cpp_codec": str(codec),
                }
            )

            settings = store.load_settings()
            raw = store.settings_dict()

        self.assertEqual(settings.tts_provider, "qwen3tts")
        self.assertEqual(settings.qwentts_cpp_voice_mode, "preset")
        self.assertEqual(settings.qwentts_cpp_speaker, "vivian")
        self.assertEqual(settings.qwentts_cpp_model, str(custom_model))
        self.assertEqual(settings.hermes_max_fillers, 0)
        self.assertEqual(raw["qwentts_cpp_model"], str(custom_model))
        self.assertEqual(raw["qwentts_cpp_speaker"], "vivian")
        self.assertEqual(raw["qwentts_cpp_instruct"], "")

    def test_config_store_migrates_legacy_tts_segment_defaults_for_speed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "settings.sqlite3")
            store.set_settings({"tts_segment_min_chars": 24, "tts_segment_max_chars": 90})
            store.ensure_defaults()

            settings = store.load_settings()

        self.assertEqual(settings.tts_segment_min_chars, 8)
        self.assertEqual(settings.tts_segment_max_chars, 48)

    def test_config_store_migrates_empty_qwentts_extra_args_for_streaming_speed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "settings.sqlite3")
            store.set_settings({"qwentts_cpp_extra_args": ""})
            store.ensure_defaults()

            settings = store.load_settings()

        self.assertIn("--codec-chunk-dur 0.5", settings.qwentts_cpp_extra_args)
        self.assertIn("--codec-left-dur 0.1", settings.qwentts_cpp_extra_args)

    def test_config_store_migrates_legacy_web_search_provider_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "settings.sqlite3")
            store.set_settings({"web_search_providers": "tavily,duckduckgo,searxng"})
            store.ensure_defaults()
            migrated = store.load_settings()

            custom_store = ConfigStore(Path(tmp) / "custom.sqlite3")
            custom_store.set_settings({"web_search_providers": "duckduckgo,tavily"})
            custom_store.ensure_defaults()
            custom = custom_store.load_settings()

        self.assertEqual(migrated.web_search_providers, "tavily,brave,searxng,duckduckgo")
        self.assertEqual(custom.web_search_providers, "duckduckgo,tavily")

    def test_config_store_llm_profiles_apply_to_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.sqlite3")
            profile = {
                "id": "test_openai",
                "name": "Test OpenAI",
                "provider": "openai_compatible",
                "base_url": "http://llm.example/v1",
                "model": "test-model",
                "api_key": "secret",
                "max_tokens": 123,
                "timeout_seconds": 12.5,
                "voice_no_think": False,
                "wait_fillers_enabled": True,
                "max_wait_seconds": 22,
                "fallback_enabled": False,
                "web_search_enabled": True,
                "notes": "test",
            }
            store.upsert_llm_profile(profile)
            saved = store.llm_profile("test_openai")
            assert saved is not None

            store.set_settings(store.settings_for_llm_profile(saved))
            settings = store.load_settings()

        self.assertEqual(settings.active_llm_profile_id, "test_openai")
        self.assertEqual(settings.llm_provider, "openai_compatible")
        self.assertEqual(settings.llm_base_url, "http://llm.example/v1")
        self.assertEqual(settings.llm_model, "test-model")
        self.assertEqual(settings.llm_max_tokens, 123)
        self.assertEqual(settings.hermes_max_fillers, 1)
        self.assertTrue(settings.web_search_enabled)

    def test_active_llm_profile_overrides_stale_wait_filler_setting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.sqlite3")
            profile = store.llm_profile("hermes_default")
            assert profile is not None
            profile["wait_fillers_enabled"] = False
            store.upsert_llm_profile(profile)
            store.set_settings(
                {
                    "active_llm_profile_id": "hermes_default",
                    "hermes_max_fillers": 2,
                }
            )

            settings = store.load_settings()

        self.assertEqual(settings.active_llm_profile_id, "hermes_default")
        self.assertEqual(settings.hermes_max_fillers, 0)

    def test_web_search_toggle_syncs_active_llm_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.sqlite3")

            store.set_settings({"web_search_enabled": True})
            enabled = store.load_settings()
            profile_enabled = store.llm_profile(enabled.active_llm_profile_id)
            assert profile_enabled is not None

            store.set_settings({"web_search_enabled": False})
            disabled = store.load_settings()
            profile_disabled = store.llm_profile(disabled.active_llm_profile_id)
            assert profile_disabled is not None

        self.assertTrue(enabled.web_search_enabled)
        self.assertTrue(profile_enabled["web_search_enabled"])
        self.assertFalse(disabled.web_search_enabled)
        self.assertFalse(profile_disabled["web_search_enabled"])

    def test_config_store_keeps_qwen_legacy_fields_in_sync_when_switching_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "settings.sqlite3"
            custom_model = Path(tmp) / "customvoice.gguf"
            design_model = Path(tmp) / "design.gguf"
            base_model = Path(tmp) / "base.gguf"
            for path in (custom_model, design_model, base_model):
                path.touch()

            store = ConfigStore(db)
            store.set_settings(
                {
                    "qwentts_cpp_voice_mode": "preset",
                    "qwentts_cpp_voice_preset": "vivian",
                    "qwentts_cpp_base_model": str(base_model),
                    "qwentts_cpp_customvoice_model": str(custom_model),
                    "qwentts_cpp_voicedesign_model": str(design_model),
                }
            )
            preset = store.settings_dict()
            store.set_settings(
                {
                    "qwentts_cpp_voice_mode": "design",
                    "qwentts_cpp_voice_design": "cool, clear voice",
                }
            )
            design = store.settings_dict()
            store.set_settings({"qwentts_cpp_voice_mode": "default"})
            default = store.settings_dict()

        self.assertEqual(preset["qwentts_cpp_model"], str(custom_model))
        self.assertEqual(preset["qwentts_cpp_speaker"], "vivian")
        self.assertEqual(design["qwentts_cpp_model"], str(design_model))
        self.assertEqual(design["qwentts_cpp_instruct"], "cool, clear voice")
        self.assertEqual(design["qwentts_cpp_speaker"], "")
        self.assertEqual(default["qwentts_cpp_model"], str(base_model))
        self.assertEqual(default["qwentts_cpp_speaker"], "")
        self.assertEqual(default["qwentts_cpp_instruct"], "")

    def test_qwen_voice_and_model_settings_are_hot_updated_without_restart(self) -> None:
        hot_keys = {
            "qwentts_cpp_voice_mode": "design",
            "qwentts_cpp_voice_preset": "vivian",
            "qwentts_cpp_voice_design": "warm clear voice",
            "qwentts_cpp_clone_voice_id": "voice_1",
            "qwentts_cpp_base_model": "/tmp/base.gguf",
            "qwentts_cpp_customvoice_model": "/tmp/custom.gguf",
            "qwentts_cpp_voicedesign_model": "/tmp/design.gguf",
            "qwentts_cpp_codec": "/tmp/codec.gguf",
            "qwentts_cpp_backend": "Vulkan0",
            "qwentts_cpp_seed": 123,
            "qwentts_cpp_max_new_frames": 512,
            "qwentts_cpp_extra_args": "--codec-chunk-dur 0.5",
        }

        self.assertFalse(_requires_rebuild(hot_keys))
        self.assertFalse(_requires_rebuild({"omnivoice_voice_mode": "design", "omnivoice_seed": 99}))
        self.assertTrue(_requires_rebuild({"tts_provider": "sherpa_kokoro"}))
        self.assertTrue(_requires_rebuild({"qwentts_cpp_bin": "/tmp/qwen-tts"}))
        self.assertTrue(_requires_rebuild({"omnivoice_bin": "/tmp/omnivoice-tts"}))

    def test_config_store_falls_back_to_base_when_optional_qwen_voice_model_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "settings.sqlite3"
            base_model = Path(tmp) / "base.gguf"
            codec = Path(tmp) / "codec.gguf"
            base_model.touch()
            codec.touch()

            store = ConfigStore(db)
            store.set_settings(
                {
                    "tts_provider": "qwen3tts",
                    "qwentts_cpp_voice_mode": "preset",
                    "qwentts_cpp_voice_preset": "vivian",
                    "qwentts_cpp_base_model": str(base_model),
                    "qwentts_cpp_customvoice_model": str(Path(tmp) / "missing-customvoice.gguf"),
                    "qwentts_cpp_codec": str(codec),
                }
            )

            settings = store.load_settings()

        self.assertEqual(settings.qwentts_cpp_voice_mode, "preset")
        self.assertEqual(settings.qwentts_cpp_speaker, "")
        self.assertEqual(settings.qwentts_cpp_model, str(base_model))

    def test_config_store_keeps_clone_mode_without_selected_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "settings.sqlite3"
            base_model = Path(tmp) / "base.gguf"
            codec = Path(tmp) / "codec.gguf"
            base_model.touch()
            codec.touch()

            store = ConfigStore(db)
            store.set_settings(
                {
                    "tts_provider": "qwen3tts",
                    "qwentts_cpp_voice_mode": "clone",
                    "qwentts_cpp_clone_voice_id": "",
                    "qwentts_cpp_base_model": str(base_model),
                    "qwentts_cpp_codec": str(codec),
                }
            )

            settings = store.load_settings()

        self.assertEqual(settings.qwentts_cpp_voice_mode, "clone")
        self.assertEqual(settings.qwentts_cpp_model, str(base_model))
        self.assertEqual(settings.qwentts_cpp_ref_wav, "")
        self.assertEqual(settings.qwentts_cpp_ref_spk, "")

    def test_config_store_maps_omnivoice_modes_without_touching_qwen_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "settings.sqlite3"
            ref_wav = Path(tmp) / "reference.wav"
            ref_txt = Path(tmp) / "reference.txt"
            ref_rvq = Path(tmp) / "reference.rvq"
            for path in (ref_wav, ref_txt, ref_rvq):
                path.touch()
            store = ConfigStore(db)
            store.upsert_voice(
                {
                    "id": "voice_omni",
                    "name": "Omni Clone",
                    "provider": "qwen3tts",
                    "mode": "clone",
                    "ref_wav": str(ref_wav),
                    "ref_text": str(ref_txt),
                    "ref_rvq": "/tmp/qwen.rvq",
                    "omnivoice_ref_rvq": str(ref_rvq),
                }
            )
            store.set_settings(
                {
                    "tts_provider": "omnivoice",
                    "qwentts_cpp_voice_mode": "preset",
                    "qwentts_cpp_voice_preset": "vivian",
                    "omnivoice_voice_mode": "clone",
                    "omnivoice_clone_voice_id": "voice_omni",
                }
            )

            settings = store.load_settings()
            raw = store.settings_dict()

        self.assertEqual(settings.tts_provider, "omnivoice")
        self.assertEqual(settings.omnivoice_voice_mode, "clone")
        self.assertEqual(settings.omnivoice_ref_wav, str(ref_wav))
        self.assertEqual(settings.omnivoice_ref_text, str(ref_txt))
        self.assertEqual(settings.omnivoice_ref_rvq, str(ref_rvq))
        self.assertEqual(settings.qwentts_cpp_voice_mode, "preset")
        self.assertEqual(raw["omnivoice_ref_rvq"], str(ref_rvq))

    def test_admin_state_exposes_ui_required_values_and_validates_qwen_speaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_db = os.environ.get("HERMES_STS_CONFIG_DB")
            os.environ["HERMES_STS_CONFIG_DB"] = str(Path(tmp) / "admin.sqlite3")
            try:
                store = ConfigStore.default()
                payload = _settings_payload(store.load_settings(), store)
                with self.assertRaises(HTTPException) as bad_speaker:
                    _validate_settings_patch({"qwentts_cpp_voice_preset": "not_a_real_speaker"})
                with self.assertRaises(HTTPException) as bad_omni_mode:
                    _validate_settings_patch({"omnivoice_voice_mode": "preset"})
            finally:
                if old_db is None:
                    os.environ.pop("HERMES_STS_CONFIG_DB", None)
                else:
                    os.environ["HERMES_STS_CONFIG_DB"] = old_db

        values = payload["values"]
        self.assertIn("sts_persona_source", values)
        self.assertIn("sts_persona_preset", values)
        self.assertIn("sts_persona_custom", values)
        self.assertIn("tts_voice_source", values)
        self.assertIn("tts_segment_max_chars", values)
        self.assertIn("qwentts_cpp_seed", values)
        self.assertIn("omnivoice_voice_mode", values)
        self.assertIn("omnivoice_model", values)
        self.assertIn("hermes_history_max_messages", values)
        self.assertIn("hermes_history_max_chars", values)
        self.assertIn("hermes_history_idle_reset_seconds", values)
        self.assertIn("hermes_agent_max_wait_seconds", values)
        self.assertIn("hermes_filler_interval_seconds", values)
        self.assertIn("hermes_max_fillers", values)
        self.assertIn("brave_api_key", values)
        self.assertIn("brave_base_url", values)
        self.assertIn("brave_timeout_seconds", values)
        self.assertEqual(bad_speaker.exception.status_code, 422)
        self.assertEqual(bad_omni_mode.exception.status_code, 422)
        _validate_settings_patch({"tts_provider": "omnivoice", "omnivoice_voice_mode": "auto"})
        _validate_settings_patch({"web_search_providers": "tavily,brave,searxng,duckduckgo"})
        _validate_settings_patch({"web_search_providers": "brava", "brave_timeout_seconds": 1.5})
        with self.assertRaises(HTTPException):
            _validate_settings_patch({"web_search_providers": "unknown"})

    def test_admin_seed_voice_profiles_can_be_saved_tagged_and_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_db = os.environ.get("HERMES_STS_CONFIG_DB")
            os.environ["HERMES_STS_CONFIG_DB"] = str(Path(tmp) / "admin.sqlite3")
            try:
                store = ConfigStore.default()
                voice_id = "seed_test"
                store.upsert_voice(
                    {
                        "id": voice_id,
                        "name": "冷静 A",
                        "provider": "qwen3tts",
                        "mode": "seed",
                        "seed": 12345,
                        "tags": "冷静,清晰",
                        "note": "第三条随机，低频更稳",
                    }
                )
                voice = store.voice_profile(voice_id)
                store.set_settings({"qwentts_cpp_voice_mode": "default", "qwentts_cpp_seed": voice["seed"]})
                settings = store.load_settings()
            finally:
                if old_db is None:
                    os.environ.pop("HERMES_STS_CONFIG_DB", None)
                else:
                    os.environ["HERMES_STS_CONFIG_DB"] = old_db

        self.assertIsNotNone(voice)
        self.assertEqual(voice["tags"], "冷静,清晰")
        self.assertEqual(voice["note"], "第三条随机，低频更稳")
        self.assertEqual(settings.qwentts_cpp_seed, 12345)
        self.assertEqual(settings.qwentts_cpp_voice_mode, "default")

    def test_design_voice_profiles_keep_prompt_note_and_map_to_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "settings.sqlite3")
            store.upsert_voice(
                {
                    "id": "design_test",
                    "name": "冷感播报",
                    "provider": "qwen3tts",
                    "mode": "design",
                    "design_prompt": "female adult, cool clear Mandarin, calm pace, low energy",
                    "tags": "冷感,播报",
                    "note": "适合提醒和短句快答",
                }
            )
            voice = store.voice_profile("design_test")

        self.assertIsNotNone(voice)
        self.assertEqual(voice["note"], "适合提醒和短句快答")
        self.assertEqual(voice["design_prompt"], "female adult, cool clear Mandarin, calm pace, low energy")
        self.assertEqual(
            _settings_for_voice_profile(voice),
            {
                "qwentts_cpp_voice_mode": "design",
                "qwentts_cpp_voice_design": "female adult, cool clear Mandarin, calm pace, low energy",
            },
        )

    def test_preview_voice_preserves_qwen_settings_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = ConfigStore(tmp_path / "config.sqlite3")
            model = tmp_path / "design.gguf"
            codec = tmp_path / "codec.gguf"
            model.touch()
            codec.touch()
            settings = Settings(
                qwentts_cpp_model=str(model),
                qwentts_cpp_codec=str(codec),
                qwentts_cpp_seed=777,
                qwentts_cpp_lang="Chinese",
                qwentts_cpp_format="wav16",
                qwentts_cpp_extra_args="--temp 0.7",
            )

            voice = _preview_voice(
                PreviewRequest(text="你好", voice_mode="design", design_prompt="warm clear voice"),
                settings,
                store,
            )

        self.assertEqual(voice.instruct, "warm clear voice")
        self.assertEqual(voice.seed, 777)
        self.assertEqual(voice.model, str(model))
        self.assertEqual(voice.codec, str(codec))
        self.assertEqual(voice.extra_args, "--temp 0.7")

    def test_config_store_deleted_persona_stays_deleted_after_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "settings.sqlite3")

            self.assertTrue(store.persona_profile("news_anchor"))
            self.assertTrue(store.delete_persona("news_anchor"))
            store.ensure_defaults()

            self.assertIsNone(store.persona_profile("news_anchor"))
            self.assertGreaterEqual(len(store.persona_profiles()), 1)

    def test_config_store_seeds_kokoro_defaults_for_ui_switching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "settings.sqlite3")
            settings = store.load_settings()

        self.assertTrue(settings.sherpa_kokoro_model.endswith("models/kokoro-multi-lang-v1_0/model.onnx"))
        self.assertTrue(settings.sherpa_kokoro_voices.endswith("models/kokoro-multi-lang-v1_0/voices.bin"))
        self.assertIn("lexicon-zh.txt", settings.sherpa_kokoro_lexicon)

    def test_realtime_turn_metrics_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_db = os.environ.get("HERMES_STS_CONFIG_DB")
            os.environ["HERMES_STS_CONFIG_DB"] = str(Path(tmp) / "metrics.sqlite3")
            try:
                session = bare_session()
                session.settings = Settings(latency_logging=True)
                session._log_turn_metrics(
                    TurnMetrics(
                        turn_id="turn_test",
                        started_at=time.perf_counter(),
                        utterance_ms=320,
                        stt_ms=11,
                        llm_ms=22,
                        first_tts_ms=33,
                        first_audio_ms=44,
                        tts_segments=1,
                        audio_chunks=2,
                    ),
                    status="completed",
                )
                metrics = ConfigStore.default().metrics(5)
            finally:
                if old_db is None:
                    os.environ.pop("HERMES_STS_CONFIG_DB", None)
                else:
                    os.environ["HERMES_STS_CONFIG_DB"] = old_db

        self.assertEqual(metrics[0]["kind"], "turn")
        self.assertEqual(metrics[0]["value"]["turn_id"], "turn_test")
        self.assertEqual(metrics[0]["value"]["first_audio_ms"], 44)

    def test_admin_state_summary_payloads_expose_cockpit_shape(self) -> None:
        class FakeWebSearch:
            def description(self) -> str:
                return "chain:tavily,duckduckgo"

            def state(self) -> dict[str, object]:
                return {
                    "provider": "chain:tavily,duckduckgo",
                    "providers": ["tavily", "duckduckgo"],
                    "recent_success": "duckduckgo",
                    "cooldowns": {},
                    "last_error": "",
                }

        with tempfile.TemporaryDirectory() as tmp:
            conv_store = ConversationStore(str(Path(tmp) / "conversations.sqlite3"))
            conv_id = conv_store.create_conversation()
            conv_store.append_message(conv_id, "user", "你好")

            registry = ToolRegistry()
            settings = Settings(web_search_enabled=True, web_search_providers="tavily,duckduckgo")
            conversation = _conversation_payload(conv_store)
            web_search = _web_search_payload(settings, FakeWebSearch())
            diagnostics = _diagnostics_payload(settings, [], tools=registry, web_search=FakeWebSearch())

        self.assertTrue(conversation["enabled"])
        self.assertEqual(conversation["active"]["message_count"], 1)
        self.assertTrue(conversation["reset_available"])
        self.assertEqual(web_search["configured_providers"], ["tavily", "duckduckgo"])
        self.assertEqual(web_search["recent_success"], "duckduckgo")
        self.assertIn("llm", diagnostics)
        self.assertIn("recent", diagnostics)
        self.assertIn("系统", diagnostics["tools"]["message"])


if __name__ == "__main__":
    unittest.main()
