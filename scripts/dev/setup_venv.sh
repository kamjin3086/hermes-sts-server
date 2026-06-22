#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="$ROOT/.venv-sts"
MIRROR="${PYPI_INDEX_URL:-}"

if [[ -z "$MIRROR" ]]; then
  MIRROR="https://pypi.org/simple"
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "Missing uv. Install it first: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

if [[ ! -d "$VENV" ]]; then
  uv venv --python 3.12 "$VENV"
fi

uv pip install --python "$VENV/bin/python" --no-cache --index-url "$MIRROR" -e "$ROOT[sherpa]"

echo "STS venv ready: $VENV"
