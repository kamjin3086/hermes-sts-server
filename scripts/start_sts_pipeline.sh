#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv-sts/bin/python"
LOG_DIR="$ROOT/logs"
STDOUT_LOG="$LOG_DIR/sts-server.out.log"
STDERR_LOG="$LOG_DIR/sts-server.err.log"
ENV_FILE="$ROOT/.env"

dotenv_value() {
  local name="$1"
  local default="$2"
  if [[ -f "$ENV_FILE" ]]; then
    local line
    line="$(grep -E "^${name}=" "$ENV_FILE" | head -n1 || true)"
    if [[ -n "$line" ]]; then
      local value="${line#*=}"
      value="${value%$'\r'}"
      value="${value%\"}"
      value="${value#\"}"
      printf '%s' "$value"
      return
    fi
  fi
  printf '%s' "$default"
}

http_ok() {
  local url="$1"
  local api_key="${2:-}"
  if [[ -n "$api_key" ]]; then
    curl -fsS --max-time 5 -H "Authorization: Bearer $api_key" "$url" >/dev/null 2>&1
  else
    curl -fsS --max-time 5 "$url" >/dev/null 2>&1
  fi
}

step() {
  echo "==> $*"
}

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing STS venv Python: $PYTHON. Run scripts/setup_venv.sh first." >&2
  exit 1
fi

cd "$ROOT"
mkdir -p "$LOG_DIR"

HOST_NAME="$(dotenv_value HERMES_STS_HOST 127.0.0.1)"
PORT="$(dotenv_value HERMES_STS_PORT 8765)"
CONNECT_HOST="$HOST_NAME"
if [[ "$CONNECT_HOST" == "0.0.0.0" || "$CONNECT_HOST" == "::" ]]; then
  CONNECT_HOST="127.0.0.1"
fi
HEALTH_URL="http://${CONNECT_HOST}:${PORT}/health"
HERMES_BASE_URL="$(dotenv_value HERMES_BASE_URL http://127.0.0.1:8642/v1)"
HERMES_API_KEY="$(dotenv_value HERMES_API_KEY "")"
HERMES_MODELS_URL="${HERMES_BASE_URL%/}/models"

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

stop_sts_on_port_if_owned() {
  local owners=()
  mapfile -t owners < <(port_owner_pids "$PORT")
  for owner_pid in "${owners[@]}"; do
    [[ -z "$owner_pid" ]] && continue
    local cmd
    cmd="$(process_cmdline "$owner_pid")"
    if [[ "$cmd" == *"hermes_sts"* || "$cmd" == *"$PYTHON -m hermes_sts"* ]]; then
      step "Stopping STS process on port $PORT (PID $owner_pid)"
      kill "$owner_pid" 2>/dev/null || true
      for _ in $(seq 1 20); do
        if kill -0 "$owner_pid" 2>/dev/null; then
          sleep 0.25
        else
          break
        fi
      done
      if kill -0 "$owner_pid" 2>/dev/null; then
        kill -9 "$owner_pid" 2>/dev/null || true
      fi
    else
      echo "Port $PORT is occupied by a non-STS process (PID $owner_pid): $cmd" >&2
      exit 1
    fi
  done
}

step "Checking Hermes API"
if http_ok "$HERMES_MODELS_URL" "$HERMES_API_KEY"; then
  echo "Hermes API: OK"
else
  echo "Warning: Hermes API is not reachable at $HERMES_MODELS_URL. STS can still start, but LLM responses may use fallback."
fi

step "Restarting STS service"
stop_sts_on_port_if_owned

step "Starting STS server on ${HOST_NAME}:${PORT}"
if command -v setsid >/dev/null 2>&1; then
  setsid -f "$PYTHON" -m hermes_sts >"$STDOUT_LOG" 2>"$STDERR_LOG" < /dev/null
else
  nohup "$PYTHON" -m hermes_sts >"$STDOUT_LOG" 2>"$STDERR_LOG" < /dev/null &
fi

started=0
for _ in $(seq 1 30); do
  sleep 1
  if http_ok "$HEALTH_URL"; then
    started=1
    break
  fi
done
if [[ "$started" -ne 1 ]]; then
  echo "STDERR tail:"
  tail -n 40 "$STDERR_LOG" || true
  echo "STS did not become healthy on $HEALTH_URL" >&2
  exit 1
fi
echo "STS started: $HEALTH_URL"

step "Current STS health"
curl -fsS "$HEALTH_URL"
echo
echo "WebSocket endpoint: ws://${CONNECT_HOST}:${PORT}/v1/realtime"
echo "Logs:"
echo "  $STDOUT_LOG"
echo "  $STDERR_LOG"
