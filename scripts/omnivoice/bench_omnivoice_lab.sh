#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB_DIR="${OMNIVOICE_LAB_DIR:-"$(cd "$ROOT_DIR/.." && pwd)/hermes-omnivoice-lab"}"
BIN="${OMNIVOICE_BIN:-$LAB_DIR/src/omnivoice.cpp/build/omnivoice-tts}"
MODEL="${OMNIVOICE_MODEL:-$LAB_DIR/models/omnivoice-base-Q8_0.gguf}"
CODEC="${OMNIVOICE_CODEC:-$LAB_DIR/models/omnivoice-tokenizer-F32.gguf}"
BACKEND="${OMNIVOICE_BACKEND:-Vulkan0}"
LANG="${OMNIVOICE_LANG:-Chinese}"
TEXT="${1:-你好，我是 Hermes。现在测试 OmniVoice 的自动音色和伪流式输出。}"
OUT="${OMNIVOICE_BENCH_OUT:-/tmp/omnivoice-bench.wav}"

export GGML_BACKEND="$BACKEND"
start_ms="$(date +%s%3N)"
printf '%s\n' "$TEXT" | "$BIN" --model "$MODEL" --codec "$CODEC" --format wav16 --lang "$LANG" --seed "${OMNIVOICE_SEED:-42}" -o "$OUT"
end_ms="$(date +%s%3N)"

bytes="$(wc -c < "$OUT" | tr -d ' ')"
printf 'OmniVoice bench ok: %sms, %s bytes, %s\n' "$((end_ms - start_ms))" "$bytes" "$OUT"
