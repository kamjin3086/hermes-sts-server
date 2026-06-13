from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from hermes_sts.audio import Utterance, pcm16_bytes_to_float32
from hermes_sts.config import Settings


@dataclass
class EnergyVad:
    settings: Settings
    in_speech: bool = False
    speech_ms: int = 0
    silence_ms: int = 0
    current: bytearray = field(default_factory=bytearray)

    def accept(self, raw_pcm16: bytes) -> tuple[str | None, Utterance | None]:
        audio = pcm16_bytes_to_float32(raw_pcm16)
        if audio.size == 0:
            return None, None

        frame_ms = int(audio.size / self.settings.sample_rate * 1000)
        rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
        voiced = rms >= self.settings.vad_energy_threshold

        event: str | None = None
        utterance: Utterance | None = None

        if voiced:
            self.speech_ms += frame_ms
            self.silence_ms = 0
            self.current.extend(raw_pcm16)
            if not self.in_speech and self.speech_ms >= self.settings.vad_start_ms:
                self.in_speech = True
                event = "speech_started"
        elif self.in_speech:
            self.silence_ms += frame_ms
            self.current.extend(raw_pcm16)
            if self.silence_ms >= self.settings.vad_end_ms:
                event, utterance = self._finish(rms)
        else:
            self.speech_ms = 0

        if self.in_speech:
            duration_ms = int(len(self.current) / 2 / self.settings.sample_rate * 1000)
            if duration_ms >= self.settings.vad_max_utterance_ms:
                event, utterance = self._finish(rms)

        return event, utterance

    def _finish(self, rms: float) -> tuple[str, Utterance | None]:
        raw = bytes(self.current)
        duration_ms = int(len(raw) / 2 / self.settings.sample_rate * 1000)
        self.reset()
        if duration_ms < self.settings.vad_min_utterance_ms:
            return "speech_stopped", None
        return "speech_stopped", Utterance(pcm16=raw, duration_ms=duration_ms, rms=rms)

    def reset(self) -> None:
        self.in_speech = False
        self.speech_ms = 0
        self.silence_ms = 0
        self.current.clear()
