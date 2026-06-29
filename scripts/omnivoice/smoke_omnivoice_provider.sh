#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

export STS_TTS_PROVIDER=omnivoice
export OMNIVOICE_BIN="${OMNIVOICE_BIN:-$(cd "$ROOT_DIR/.." && pwd)/hermes-omnivoice-lab/src/omnivoice.cpp/build/omnivoice-tts}"
export OMNIVOICE_CODEC_BIN="${OMNIVOICE_CODEC_BIN:-$(cd "$ROOT_DIR/.." && pwd)/hermes-omnivoice-lab/src/omnivoice.cpp/build/omnivoice-codec}"
export OMNIVOICE_MODEL="${OMNIVOICE_MODEL:-$(cd "$ROOT_DIR/.." && pwd)/hermes-omnivoice-lab/models/omnivoice-base-Q8_0.gguf}"
export OMNIVOICE_CODEC="${OMNIVOICE_CODEC:-$(cd "$ROOT_DIR/.." && pwd)/hermes-omnivoice-lab/models/omnivoice-tokenizer-F32.gguf}"

"${PYTHON:-python}" - <<'PY'
import asyncio
from hermes_sts.config import Settings
from hermes_sts.tts import TtsVoice, build_tts

async def main():
    settings = Settings()
    provider = build_tts(settings)
    pcm = await provider.synthesize("你好，这是 OmniVoice provider 的自动音色烟测。", voice=TtsVoice.from_settings(settings))
    if not pcm:
        raise SystemExit("empty pcm from synthesize")
    chunks = []
    async for chunk in provider.stream_pcm("你好，这是 OmniVoice 伪流式烟测。", voice=TtsVoice.from_settings(settings)):
        chunks.append(chunk)
    if not chunks:
        raise SystemExit("empty pcm from stream_pcm")
    print(f"OmniVoice provider smoke ok: synth={len(pcm)} bytes stream_first={sum(len(item) for item in chunks)} bytes")

asyncio.run(main())
PY
