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

from hermes_sts.admin import _settings_for_voice_profile, _settings_payload, _validate_settings_patch
from hermes_sts.config import Settings
from hermes_sts.config_store import ConfigStore
from hermes_sts.llm import BaseOpenAIChatProvider, HermesAgentProvider, LLMResponse, ToolCall
from hermes_sts.realtime import RealtimeSession, TurnMetrics
from hermes_sts.tts import QwenTtsCpp, TtsVoice, build_tts
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

    def test_llm_system_prompt_uses_persona_label(self) -> None:
        prompt = BaseOpenAIChatProvider._system_prompt("你是端庄新闻播报员。")
        self.assertIn("当前人格和表达风格", prompt)
        self.assertIn("端庄新闻播报员", prompt)

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
            )
            pcm16 = QwenTtsCpp(settings)._synthesize_sync("你好")

            self.assertEqual(marker_path.read_text(encoding="utf-8"), "Vulkan0|你好")
            self.assertGreater(len(pcm16), 0)
            self.assertEqual(len(pcm16) % 2, 0)
            self.assertLess(len(pcm16), 2400 * 2)

    def test_effective_tts_voice_respects_source_and_override(self) -> None:
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
        self.assertEqual(session._effective_tts_voice().speaker, "ws_voice")
        self.assertEqual(
            session._effective_tts_voice(TtsVoice(speaker="response_voice")).speaker,
            "response_voice",
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

    def test_synthesize_tts_falls_back_to_settings_voice_when_ws_voice_fails(self) -> None:
        class FakeTts:
            def __init__(self) -> None:
                self.voices = []

            async def synthesize(self, text, *, voice=None):
                self.voices.append(voice.speaker)
                if voice.speaker == "bad_ws":
                    raise RuntimeError("unknown speaker")
                return b"\x00\x00"

        session = bare_session()
        session.settings = Settings(
            tts_voice_source="ws",
            qwentts_cpp_speaker="ui_voice",
        )
        session.tts = FakeTts()

        pcm16 = asyncio.run(session._synthesize_tts("你好", voice=TtsVoice(speaker="bad_ws")))
        self.assertEqual(pcm16, b"\x00\x00")
        self.assertEqual(session.tts.voices, ["bad_ws", "ui_voice"])

    def test_realtime_turn_voice_is_reused_for_waiting_and_answer_audio(self) -> None:
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

        self.assertEqual(session.tts.voices, ["response_voice", "response_voice"])

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

        self.assertIn("--speaker", speaker_cmd)
        self.assertIn("vivian", speaker_cmd)
        self.assertIn("--seed", speaker_cmd)
        self.assertIn("42", speaker_cmd)
        self.assertNotIn("--speaker", clone_cmd)
        self.assertIn("--instruct", clone_cmd)
        self.assertIn("warm tone", clone_cmd)
        self.assertIn("--ref-wav", clone_cmd)
        self.assertIn("/tmp/ref.wav", clone_cmd)
        self.assertIn("--ref-text", clone_cmd)
        self.assertIn("/tmp/ref.txt", clone_cmd)
        self.assertIn("--ref-spk", clone_cmd)
        self.assertIn("/tmp/ref.spk", clone_cmd)
        self.assertIn("--ref-rvq", clone_cmd)
        self.assertIn("/tmp/ref.rvq", clone_cmd)

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
        self.assertEqual(raw["qwentts_cpp_model"], str(custom_model))
        self.assertEqual(raw["qwentts_cpp_speaker"], "vivian")
        self.assertEqual(raw["qwentts_cpp_instruct"], "")

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

    def test_admin_state_exposes_ui_required_values_and_validates_qwen_speaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_db = os.environ.get("HERMES_STS_CONFIG_DB")
            os.environ["HERMES_STS_CONFIG_DB"] = str(Path(tmp) / "admin.sqlite3")
            try:
                store = ConfigStore.default()
                payload = _settings_payload(store.load_settings(), store)
                with self.assertRaises(HTTPException) as bad_speaker:
                    _validate_settings_patch({"qwentts_cpp_voice_preset": "not_a_real_speaker"})
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
        self.assertIn("hermes_history_max_messages", values)
        self.assertIn("hermes_history_max_chars", values)
        self.assertIn("hermes_history_idle_reset_seconds", values)
        self.assertIn("hermes_agent_max_wait_seconds", values)
        self.assertIn("hermes_filler_interval_seconds", values)
        self.assertIn("hermes_max_fillers", values)
        self.assertEqual(bad_speaker.exception.status_code, 422)

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


if __name__ == "__main__":
    unittest.main()
