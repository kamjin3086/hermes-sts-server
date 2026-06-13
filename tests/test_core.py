from __future__ import annotations

import asyncio
import math
import unittest

from hermes_sts.config import Settings
from hermes_sts.llm import BaseOpenAIChatProvider
from hermes_sts.realtime import RealtimeSession
from hermes_sts.tools import ToolRegistry
from hermes_sts.vad import EnergyVad, build_vad


def pcm_tone(sample_rate: int, duration_s: float, amplitude: float = 0.2) -> bytes:
    frames = int(sample_rate * duration_s)
    out = bytearray()
    for i in range(frames):
        sample = int(math.sin(2 * math.pi * 440 * i / sample_rate) * amplitude * 32767)
        out.extend(sample.to_bytes(2, "little", signed=True))
    return bytes(out)


def pcm_silence(sample_rate: int, duration_s: float) -> bytes:
    return b"\x00\x00" * int(sample_rate * duration_s)


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

    def test_tool_registry_executes_registered_tool(self) -> None:
        result = asyncio.run(ToolRegistry().execute("noop", "{}"))
        self.assertEqual(result, "noop completed")

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
