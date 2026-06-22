#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if [[ -x ".venv-sts/bin/python" ]]; then
  PYTHON_BIN=".venv-sts/bin/python"
else
  PYTHON_BIN="${PYTHON:-python3}"
fi

"$PYTHON_BIN" - <<'PY'
from __future__ import annotations

import time
import wave
from dataclasses import replace
from pathlib import Path

from hermes_sts.config import settings
from hermes_sts.tts import build_tts

out = Path("/home/kamjin/projects/hermes-tts-lab/samples/provider_smoke.wav")
out.parent.mkdir(parents=True, exist_ok=True)

smoke_settings = replace(
    settings,
    tts_provider="qwen3tts",
    qwentts_cpp_voice_mode="default",
    qwentts_cpp_seed=42,
)

started = time.perf_counter()
tts = build_tts(smoke_settings)
if hasattr(tts, "_synthesize_sync"):
    pcm16 = tts._synthesize_sync("你好，我是 Qwen3TTS，本地语音已经可以使用。")
else:
    import asyncio

    pcm16 = asyncio.run(tts.synthesize("你好，我是 Qwen3TTS，本地语音已经可以使用。"))
elapsed = time.perf_counter() - started

with wave.open(str(out), "wb") as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(smoke_settings.sample_rate)
    wf.writeframes(pcm16)

print(f"provider={smoke_settings.tts_provider}")
print(f"backend={smoke_settings.qwentts_cpp_backend}")
print(f"voice_mode={smoke_settings.qwentts_cpp_voice_mode}")
print(f"seed={smoke_settings.qwentts_cpp_seed}")
print(f"pcm_bytes={len(pcm16)}")
print(f"audio_seconds={len(pcm16) / 2 / smoke_settings.sample_rate:.3f}")
print(f"elapsed_seconds={elapsed:.3f}")
print(f"wav={out}")
PY
