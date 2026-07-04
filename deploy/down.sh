#!/usr/bin/env bash
# Stop the MT5 container (keeps the named volume with Wine/login state).
# If mt5-bridge.service is enabled, stop it first so the supervisor does not
# immediately bring the container back.
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

if systemctl is-active --quiet mt5-bridge 2>/dev/null; then
  echo "==> Stopping mt5-bridge.service..."
  systemctl stop mt5-bridge
fi

if [[ -f "$MT5_ENV_FILE" ]]; then
  compose down
else
  docker rm -f "$CONTAINER" 2>/dev/null || true
fi
echo "==> MT5 container stopped (volume forex-bot-mt5-config kept)."
