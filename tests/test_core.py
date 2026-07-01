from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import stat
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

from fastapi import HTTPException

from hermes_sts.admin import (
    PreviewRequest,
    _build_voice_design_prompt,
    _conversation_payload,
    _diagnostics_payload,
    _effective_voice_payload,
    _preview_voice,
    _requires_rebuild,
    _same_persona_profile,
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
from hermes_sts.tools import ToolRegistry, ToolSpec, ToolExecution, register_default_local_tools
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

    def test_strip_inline_tool_markup_from_assistant_text(self) -> None:
        text = (
            "好的。\n"
            "<tool_call>play_emotion>\n"
            "<parameter=emotion>\n"
            "attentive\n"
            "</parameter>\n"
            "</function>\n"
            "</tool_call>"
        )

        clean, stripped = BaseOpenAIChatProvider._strip_inline_tool_markup(text)

        self.assertTrue(stripped)
        self.assertEqual(clean, "好的。")

    def test_sanitize_prompt_messages_removes_assistant_tool_tag_history(self) -> None:
        messages = [
            {"role": "user", "content": "<tool_call>这只是用户原文</tool_call>"},
            {
                "role": "assistant",
                "content": "<tool_call>play_emotion>\n<parameter=emotion>\nattentive\n</parameter>\n</function>\n</tool_call>",
            },
        ]

        clean = BaseOpenAIChatProvider._sanitize_prompt_messages(messages)

        self.assertEqual(clean[0]["content"], "<tool_call>这只是用户原文</tool_call>")
        self.assertEqual(clean[1]["content"], "")

    def test_chat_parses_inline_tool_markup_without_saving_text(self) -> None:
        class InlineToolProvider(DummyChatProvider):
            async def _post_chat_completions(self, body):
                return {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    "<tool_call>dance>\n"
                                    "<parameter=move>\nside_to_side_sway\n</parameter>\n"
                                    "<parameter=repeat>\n3\n</parameter>\n"
                                    "</function>\n"
                                    "</tool_call>"
                                )
                            }
                        }
                    ]
                }

        provider = InlineToolProvider(Settings())
        response = asyncio.run(provider.chat("跳舞", tools=[{"type": "function", "function": {"name": "dance"}}]))

        self.assertEqual(response.text, "")
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].name, "dance")
        self.assertEqual(response.tool_calls[0].arguments, '{"move": "side_to_side_sway", "repeat": 3}')
        self.assertEqual(provider.history, [])

    def test_unknown_inline_tool_markup_is_dropped_without_execution(self) -> None:
        class InlineToolProvider(DummyChatProvider):
            async def _post_chat_completions(self, body):
                return {
                    "choices": [
                        {
                            "message": {
                                "content": "<function=unknown_tool>\n<parameter=x>\n1\n</parameter>\n</function>"
                            }
                        }
                    ]
                }

        provider = InlineToolProvider(Settings())
        response = asyncio.run(provider.chat("测试", tools=[{"type": "function", "function": {"name": "dance"}}]))

        self.assertEqual(response.text, "")
        self.assertEqual(response.tool_calls, [])
        self.assertEqual(provider.history, [])

    def test_streaming_inline_tool_markup_falls_back_before_speech(self) -> None:
        class InlineToolStreamingProvider(DummyChatProvider):
            async def _post_chat_completions_stream(self, body):
                yield "<"
                yield "tool_call>play"
                yield "_emotion>"

        async def run() -> list[str]:
            provider = InlineToolStreamingProvider(Settings())
            chunks: list[str] = []
            async for chunk in provider.stream_text(
                "聊天",
                tools=[{"type": "function", "function": {"name": "play_emotion"}}],
            ):
                chunks.append(chunk)
            return chunks

        with self.assertRaises(LLMToolCallDetected):
            asyncio.run(run())

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

    def test_llm_history_prompt_keeps_stable_anchor_when_window_slides(self) -> None:
        settings = Settings(
            hermes_history_max_messages=6,
            hermes_history_anchor_messages=2,
            hermes_history_max_chars=10000,
        )
        provider = DummyChatProvider(settings)
        provider.history = [{"role": "user", "content": f"m{i}"} for i in range(10)]

        first = provider._history_for_prompt()
        provider.history.extend([{"role": "assistant", "content": "m10"}, {"role": "user", "content": "m11"}])
        second = provider._history_for_prompt()

        self.assertEqual([message["content"] for message in first], ["m0", "m1", "m6", "m7", "m8", "m9"])
        self.assertEqual([message["content"] for message in second], ["m0", "m1", "m8", "m9", "m10", "m11"])

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
        self.assertFalse(result.needs_response)
        self.assertEqual(result.category, "motion")

    def test_tool_registry_infers_client_tool_response_policy(self) -> None:
        registry = ToolRegistry()
        registry.set_client_tools(
            [
                {
                    "type": "function",
                    "name": "camera",
                    "description": "Take a picture with the camera and ask a question about it.",
                    "parameters": {"type": "object", "properties": {"question": {"type": "string"}}},
                },
                {
                    "type": "function",
                    "name": "play_emotion",
                    "description": "Play a robot emotion.",
                    "parameters": {"type": "object", "properties": {"emotion": {"type": "string"}}},
                },
            ]
        )

        camera = asyncio.run(registry.execute("camera", '{"question":"what is this?"}'))
        emotion = asyncio.run(registry.execute("play_emotion", '{"emotion":"happy"}'))

        self.assertTrue(camera.needs_response)
        self.assertEqual(camera.category, "vision")
        self.assertFalse(emotion.needs_response)
        self.assertEqual(emotion.category, "emotion")

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

    def test_terminal_tool_executes_allowlisted_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = ToolRegistry()
            settings = Settings(
                llm_provider="openai_compatible",
                terminal_tool_enabled=True,
                terminal_tool_allowed_commands=Path(sys.executable).name,
                terminal_tool_cwd=tmp,
                terminal_tool_timeout_seconds=2.0,
                terminal_tool_max_output_chars=1000,
            )
            register_default_local_tools(registry, settings)

            result = asyncio.run(
                registry.execute(
                    "terminal_exec",
                    {"command": f"{sys.executable} -c 'print(\"terminal-ok\")'"},
                )
            )

        self.assertEqual(result.mode, "local")
        self.assertIn("exit_code: 0", result.result)
        self.assertIn("terminal-ok", result.result)

    def test_terminal_tool_rejects_unallowlisted_command(self) -> None:
        registry = ToolRegistry()
        settings = Settings(
            llm_provider="openai_compatible",
            terminal_tool_enabled=True,
            terminal_tool_allowed_commands="curl",
        )
        register_default_local_tools(registry, settings)

        result = asyncio.run(registry.execute("terminal_exec", {"command": "rm -rf /tmp/nope"}))

        self.assertIn("not allowlisted", result.result)

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

        self.assertIsNone(answer)
        self.assertIsNone(session.pending_tool_context)
        event_types = [event["type"] for event in events]
        self.assertEqual(event_types, ["response.created", "response.function_call_arguments.done", "response.output_audio.done", "response.output_audio_transcript.done", "response.done"])
        self.assertEqual(events[1]["name"], "dance")
        self.assertEqual(events[1]["call_id"], "call_1")
        self.assertEqual(events[1]["arguments"], '{"dance": "happy"}')

    def test_session_keeps_followup_for_client_tool_that_needs_response(self) -> None:
        class FakeLlm:
            async def chat(self, *args, **kwargs):
                return LLMResponse(
                    tool_calls=[ToolCall(id="call_camera", name="camera", arguments='{"question":"what is this?"}')]
                )

            async def ensure_active_conversation(self) -> str:
                return ""

        session = bare_session()
        session.llm = FakeLlm()
        session.tools.set_client_tools(
            [
                {
                    "type": "function",
                    "name": "camera",
                    "description": "Take a picture with the camera and ask a question about it.",
                    "parameters": {"type": "object", "properties": {"question": {"type": "string"}}},
                }
            ]
        )
        events = []

        async def fake_send(self, event):
            events.append(event)

        session._send = types.MethodType(fake_send, session)
        answer = asyncio.run(session._ask_llm_with_tools("看看这是什么", response_id="resp_1", item_id="item_1"))

        self.assertIsNone(answer)
        self.assertIsNotNone(session.pending_tool_context)
        self.assertEqual(events[0]["type"], "response.created")
        self.assertEqual(events[1]["type"], "response.function_call_arguments.done")
        self.assertEqual(events[1]["name"], "camera")

    def test_direct_camera_command_routes_to_client_tool(self) -> None:
        session = bare_session()
        session.tools.set_client_tools(
            [
                {
                    "type": "function",
                    "name": "camera",
                    "description": "Take a picture with the camera and ask a question about it.",
                    "parameters": {"type": "object", "properties": {"question": {"type": "string"}}},
                }
            ]
        )

        tool_call = session._direct_client_action_tool_call("帮我看前面是什么")

        self.assertIsNotNone(tool_call)
        self.assertEqual(tool_call.name, "camera")
        self.assertEqual(json.loads(tool_call.arguments)["question"], "帮我看前面是什么")

    def test_direct_camera_command_handles_common_deictic_phrases(self) -> None:
        session = bare_session()
        session.tools.set_client_tools(
            [
                {
                    "type": "function",
                    "name": "camera",
                    "description": "Take a picture with the camera and ask a question about it.",
                    "parameters": {"type": "object", "properties": {"question": {"type": "string"}}},
                }
            ]
        )
        session.llm = types.SimpleNamespace(history=[])

        self.assertEqual(session._direct_client_action_tool_call("你看一下这是什么").name, "camera")
        self.assertEqual(session._direct_client_action_tool_call("看这里").name, "camera")

    def test_direct_camera_followup_uses_recent_visual_context(self) -> None:
        session = bare_session()
        session.tools.set_client_tools(
            [
                {
                    "type": "function",
                    "name": "camera",
                    "description": "Take a picture with the camera and ask a question about it.",
                    "parameters": {"type": "object", "properties": {"question": {"type": "string"}}},
                }
            ]
        )
        session.llm = types.SimpleNamespace(history=[])
        session.llm.history = [
            {"role": "user", "content": "你看一下这是什么"},
            {"role": "assistant", "content": "我马上帮你拍照看看。"},
        ]

        tool_call = session._direct_client_action_tool_call("这是什么？")

        self.assertIsNotNone(tool_call)
        self.assertEqual(tool_call.name, "camera")

    def test_empty_camera_result_does_not_claim_completed(self) -> None:
        class FakeLlm:
            async def chat(self, *args, **kwargs):
                raise AssertionError("LLM should not be called when camera returns no visual content")

            async def ensure_active_conversation(self) -> str:
                return ""

        session = bare_session()
        session.llm = FakeLlm()
        session.tools.set_client_tools(
            [
                {
                    "type": "function",
                    "name": "camera",
                    "description": "Take a picture with the camera and ask a question about it.",
                    "parameters": {"type": "object", "properties": {"question": {"type": "string"}}},
                }
            ]
        )
        session.pending_tool_context = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "看前面是什么"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_camera",
                        "type": "function",
                        "function": {"name": "camera", "arguments": '{"question":"看前面是什么"}'},
                    }
                ],
            },
        ]
        session.pending_tool_results = [
            {"role": "tool", "tool_call_id": "call_camera", "content": '{"ok": true}'}
        ]
        sent = []

        async def fake_send_response(self, text, *, transcript, metrics=None, voice=None):
            sent.append(text)

        session._send_response = types.MethodType(fake_send_response, session)
        asyncio.run(session._process_tool_result_turn())

        self.assertEqual(len(sent), 1)
        self.assertIn("没有拿到可描述的画面内容", sent[0])
        self.assertNotIn("已完成", sent[0])

    def test_camera_result_falls_back_to_visual_summary_when_llm_is_empty(self) -> None:
        class FakeLlm:
            async def chat(self, *args, **kwargs):
                return LLMResponse(text="")

            async def ensure_active_conversation(self) -> str:
                return ""

        session = bare_session()
        session.llm = FakeLlm()
        session.tools.set_client_tools(
            [
                {
                    "type": "function",
                    "name": "camera",
                    "description": "Take a picture with the camera and ask a question about it.",
                    "parameters": {"type": "object", "properties": {"question": {"type": "string"}}},
                }
            ]
        )
        session.pending_tool_context = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "你看到了什么"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_camera",
                        "type": "function",
                        "function": {"name": "camera", "arguments": '{"question":"你看到了什么"}'},
                    }
                ],
            },
        ]
        session.pending_tool_results = [
            {"role": "tool", "tool_call_id": "call_camera", "content": '{"ok": true, "description": "前方有一张桌子和一台显示器。"}'}
        ]
        sent = []

        async def fake_send_response(self, text, *, transcript, metrics=None, voice=None):
            sent.append(text)

        session._send_response = types.MethodType(fake_send_response, session)
        asyncio.run(session._process_tool_result_turn())

        self.assertEqual(sent, ["我看到：前方有一张桌子和一台显示器。"])

    def test_direct_dance_command_routes_to_client_tool_without_llm(self) -> None:
        class FakeLlm:
            history: list[dict[str, str]] = []

            async def chat(self, *args, **kwargs):
                raise AssertionError("direct action should not call LLM")

        session = bare_session()
        session.llm = FakeLlm()
        session.tools.set_client_tools(
            [
                {
                    "type": "function",
                    "name": "dance",
                    "description": "Play a named or random dance move once (or repeat).",
                    "parameters": {
                        "type": "object",
                        "properties": {"move": {"type": "string"}, "repeat": {"type": "integer"}},
                    },
                }
            ]
        )
        events = []

        async def fake_send(self, event):
            events.append(event)

        session._send = types.MethodType(fake_send, session)
        routed = asyncio.run(session._route_direct_client_action("那你给我跳个舞", instructions=""))

        self.assertTrue(routed)
        tool_event = next(event for event in events if event["type"] == "response.function_call_arguments.done")
        self.assertEqual(tool_event["name"], "dance")
        self.assertEqual(json.loads(tool_event["arguments"]), {"move": "random", "repeat": 1})
        self.assertIsNone(session.pending_tool_context)

    def test_direct_contextual_start_routes_to_dance_only_after_dance_context(self) -> None:
        class FakeLlm:
            history = [{"role": "user", "content": "三个舞。"}]

        session = bare_session()
        session.llm = FakeLlm()
        session.tools.set_client_tools(
            [
                {
                    "type": "function",
                    "name": "dance",
                    "description": "Play a dance.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]
        )
        events = []

        async def fake_send(self, event):
            events.append(event)

        session._send = types.MethodType(fake_send, session)
        self.assertTrue(asyncio.run(session._route_direct_client_action("开始", instructions="")))

        session.llm.history = []
        self.assertIsNone(session._direct_client_action_tool_call("开始"))

    def test_direct_head_and_emotion_commands_route_to_client_tools(self) -> None:
        session = bare_session()
        session.tools.set_client_tools(
            [
                {
                    "type": "function",
                    "name": "move_head",
                    "description": "Move head.",
                    "parameters": {"type": "object", "properties": {"direction": {"type": "string"}}},
                },
                {
                    "type": "function",
                    "name": "play_emotion",
                    "description": "Play emotion.",
                    "parameters": {"type": "object", "properties": {"emotion": {"type": "string"}}},
                },
            ]
        )

        head = session._direct_client_action_tool_call("摇头")
        emotion = session._direct_client_action_tool_call("做一个开心的动作")

        self.assertEqual(head.name if head else None, "move_head")
        self.assertEqual(json.loads(head.arguments)["direction"], "front")
        self.assertEqual(emotion.name if emotion else None, "play_emotion")
        self.assertEqual(json.loads(emotion.arguments)["emotion"], "happy")

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
                        ToolCall(id="call_client", name="camera", arguments='{"question":"what is this?"}'),
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
                    "name": "camera",
                    "description": "Take a picture with the camera and ask a question about it.",
                    "parameters": {"type": "object", "properties": {"question": {"type": "string"}}},
                }
            ]
        )
        events = []

        async def fake_send(self, event):
            events.append(event)

        session._send = types.MethodType(fake_send, session)
        answer = asyncio.run(session._ask_llm_with_tools("报时并跳舞", response_id="resp_1", item_id="item_1"))

        self.assertIsNone(answer)
        self.assertEqual(events[1]["name"], "camera")
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

    def test_explicit_weather_request_uses_web_search_before_llm(self) -> None:
        class FakeLlm:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self.last_messages: list[dict[str, object]] = []

            async def ensure_active_conversation(self) -> str:
                return "conv"

            async def chat(self, *args, **kwargs):
                self.calls.append({"args": args, "kwargs": kwargs})
                messages = kwargs["messages"]
                self.last_messages = messages
                return LLMResponse(text="杭州今天多云，出门带伞更稳妥。")

        session = bare_session()
        llm = FakeLlm()
        session.llm = llm
        session.tools.register_local(
            ToolSpec(
                name="web_search",
                description="Search the web.",
                kind="slow",
                mode="local",
                parameters={"type": "object", "properties": {"query": {"type": "string"}}},
                handler=lambda args: f"杭州天气搜索结果：{args['query']}",
            )
        )

        answer = asyncio.run(session._ask_llm_with_tools("查一下杭州的天气", response_id="resp_1", item_id="item_1"))

        self.assertEqual(answer, "杭州今天多云，出门带伞更稳妥。")
        self.assertEqual(len(llm.calls), 1)
        self.assertIsNone(llm.calls[0]["kwargs"].get("tools"))
        self.assertEqual(llm.last_messages[-1]["role"], "tool")
        self.assertIn("杭州天气", llm.last_messages[-1]["content"])

    def test_explicit_terminal_request_uses_terminal_tool_before_llm(self) -> None:
        class FakeLlm:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self.last_messages: list[dict[str, object]] = []

            async def ensure_active_conversation(self) -> str:
                return "conv"

            async def chat(self, *args, **kwargs):
                self.calls.append({"args": args, "kwargs": kwargs})
                messages = kwargs["messages"]
                self.last_messages = messages
                return LLMResponse(text="命令输出是 terminal-ok。")

        session = bare_session()
        llm = FakeLlm()
        session.llm = llm
        seen_args: list[dict[str, object]] = []
        session.tools.register_local(
            ToolSpec(
                name="terminal_exec",
                description="Run a terminal command.",
                kind="slow",
                mode="local",
                parameters={"type": "object", "properties": {"command": {"type": "string"}}},
                handler=lambda args: seen_args.append(args) or "stdout:\nterminal-ok",
            )
        )

        answer = asyncio.run(
            session._ask_llm_with_tools("请调用 terminal_exec 执行命令 date，然后简短告诉我输出。", response_id="resp_1", item_id="item_1")
        )

        self.assertEqual(answer, "命令输出是 terminal-ok。")
        self.assertEqual(seen_args, [{"command": "date"}])
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(llm.last_messages[-1]["name"], "terminal_exec")

    def test_plain_today_phrase_does_not_force_web_search(self) -> None:
        class FakeLlm:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def ensure_active_conversation(self) -> str:
                return "conv"

            async def chat(self, *args, **kwargs):
                self.calls.append({"args": args, "kwargs": kwargs})
                return LLMResponse(text="听起来今天有点累，我在。")

        session = bare_session()
        llm = FakeLlm()
        session.llm = llm
        session.tools.register_local(
            ToolSpec(
                name="web_search",
                description="Search the web.",
                kind="slow",
                mode="local",
                parameters={"type": "object", "properties": {"query": {"type": "string"}}},
                handler=lambda _args: "should not run",
            )
        )

        answer = asyncio.run(session._ask_llm_with_tools("今天心情不太好", response_id="resp_1", item_id="item_1"))

        self.assertEqual(answer, "听起来今天有点累，我在。")
        self.assertEqual(llm.calls[0]["args"], ("今天心情不太好",))

    def test_direct_search_query_is_normalized_for_common_realtime_queries(self) -> None:
        self.assertEqual(
            RealtimeSession._search_query_for_transcript("查一下杭州的天气，简单告诉我现在适不适合出门"),
            "杭州天气",
        )
        self.assertEqual(
            RealtimeSession._search_query_for_transcript("查一下人民币美元汇率，简单告诉我大概是多少"),
            "美元兑人民币 实时汇率 USD CNY",
        )

    def test_direct_terminal_command_extracts_only_explicit_commands(self) -> None:
        self.assertEqual(
            RealtimeSession._terminal_command_for_transcript("请通过终端运行 curl -s https://wttr.in/Hangzhou?format=j1，然后告诉我结果。"),
            "curl -s https://wttr.in/Hangzhou?format=j1",
        )
        self.assertEqual(
            RealtimeSession._terminal_command_for_transcript("请执行命令 date，然后简短告诉我输出。"),
            "date",
        )
        self.assertEqual(RealtimeSession._terminal_command_for_transcript("现在几点了"), "")

    def test_local_tool_loop_can_run_multiple_steps(self) -> None:
        class FakeLlm:
            def __init__(self) -> None:
                self.calls = 0
                self.messages_by_call: list[list[dict[str, object]]] = []

            async def ensure_active_conversation(self) -> str:
                return "conv"

            async def chat(self, *args, **kwargs):
                self.calls += 1
                if "messages" in kwargs:
                    self.messages_by_call.append(list(kwargs["messages"]))
                if self.calls == 1:
                    return LLMResponse(tool_calls=[ToolCall(id="call_1", name="noop", arguments="{}")])
                if self.calls == 2:
                    return LLMResponse(tool_calls=[ToolCall(id="call_2", name="current_time", arguments="{}")])
                return LLMResponse(text="已经查完。")

        session = bare_session()
        llm = FakeLlm()
        session.llm = llm

        answer = asyncio.run(session._ask_llm_with_tools("先测试再报时", response_id="resp_1", item_id="item_1"))

        self.assertEqual(answer, "已经查完。")
        self.assertEqual(llm.calls, 3)
        self.assertEqual(llm.messages_by_call[0][-1]["content"], "noop completed")
        self.assertEqual(llm.messages_by_call[1][-1]["name"], "current_time")

    def test_tool_system_prompt_keeps_voice_description_out_of_spoken_text(self) -> None:
        session = object.__new__(RealtimeSession)
        session.settings = Settings()
        session.instructions = ""
        session.next_response_instructions = ""

        prompt = session._tool_system_prompt(instructions="保持温柔。")

        self.assertIn("不要输出 emoji", prompt)
        self.assertIn("不要写音色", prompt)
        self.assertIn("音色描述由 TTS 声音配置单独控制", prompt)
        self.assertIn("必须基于这些结果直接回答用户", prompt)

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
        settings_effective = session._effective_instructions("temporary")
        self.assertIn("夜航副驾", settings_effective)
        self.assertIn("WS profile", settings_effective)
        self.assertIn("Response-specific instructions", settings_effective)

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

        self.assertEqual(migrated.web_search_providers, "brave,tavily,searxng,duckduckgo")
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
            store = ConfigStore(Path(tmp) / "admin.sqlite3")
            payload = _settings_payload(store.load_settings(), store)
            with self.assertRaises(HTTPException) as bad_speaker:
                _validate_settings_patch({"qwentts_cpp_voice_preset": "not_a_real_speaker"})
            with self.assertRaises(HTTPException) as bad_omni_mode:
                _validate_settings_patch({"omnivoice_voice_mode": "preset"})

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
        self.assertIn("hermes_history_anchor_messages", values)
        self.assertIn("hermes_history_idle_reset_seconds", values)
        self.assertIn("hermes_agent_max_wait_seconds", values)
        self.assertIn("hermes_filler_interval_seconds", values)
        self.assertIn("hermes_max_fillers", values)
        self.assertIn("brave_api_key", values)
        self.assertIn("brave_base_url", values)
        self.assertIn("brave_timeout_seconds", values)
        self.assertIn("terminal_tool_enabled", values)
        self.assertIn("terminal_tool_allowed_commands", values)
        self.assertEqual(bad_speaker.exception.status_code, 422)
        self.assertEqual(bad_omni_mode.exception.status_code, 422)
        _validate_settings_patch({"tts_provider": "omnivoice", "omnivoice_voice_mode": "auto"})
        _validate_settings_patch({"web_search_providers": "tavily,brave,searxng,duckduckgo"})
        _validate_settings_patch({"tavily_search_depth": "advanced", "tavily_timeout_seconds": 5.0})
        _validate_settings_patch({"web_search_providers": "brava", "brave_timeout_seconds": 1.5})
        _validate_settings_patch(
            {
                "terminal_tool_allowed_commands": "curl,python3",
                "terminal_tool_timeout_seconds": 5,
                "terminal_tool_max_output_chars": 1000,
            }
        )
        with self.assertRaises(HTTPException):
            _validate_settings_patch({"web_search_providers": "unknown"})
        with self.assertRaises(HTTPException):
            _validate_settings_patch({"tavily_search_depth": "fast"})
        with self.assertRaises(HTTPException):
            _validate_settings_patch({"terminal_tool_allowed_commands": "/bin/curl"})

    def test_admin_seed_voice_profiles_can_be_saved_tagged_and_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "admin.sqlite3")
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
                "tts_provider": "qwen3tts",
                "qwentts_cpp_voice_mode": "design",
                "qwentts_cpp_voice_design": "female adult, cool clear Mandarin, calm pace, low energy",
            },
        )

    def test_effective_voice_reports_qwen_design_fallback_when_model_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            base = tmp_path / "base.gguf"
            codec = tmp_path / "codec.gguf"
            base.touch()
            codec.touch()
            store = ConfigStore(tmp_path / "settings.sqlite3")
            store.set_settings(
                {
                    "tts_provider": "qwen3tts",
                    "qwentts_cpp_voice_mode": "design",
                    "qwentts_cpp_voice_design": "cool clear Mandarin",
                    "qwentts_cpp_base_model": str(base),
                    "qwentts_cpp_voicedesign_model": str(tmp_path / "missing-design.gguf"),
                    "qwentts_cpp_codec": str(codec),
                }
            )
            payload = _effective_voice_payload(store.load_settings(), store)

        self.assertEqual(payload["engine"], "qwen3tts")
        self.assertEqual(payload["mode"], "design")
        self.assertFalse(payload["ready"])
        self.assertIn("VoiceDesign", payload["fallback_reason"])

    def test_omnivoice_voice_profile_maps_provider_without_touching_qwen_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "settings.sqlite3")
            store.upsert_voice(
                {
                    "id": "omni_design",
                    "name": "Omni 冷静",
                    "provider": "omnivoice",
                    "mode": "design",
                    "design_prompt": "female, young adult, chinese accent, moderate pitch",
                }
            )
            voice = store.voice_profile("omni_design")
            assert voice is not None
            store.set_settings({"qwentts_cpp_voice_mode": "preset", "qwentts_cpp_voice_preset": "vivian"})
            store.set_settings(_settings_for_voice_profile(voice))
            settings = store.load_settings()

        self.assertEqual(settings.tts_provider, "omnivoice")
        self.assertEqual(settings.omnivoice_voice_mode, "design")
        self.assertEqual(settings.omnivoice_voice_design, "female, young adult, chinese accent, moderate pitch")
        self.assertEqual(settings.qwentts_cpp_voice_mode, "preset")

    def test_saved_preset_and_omnivoice_seed_profiles_map_to_one_click_settings(self) -> None:
        qwen_preset = {
            "id": "preset_vivian",
            "name": "常用 Vivian",
            "provider": "qwen3tts",
            "mode": "preset",
            "speaker": "vivian",
            "note": "日常快答更清晰",
        }
        omni_seed = {
            "id": "omni_seed",
            "name": "Omni 稳定 seed",
            "provider": "omnivoice",
            "mode": "seed",
            "seed": 99,
            "note": "播报感更稳",
        }
        kokoro_voice = {
            "id": "kokoro_1",
            "name": "Kokoro 备用",
            "provider": "sherpa_kokoro",
            "mode": "voice",
            "speaker": "3",
            "note": "低延迟备用",
        }

        self.assertEqual(
            _settings_for_voice_profile(qwen_preset),
            {
                "tts_provider": "qwen3tts",
                "qwentts_cpp_voice_mode": "preset",
                "qwentts_cpp_voice_preset": "vivian",
            },
        )
        self.assertEqual(
            _settings_for_voice_profile(omni_seed),
            {
                "tts_provider": "omnivoice",
                "omnivoice_voice_mode": "auto",
                "omnivoice_seed": 99,
            },
        )
        self.assertEqual(
            _settings_for_voice_profile(kokoro_voice),
            {
                "tts_provider": "sherpa_kokoro",
                "sherpa_kokoro_voice": 3,
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

    def test_config_store_seeds_expressive_persona_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "settings.sqlite3")

            personas = store.persona_profiles()
            cat = store.persona_profile("cat_companion")
            taiwan = store.persona_profile("taiwan_sweetheart")

        identity_words = ("female", "male", "adult", "elderly", "child", "young")
        for persona in personas:
            if persona["voice_mode"] == "design":
                self.assertIn(persona["voice_identity"], {"young_female", "adult_female", "young_male", "adult_male", "child", "elder"})
                self.assertTrue(
                    any(word in persona["voice_ref"].lower() for word in identity_words),
                    persona["voice_ref"],
                )
        self.assertIsNotNone(cat)
        self.assertEqual(cat["name"], "猫系小甜心")
        self.assertEqual(cat["voice_mode"], "design")
        self.assertIn("喵", cat["prompt"])
        self.assertIsNotNone(taiwan)
        self.assertEqual(taiwan["name"], "台湾甜妹")
        self.assertEqual(taiwan["voice_mode"], "design")
        self.assertIn("Taiwanese Mandarin", taiwan["voice_ref"])

    def test_same_persona_profile_ignores_storage_metadata(self) -> None:
        stored = {
            "id": "field_operator",
            "name": "快反执行",
            "prompt": "短句执行",
            "voice_engine": "qwen3tts",
            "voice_mode": "design",
            "voice_identity": "young_female",
            "voice_ref": "confident voice",
            "voice_seed": 42,
            "updated_at": 100.0,
        }
        incoming = dict(stored)
        incoming.pop("updated_at")

        self.assertTrue(_same_persona_profile(stored, incoming))
        self.assertFalse(_same_persona_profile(stored, {**incoming, "voice_ref": "other"}))
        self.assertFalse(_same_persona_profile(stored, {**incoming, "voice_identity": "adult_male"}))

    def test_voice_design_prompt_uses_structured_identity(self) -> None:
        prompt = _build_voice_design_prompt("warm bright Mandarin voice", "young_male")

        self.assertIn("warm bright Mandarin voice", prompt)
        self.assertIn("young adult male Mandarin speaker identity", prompt)
        self.assertEqual(_build_voice_design_prompt(prompt, "young_male"), prompt)
        self.assertIn(
            "young adult female Mandarin speaker identity",
            _build_voice_design_prompt("warm bright Mandarin voice", "unsupported"),
        )

    def test_config_store_seeds_kokoro_defaults_for_ui_switching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "settings.sqlite3")
            settings = store.load_settings()

        self.assertTrue(settings.sherpa_kokoro_model.endswith("models/kokoro-multi-lang-v1_0/model.onnx"))
        self.assertTrue(settings.sherpa_kokoro_voices.endswith("models/kokoro-multi-lang-v1_0/voices.bin"))
        self.assertIn("lexicon-zh.txt", settings.sherpa_kokoro_lexicon)

    def test_realtime_turn_metrics_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "metrics.sqlite3")
            original_default = ConfigStore.default
            ConfigStore.default = classmethod(lambda cls: store)  # type: ignore[method-assign]
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
                metrics = store.metrics(5)
            finally:
                ConfigStore.default = original_default  # type: ignore[method-assign]

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

    def test_streaming_tool_call_after_partial_speech_executes_tool(self) -> None:
        class PartialThenToolLlm:
            def __init__(self) -> None:
                self.chat_calls = 0

            async def ensure_active_conversation(self) -> str:
                return "conv"

            async def stream_text(self, *args, **kwargs):
                yield "让我帮你查"
                raise LLMToolCallDetected("tool")

            async def chat(self, *args, **kwargs):
                self.chat_calls += 1
                if self.chat_calls == 1:
                    return LLMResponse(tool_calls=[ToolCall(id="call_1", name="noop", arguments="{}")])
                return LLMResponse(text="查到了")

        async def run() -> tuple[bool, int, list[str], list[str], bool]:
            session = bare_session()
            session.settings = Settings(llm_streaming_enabled=True)
            session.llm = PartialThenToolLlm()
            response_created_count = 0
            done_transcripts: list[str] = []
            sent_texts: list[str] = []
            execute_called = [False]

            async def fake_send_tts_segment(self, text, *, response_id, item_id, metrics=None, voice=None):
                pass

            async def fake_send_response_created(self, response_id, item_id, metrics=None):
                nonlocal response_created_count
                response_created_count += 1

            async def fake_send_response_done(self, *, response_id, item_id, transcript):
                done_transcripts.append(transcript)

            async def fake_send_text_segments(self, text, *, response_id, item_id, metrics=None, voice=None):
                sent_texts.append(text)

            async def fake_execute(name, arguments):
                execute_called[0] = True
                return ToolExecution(name=name, arguments={}, result="noop completed", mode="local", forwarded=False)

            session._send_tts_segment = types.MethodType(fake_send_tts_segment, session)
            session._send_response_created = types.MethodType(fake_send_response_created, session)
            session._send_response_done = types.MethodType(fake_send_response_done, session)
            session._send_text_segments = types.MethodType(fake_send_text_segments, session)
            session.tools.execute = fake_execute

            result = await session._respond_with_llm_stream("查新闻", voice=TtsVoice(speaker="ui_voice"))
            return result, response_created_count, done_transcripts, sent_texts, execute_called[0]

        result, response_created_count, done_transcripts, sent_texts, execute_called = asyncio.run(run())

        self.assertTrue(result)
        self.assertEqual(response_created_count, 2)
        self.assertEqual(len(done_transcripts), 2)
        self.assertEqual(sent_texts, ["查到了"])
        self.assertTrue(execute_called)

    def test_streaming_tool_call_after_partial_speech_no_tool_calls_sends_followup_answer(self) -> None:
        class PartialThenToolNoCallsLlm:
            def __init__(self) -> None:
                self.chat_calls = 0

            async def ensure_active_conversation(self) -> str:
                return "conv"

            async def stream_text(self, *args, **kwargs):
                yield "让我帮你查"
                raise LLMToolCallDetected("tool")

            async def chat(self, *args, **kwargs):
                self.chat_calls += 1
                if self.chat_calls == 1:
                    return LLMResponse(tool_calls=[ToolCall(id="call_1", name="noop", arguments="{}")])
                return LLMResponse(text="好的", tool_calls=None)

        async def run() -> tuple[bool, int, list[str], list[str], bool]:
            session = bare_session()
            session.settings = Settings(llm_streaming_enabled=True)
            session.llm = PartialThenToolNoCallsLlm()
            response_created_count = 0
            done_transcripts: list[str] = []
            sent_texts: list[str] = []
            execute_called = [False]

            async def fake_send_tts_segment(self, text, *, response_id, item_id, metrics=None, voice=None):
                pass

            async def fake_send_response_created(self, response_id, item_id, metrics=None):
                nonlocal response_created_count
                response_created_count += 1

            async def fake_send_response_done(self, *, response_id, item_id, transcript):
                done_transcripts.append(transcript)

            async def fake_send_text_segments(self, text, *, response_id, item_id, metrics=None, voice=None):
                sent_texts.append(text)

            async def fake_execute(name, arguments):
                execute_called[0] = True
                return ToolExecution(name=name, arguments={}, result="noop completed", mode="local", forwarded=False)

            session._send_tts_segment = types.MethodType(fake_send_tts_segment, session)
            session._send_response_created = types.MethodType(fake_send_response_created, session)
            session._send_response_done = types.MethodType(fake_send_response_done, session)
            session._send_text_segments = types.MethodType(fake_send_text_segments, session)
            session.tools.execute = fake_execute

            result = await session._respond_with_llm_stream("查新闻", voice=TtsVoice(speaker="ui_voice"))
            return result, response_created_count, done_transcripts, sent_texts, execute_called[0]

        result, response_created_count, done_transcripts, sent_texts, execute_called = asyncio.run(run())

        self.assertTrue(result)
        self.assertEqual(response_created_count, 2)
        self.assertEqual(len(done_transcripts), 2)
        self.assertEqual(sent_texts, ["好的"])
        self.assertTrue(execute_called)

    def test_client_tool_followup_timeout_clears_pending_state(self) -> None:
        async def run() -> tuple[object, int]:
            session = bare_session()
            session.settings = Settings(client_tool_followup_timeout_seconds=0.01)
            session.pending_tool_context = [{"role": "user", "content": "摇头"}]
            session.pending_tool_results = [{"role": "tool", "content": "done"}]
            session.tool_followup_timer = None
            await asyncio.sleep(0.05)
            return session.pending_tool_context, len(session.pending_tool_results)

        pending_context, pending_results_len = asyncio.run(run())

        self.assertIsNone(pending_context)
        self.assertEqual(pending_results_len, 0)

    def test_client_tool_followup_timeout_cancelled_by_response(self) -> None:
        session = bare_session()
        session.settings = Settings(client_tool_followup_timeout_seconds=10.0)
        session.pending_tool_context = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "摇头"},
        ]
        session.pending_tool_results = []
        session.tool_followup_timer = None

        session._handle_conversation_item(
            {"type": "function_call_output", "call_id": "call_1", "output": "done"}
        )

        self.assertEqual(len(session.pending_tool_results), 1)
        self.assertIsNotNone(session.tool_followup_timer)

    def test_client_tool_followup_timeout_cancelled_on_disconnect(self) -> None:
        async def run() -> bool:
            session = bare_session()
            session.pending_tool_context = [{"role": "user", "content": "摇头"}]
            session.pending_tool_results = [{"role": "tool", "content": "done"}]

            async def dummy():
                await asyncio.sleep(100)

            timer_task = asyncio.create_task(dummy())
            session.tool_followup_timer = timer_task

            await session._cancel_processing(send_done=False)

            timer_after = session.tool_followup_timer
            cancelled = timer_after is None or timer_after.cancelled() or timer_after.done()

            if timer_after and not timer_after.done():
                timer_after.cancel()
                try:
                    await timer_after
                except asyncio.CancelledError:
                    pass

            return cancelled

        cancelled = asyncio.run(run())
        self.assertTrue(cancelled)


if __name__ == "__main__":
    unittest.main()
