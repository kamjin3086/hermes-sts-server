#!/usr/bin/env bash
set -euo pipefail

LAB_DIR="${QWENTTS_LAB_DIR:-/home/kamjin/projects/hermes-tts-lab}"
SRC_DIR="$LAB_DIR/src/qwentts.cpp"
MODELS_DIR="$LAB_DIR/models"

mkdir -p "$LAB_DIR/src" "$MODELS_DIR" "$LAB_DIR/build" "$LAB_DIR/samples" "$LAB_DIR/benchmarks"

if [[ ! -d "$SRC_DIR/.git" ]]; then
  git clone --recurse-submodules https://github.com/ServeurpersoCom/qwentts.cpp.git "$SRC_DIR"
fi

missing_vulkan_deps=0
if ! command -v glslc >/dev/null 2>&1 || [[ ! -f /usr/include/vulkan/vulkan.h ]] || [[ ! -f /usr/include/spirv/unified1/spirv.hpp ]]; then
  missing_vulkan_deps=1
fi

if [[ "$missing_vulkan_deps" -eq 1 && "${QWENTTS_INSTALL_SYSTEM_DEPS:-0}" == "1" ]]; then
  sudo dnf install -y \
    vulkan-headers \
    vulkan-loader-devel \
    glslc \
    libshaderc-devel \
    spirv-headers-devel \
    spirv-tools-devel
  missing_vulkan_deps=0
fi

if [[ "$missing_vulkan_deps" -eq 1 ]]; then
  echo "Missing Vulkan build dependencies." >&2
  echo "Install them with:" >&2
  echo "  sudo dnf install -y vulkan-headers vulkan-loader-devel glslc libshaderc-devel spirv-headers-devel spirv-tools-devel" >&2
  echo "Or rerun with QWENTTS_INSTALL_SYSTEM_DEPS=1 ./scripts/qwen/setup_qwentts_lab.sh" >&2
  exit 1
fi

cmake -S "$SRC_DIR" -B "$SRC_DIR/build" -DGGML_VULKAN=ON
cmake --build "$SRC_DIR/build" --config Release -j "$(nproc)"

if command -v hf >/dev/null 2>&1; then
  HF_DOWNLOAD=(hf download)
elif command -v huggingface-cli >/dev/null 2>&1; then
  HF_DOWNLOAD=(huggingface-cli download)
else
  echo "Missing hf/huggingface-cli. Install huggingface_hub in an isolated tool venv first." >&2
  exit 1
fi

"${HF_DOWNLOAD[@]}" Serveurperso/Qwen3-TTS-GGUF \
  qwen-talker-1.7b-base-Q4_K_M.gguf \
  qwen-tokenizer-12hz-Q4_K_M.gguf \
  --local-dir "$MODELS_DIR"

echo "QwenTTS lab ready:"
echo "  bin=$SRC_DIR/build/qwen-tts"
echo "  model=$MODELS_DIR/qwen-talker-1.7b-base-Q4_K_M.gguf"
echo "  codec=$MODELS_DIR/qwen-tokenizer-12hz-Q4_K_M.gguf"
