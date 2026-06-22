#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${HERMES_STS_SYSTEMD_SERVICE_NAME:-hermes-sts-server.service}"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$USER_UNIT_DIR/$SERVICE_NAME"

step() {
  echo "==> $*"
}

if ! command -v systemctl >/dev/null 2>&1; then
  echo "Missing required command: systemctl" >&2
  exit 1
fi

if systemctl --user list-unit-files "$SERVICE_NAME" >/dev/null 2>&1; then
  step "Disabling and stopping $SERVICE_NAME"
  systemctl --user disable --now "$SERVICE_NAME" || true
fi

if [[ -f "$UNIT_PATH" ]]; then
  step "Removing user service: $UNIT_PATH"
  rm -f "$UNIT_PATH"
fi

step "Reloading user systemd manager"
systemctl --user daemon-reload
systemctl --user reset-failed "$SERVICE_NAME" >/dev/null 2>&1 || true

echo "Uninstalled user service: $SERVICE_NAME"
