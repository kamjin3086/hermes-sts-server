#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 /path/to/reference.wav" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB_DIR="${QWENTTS_LAB_DIR:-/home/kamjin/projects/hermes-tts-lab}"
CODEC_BIN="${QWENTTS_CODEC_BIN:-$LAB_DIR/src/qwentts.cpp/build/qwen-codec}"
MODEL="${QWENTTS_CPP_CODEC:-$LAB_DIR/models/qwen-tokenizer-12hz-Q4_K_M.gguf}"
TALKER="${QWENTTS_CPP_MODEL:-$LAB_DIR/models/qwen-talker-1.7b-base-Q4_K_M.gguf}"
BACKEND="${QWENTTS_CPP_BACKEND:-Vulkan0}"
REF_WAV="$1"

if [[ ! -x "$CODEC_BIN" ]]; then
  echo "Missing qwen-codec binary: $CODEC_BIN" >&2
  echo "Run ./scripts/qwen/setup_qwentts_lab.sh first." >&2
  exit 1
fi

if [[ ! -f "$MODEL" || ! -f "$TALKER" ]]; then
  echo "Missing Qwen3TTS model or codec GGUF under $LAB_DIR/models" >&2
  exit 1
fi

if [[ ! -f "$REF_WAV" ]]; then
  echo "Missing reference wav: $REF_WAV" >&2
  exit 1
fi

cd "$ROOT"
env GGML_BACKEND="$BACKEND" "$CODEC_BIN" --model "$MODEL" --talker "$TALKER" -i "$REF_WAV"

base="${REF_WAV%.*}"
echo "Clone assets ready:"
echo "  QWENTTS_CPP_REF_SPK=$base.spk"
echo "  QWENTTS_CPP_REF_RVQ=$base.rvq"
