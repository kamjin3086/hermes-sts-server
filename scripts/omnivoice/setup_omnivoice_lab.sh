#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB_DIR="${OMNIVOICE_LAB_DIR:-"$(cd "$ROOT_DIR/.." && pwd)/hermes-omnivoice-lab"}"
SRC_DIR="$LAB_DIR/src/omnivoice.cpp"
MODELS_DIR="$LAB_DIR/models"
REPO_URL="${OMNIVOICE_REPO_URL:-https://github.com/ServeurpersoCom/omnivoice.cpp.git}"
HF_REPO="${OMNIVOICE_HF_REPO:-Serveurperso/OmniVoice-GGUF}"

mkdir -p "$LAB_DIR/src" "$MODELS_DIR"

if [[ ! -d "$SRC_DIR/.git" ]]; then
  git clone --recursive "$REPO_URL" "$SRC_DIR"
else
  (
    cd "$SRC_DIR"
    git submodule update --init --recursive
  )
fi

if [[ "${OMNIVOICE_SKIP_BUILD:-0}" != "1" ]]; then
  (
    cd "$SRC_DIR"
    if [[ -x ./buildvulkan.sh ]]; then
      ./buildvulkan.sh
    else
      cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
      cmake --build build -j"$(nproc)"
    fi
  )
fi

download_model() {
  local name="$1"
  local target="$MODELS_DIR/$name"
  if [[ -f "$target" ]]; then
    return
  fi
  if command -v huggingface-cli >/dev/null 2>&1; then
    huggingface-cli download "$HF_REPO" "$name" --local-dir "$MODELS_DIR" --local-dir-use-symlinks False
  elif command -v hf >/dev/null 2>&1; then
    hf download "$HF_REPO" "$name" --local-dir "$MODELS_DIR"
  else
    curl -L "https://huggingface.co/$HF_REPO/resolve/main/$name" -o "$target"
  fi
}

download_model "omnivoice-base-Q8_0.gguf"
download_model "omnivoice-tokenizer-F32.gguf"

cat <<EOF
OmniVoice lab ready:
  bin:       $SRC_DIR/build/omnivoice-tts
  codec bin: $SRC_DIR/build/omnivoice-codec
  model:     $MODELS_DIR/omnivoice-base-Q8_0.gguf
  codec:     $MODELS_DIR/omnivoice-tokenizer-F32.gguf
EOF
