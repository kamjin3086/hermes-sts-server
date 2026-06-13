from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


def pcm16_bytes_to_float32(raw: bytes) -> np.ndarray:
    if not raw:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def float32_to_pcm16_bytes(audio: np.ndarray) -> bytes:
    clipped = np.clip(audio, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16).tobytes()


def pcm16_bytes_to_int16_array(raw: bytes) -> np.ndarray:
    if not raw:
        return np.zeros(0, dtype=np.int16)
    return np.frombuffer(raw, dtype=np.int16)


def chunk_pcm16(raw: bytes, *, chunk_ms: int, sample_rate: int) -> list[bytes]:
    samples_per_chunk = max(1, int(sample_rate * chunk_ms / 1000))
    bytes_per_chunk = samples_per_chunk * 2
    return [raw[i : i + bytes_per_chunk] for i in range(0, len(raw), bytes_per_chunk)]


def tone_pcm16(
    *,
    text: str,
    sample_rate: int,
    duration_s: float | None = None,
    frequency: float = 440.0,
) -> bytes:
    duration = duration_s or min(2.5, max(0.35, 0.08 * len(text)))
    n = int(sample_rate * duration)
    t = np.arange(n, dtype=np.float32) / float(sample_rate)
    envelope = np.minimum(1.0, np.arange(n, dtype=np.float32) / max(1, int(sample_rate * 0.03)))
    tail = np.minimum(1.0, np.arange(n, 0, -1, dtype=np.float32) / max(1, int(sample_rate * 0.05)))
    wave = 0.10 * np.sin(2.0 * math.pi * frequency * t) * envelope * tail
    return float32_to_pcm16_bytes(wave)


@dataclass
class Utterance:
    pcm16: bytes
    duration_ms: int
    rms: float
