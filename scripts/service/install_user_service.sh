#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE_NAME="${HERMES_STS_SYSTEMD_SERVICE_NAME:-hermes-sts-server.service}"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$USER_UNIT_DIR/$SERVICE_NAME"
PYTHON="$ROOT/.venv-sts/bin/python"
LOG_DIR="$ROOT/logs"

step() {
  echo "==> $*"
}

require_command() {
  local command_name="$1"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Missing required command: $command_name" >&2
    exit 1
  fi
}

require_command systemctl

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing STS venv Python: $PYTHON. Run scripts/dev/setup_venv.sh first." >&2
  exit 1
fi

mkdir -p "$USER_UNIT_DIR" "$LOG_DIR"

step "Writing user service: $UNIT_PATH"
cat >"$UNIT_PATH" <<EOF
[Unit]
Description=Hermes STS Server
Documentation=https://github.com/kamjin3086/hermes-sts-server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=PYTHONUNBUFFERED=1
ExecStart=$PYTHON -m hermes_sts
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

step "Reloading user systemd manager"
systemctl --user daemon-reload

if systemctl --user is-active --quiet "$SERVICE_NAME"; then
  step "Restarting $SERVICE_NAME"
  systemctl --user restart "$SERVICE_NAME"
else
  if [[ -x "$ROOT/scripts/service/stop_sts_pipeline.sh" ]]; then
    step "Stopping any standalone STS process on the configured port"
    "$ROOT/scripts/service/stop_sts_pipeline.sh" || true
  fi
  step "Enabling and starting $SERVICE_NAME"
  systemctl --user enable --now "$SERVICE_NAME"
fi

step "Service status"
systemctl --user --no-pager --full status "$SERVICE_NAME"

echo
echo "Installed user service: $SERVICE_NAME"
echo "Manage it with:"
echo "  systemctl --user status $SERVICE_NAME"
echo "  systemctl --user restart $SERVICE_NAME"
echo "  systemctl --user stop $SERVICE_NAME"
echo "  journalctl --user -u $SERVICE_NAME -f"
