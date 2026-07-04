#!/usr/bin/env bash
# Start (or restart) terminal64.exe inside the MT5 container with wine64.
# Safe to run repeatedly. Does not log into Exness — that is one-time via VNC.
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

wait_for_container 30
docker exec "$CONTAINER" chown -R abc:abc /config 2>/dev/null || true

if terminal_running; then
  echo "==> terminal64.exe already running"
  exit 0
fi

echo "==> Starting terminal64.exe (wine64)..."
exec_abc_d "wine64 \"$TERMINAL_WIN\" >> /config/mt5_terminal.log 2>&1"
sleep 8

if terminal_running; then
  echo "==> terminal64.exe is up"
else
  echo "==> terminal64.exe did not stay up — check VNC and /config/mt5_terminal.log" >&2
  docker exec "$CONTAINER" tail -20 /config/mt5_terminal.log || true
  exit 1
fi
