from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from hermes_sts.audio import Utterance, pcm16_bytes_to_float32, pcm16_bytes_to_int16_array
from hermes_sts.config import Settings


class VadProvider(Protocol):
    def accept(self, raw_pcm16: bytes) -> tuple[str | None, Utterance | None]:
        ...

    def reset(self) -> None:
        ...


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


@dataclass
class SherpaSileroVad:
    settings: Settings
    vad: object = field(init=False)
    window_size: int = field(init=False)
    buffer: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    pre_roll: bytearray = field(default_factory=bytearray)
    current: bytearray = field(default_factory=bytearray)
    started: bool = False
    silence_ms: int = 0
    pending: list[Utterance] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.settings.sherpa_silero_vad_model:
            raise RuntimeError("SHERPA_SILERO_VAD_MODEL is required for STS_VAD_PROVIDER=sherpa_silero")
        try:
            import sherpa_onnx  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("sherpa-onnx is required for STS_VAD_PROVIDER=sherpa_silero") from exc

        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = self.settings.sherpa_silero_vad_model
        config.silero_vad.threshold = self.settings.vad_threshold
        config.silero_vad.min_silence_duration = self.settings.vad_min_silence_seconds
        config.silero_vad.min_speech_duration = self.settings.vad_min_utterance_ms / 1000.0
        config.silero_vad.max_speech_duration = self.settings.vad_max_utterance_ms / 1000.0
        config.sample_rate = self.settings.sample_rate
        self.window_size = int(config.silero_vad.window_size)
        self.vad = sherpa_onnx.VoiceActivityDetector(
            config,
            buffer_size_in_seconds=self.settings.vad_buffer_seconds,
        )

    def accept(self, raw_pcm16: bytes) -> tuple[str | None, Utterance | None]:
        if self.pending:
            return "speech_stopped", self.pending.pop(0)

        samples = pcm16_bytes_to_int16_array(raw_pcm16).astype(np.float32) / 32768.0
        if samples.size == 0:
            return None, None

        event: str | None = None
        frame_ms = int(samples.size / self.settings.sample_rate * 1000)
        rms = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0
        energy_voiced = rms >= self.settings.vad_energy_threshold

        max_pre_roll_bytes = int(self.settings.sample_rate * 2 * 0.5)
        if not self.started:
            self.pre_roll.extend(raw_pcm16)
            if len(self.pre_roll) > max_pre_roll_bytes:
                del self.pre_roll[: len(self.pre_roll) - max_pre_roll_bytes]
        else:
            self.current.extend(raw_pcm16)
            if energy_voiced:
                self.silence_ms = 0
            else:
                self.silence_ms += frame_ms

        self.buffer = np.concatenate([self.buffer, samples])
        offset = 0
        while offset + self.window_size <= len(self.buffer):
            self.vad.accept_waveform(self.buffer[offset : offset + self.window_size])
            offset += self.window_size
            if not self.started and self.vad.is_speech_detected():
                self.started = True
                self.current.extend(self.pre_roll)
                self.pre_roll.clear()
                self.silence_ms = 0
                event = event or "speech_started"

            while not self.vad.empty():
                segment = self.vad.front
                self.vad.pop()
                utterance = self._segment_to_utterance(np.asarray(segment.samples, dtype=np.float32))
                if utterance is None:
                    utterance = self._finish_current(rms)
                if utterance is not None:
                    self.pending.append(utterance)
                self._clear_current()

        if offset:
            self.buffer = self.buffer[offset:]
        if not self.started and len(self.buffer) > self.window_size * 10:
            self.buffer = self.buffer[-self.window_size * 10 :]

        fallback_end_ms = max(
            self.settings.vad_end_ms,
            int(self.settings.vad_min_silence_seconds * 1000),
        )
        if self.started and self.silence_ms >= fallback_end_ms:
            utterance = self._finish_current(rms)
            if utterance is not None:
                self.pending.append(utterance)
            self._clear_current()

        if self.pending:
            return "speech_stopped", self.pending.pop(0)
        return event, None

    def _segment_to_utterance(self, samples: np.ndarray) -> Utterance | None:
        if samples.size == 0:
            return None
        duration_ms = int(samples.size / self.settings.sample_rate * 1000)
        if duration_ms < self.settings.vad_min_utterance_ms:
            return None
        rms = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0
        pcm16 = np.clip(samples, -1.0, 1.0)
        raw = (pcm16 * 32767.0).astype(np.int16).tobytes()
        return Utterance(pcm16=raw, duration_ms=duration_ms, rms=rms)

    def _finish_current(self, rms: float) -> Utterance | None:
        raw = bytes(self.current)
        duration_ms = int(len(raw) / 2 / self.settings.sample_rate * 1000)
        if duration_ms < self.settings.vad_min_utterance_ms:
            return None
        return Utterance(pcm16=raw, duration_ms=duration_ms, rms=rms)

    def _clear_current(self) -> None:
        self.started = False
        self.silence_ms = 0
        self.current.clear()

    def reset(self) -> None:
        self.buffer = np.zeros(0, dtype=np.float32)
        self.pre_roll.clear()
        self.current.clear()
        self.started = False
        self.silence_ms = 0
        self.pending.clear()
        if hasattr(self.vad, "reset"):
            self.vad.reset()


def build_vad(settings: Settings) -> VadProvider:
    provider = settings.vad_provider.strip().lower()
    if provider == "energy":
        return EnergyVad(settings)
    if provider == "sherpa_silero":
        return SherpaSileroVad(settings)
    raise RuntimeError(f"Unsupported STS_VAD_PROVIDER={settings.vad_provider!r}")
