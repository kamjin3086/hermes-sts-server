#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="$ROOT/.venv-sts/bin/python"

step() {
  echo "==> $*"
}

port_owner_pids() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp "sport = :$port" 2>/dev/null |
      sed -nE 's/.*pid=([0-9]+).*/\1/p' |
      sort -u
  elif command -v lsof >/dev/null 2>&1; then
    lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u
  fi
}

process_cmdline() {
  local pid="$1"
  if [[ -r "/proc/$pid/cmdline" ]]; then
    tr '\0' ' ' <"/proc/$pid/cmdline"
  else
    ps -p "$pid" -o args= 2>/dev/null || true
  fi
}

stop_pid() {
  local pid="$1"
  step "Stopping STS process (PID $pid)"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    if kill -0 "$pid" 2>/dev/null; then
      sleep 0.25
    else
      return 0
    fi
  done
  step "Force stopping STS process (PID $pid)"
  kill -9 "$pid" 2>/dev/null || true
  return 0
}

if [[ -x "$PYTHON" ]]; then
  cd "$ROOT"
  PORT="$("$PYTHON" - <<'PY'
from hermes_sts.config import settings
print(settings.port)
PY
)"
else
  PORT=8765
fi
owners=()
mapfile -t owners < <(port_owner_pids "$PORT")

if [[ "${#owners[@]}" -eq 0 ]]; then
  echo "STS is not listening on port $PORT."
  exit 0
fi

stopped=0
for owner_pid in "${owners[@]}"; do
  [[ -z "$owner_pid" ]] && continue
  cmd="$(process_cmdline "$owner_pid")"
  if [[ "$cmd" == *"hermes_sts"* || "$cmd" == *"$PYTHON -m hermes_sts"* ]]; then
    stop_pid "$owner_pid"
    stopped=1
  else
    echo "Port $PORT is occupied by a non-STS process (PID $owner_pid): $cmd" >&2
    exit 1
  fi
done

if [[ "$stopped" -eq 1 ]]; then
  echo "STS stopped on port $PORT."
else
  echo "No STS process stopped."
fi
