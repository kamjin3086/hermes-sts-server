#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -x ".venv-sts/bin/python" ]]; then
  PYTHON_BIN=".venv-sts/bin/python"
else
  PYTHON_BIN="${PYTHON:-python3}"
fi

echo "==> Python"
"$PYTHON_BIN" --version

echo "==> Compile check"
"$PYTHON_BIN" -m compileall -q hermes_sts tests

echo "==> Unit tests"
"$PYTHON_BIN" -m unittest discover -s tests -p "test_*.py" -v
