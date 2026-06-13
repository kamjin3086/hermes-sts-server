#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS_DIR="$ROOT/models"
mkdir -p "$MODELS_DIR"

download_if_missing() {
  local url="$1"
  local output="$2"
  if [[ -f "$output" ]]; then
    echo "Exists: $output"
    return
  fi
  echo "Downloading: $url"
  curl -L --retry 3 --connect-timeout 30 -o "$output" "$url"
}

download_if_missing \
  "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx" \
  "$MODELS_DIR/silero_vad.onnx"

SENSE_DIR="$MODELS_DIR/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
mkdir -p "$SENSE_DIR"
SENSE_BASE="https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main"
download_if_missing "$SENSE_BASE/model.int8.onnx" "$SENSE_DIR/model.int8.onnx"
download_if_missing "$SENSE_BASE/tokens.txt" "$SENSE_DIR/tokens.txt"

KOKORO_DIR="$MODELS_DIR/kokoro-multi-lang-v1_0"
KOKORO_ARCHIVE="$MODELS_DIR/kokoro-multi-lang-v1_0.tar.bz2"
if [[ ! -f "$KOKORO_DIR/model.onnx" ]]; then
  download_if_missing \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-multi-lang-v1_0.tar.bz2" \
    "$KOKORO_ARCHIVE"
  echo "Extracting Kokoro model..."
  tar -xf "$KOKORO_ARCHIVE" -C "$MODELS_DIR"
else
  echo "Exists: $KOKORO_DIR"
fi

echo "Models are ready under: $MODELS_DIR"
