#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_SYSTEM=0
SKIP_QWEN=0

for arg in "$@"; do
  case "$arg" in
    --system) INSTALL_SYSTEM=1 ;;
    --skip-qwen) SKIP_QWEN=1 ;;
    -h|--help)
      cat <<'EOF'
Usage: ./scripts/bootstrap_fedora_amd.sh [--system] [--skip-qwen]

Prepares Hermes STS on Fedora + AMD/Vulkan.

  --system     Install Fedora packages with sudo dnf first.
  --skip-qwen  Skip hermes-tts-lab/qwentts.cpp setup.
EOF
      exit 0
      ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

step() {
  printf '\n==> %s\n' "$*"
}

if [[ "$INSTALL_SYSTEM" -eq 1 ]]; then
  step "Installing Fedora packages"
  sudo dnf install -y \
    git \
    cmake \
    gcc-c++ \
    python3.12 \
    nodejs \
    npm \
    curl \
    tar \
    vulkan-headers \
    vulkan-loader-devel \
    glslc \
    libshaderc-devel \
    spirv-headers-devel \
    spirv-tools-devel
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "Missing uv. Install it first: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "Missing npm. Rerun with --system or install nodejs/npm first." >&2
  exit 1
fi

step "Preparing Python environment"
"$ROOT/scripts/dev/setup_venv.sh"

step "Downloading STS fallback/STT models"
"$ROOT/scripts/dev/download_models.sh"

if [[ "$SKIP_QWEN" -eq 0 ]]; then
  step "Preparing hermes-tts-lab and Qwen3TTS"
  if [[ "$INSTALL_SYSTEM" -eq 1 ]]; then
    QWENTTS_INSTALL_SYSTEM_DEPS=1 "$ROOT/scripts/qwen/setup_qwentts_lab.sh"
  else
    "$ROOT/scripts/qwen/setup_qwentts_lab.sh"
  fi
fi

step "Building admin console"
npm --prefix "$ROOT/admin_ui" install
npm --prefix "$ROOT/admin_ui" run build

step "Done"
cat <<EOF
Start the service:
  $ROOT/scripts/service/start_sts_pipeline.sh

Open the console:
  http://127.0.0.1:8765/

Daily configuration belongs in the console. System packages, venv setup,
qwentts.cpp build, and first model downloads belong in this bootstrap script.
EOF
