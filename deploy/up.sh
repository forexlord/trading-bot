#!/usr/bin/env bash
# One-shot bring-up for interactive use (not the systemd path).
# Starts the container, terminal, and bridge, then exits.
# For production, prefer: systemctl enable --now mt5-bridge  (runs supervise-mt5.sh).
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

if [[ ! -f "$MT5_ENV_FILE" ]]; then
  cp "$DEPLOY_DIR/mt5.env.example" "$MT5_ENV_FILE"
  echo "Created $MT5_ENV_FILE — edit MT5_VNC_PASSWORD, then re-run." >&2
  exit 1
fi

echo "==> Starting MT5 container..."
compose up -d

echo "==> Waiting for container..."
wait_for_container 90

echo "==> Waiting for terminal64.exe in Wine prefix..."
for i in $(seq 1 120); do
  if docker exec "$CONTAINER" test -f "/config/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe"; then
    break
  fi
  if (( i == 120 )); then
    echo "MT5 installer did not finish. Check: docker logs mt5 --tail 50" >&2
    exit 1
  fi
  sleep 5
done

docker exec "$CONTAINER" chown -R abc:abc /config 2>/dev/null || true

"$DEPLOY_DIR/start-mt5-terminal.sh"

# Retry IPC for a few minutes (auto-login after reboot often needs time).
echo "==> Waiting for MT5 IPC (login + Python integration)..."
IPC_OK=0
for i in $(seq 1 24); do
  if QUIET=1 "$DEPLOY_DIR/setup-mt5-bridge.sh"; then
    IPC_OK=1
    break
  fi
  echo "    attempt $i/24 failed — retry in 15s (log in via VNC if first boot)"
  sleep 15
done

if [[ "$IPC_OK" != "1" ]]; then
  cat >&2 <<'EOF'

Stack is partially up (container + terminal), but IPC/bridge failed.

One-time GUI steps (ssh -L 3300:127.0.0.1:3300 user@vps → http://localhost:3300):
  1. File → Login to Trade Account (same as .env)
  2. Tools → Options → Community → Python integration ON
  3. Tools → Options → Expert Advisors → Allow algorithmic trading ON
  4. File → Exit, then: ./deploy/start-mt5-terminal.sh
  5. Log in again, then: ./deploy/setup-mt5-bridge.sh

Or enable the supervisor (retries forever):
  sudo systemctl enable --now mt5-bridge
EOF
  exit 1
fi

echo
echo "==> Stack is up."
echo "    VNC:    ssh -L 3300:127.0.0.1:3300 user@vps  →  http://localhost:3300"
echo "    Bridge: 127.0.0.1:8001"
echo "    Status: ./deploy/status.sh"
echo "    Prod:   sudo cp deploy/*.service /etc/systemd/system/ && sudo systemctl enable --now mt5-bridge"
