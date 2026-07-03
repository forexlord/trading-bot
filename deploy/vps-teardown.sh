#!/usr/bin/env bash
# Remove ONLY forex-bot + MT5 docker stack. Does NOT touch Sol3Hive/pm2/nginx/swap/ufw.
set -euo pipefail

echo "==> Stopping forex-bot systemd unit (if installed)..."
systemctl stop forex-bot 2>/dev/null || true
systemctl disable forex-bot 2>/dev/null || true
rm -f /etc/systemd/system/forex-bot.service
systemctl daemon-reload 2>/dev/null || true

echo "==> Removing /opt/forex-bot..."
rm -rf /opt/forex-bot

echo "==> Stopping MT5 container and wiping Wine/MT5 state..."
if [ -d /opt/mt5 ]; then
  cd /opt/mt5
  docker compose down 2>/dev/null || true
  rm -rf /opt/mt5/config
  echo "    (kept /opt/mt5/docker-compose.yaml — recreate container with: cd /opt/mt5 && docker compose up -d)"
else
  docker rm -f mt5 2>/dev/null || true
fi

echo "==> Teardown complete. Sol3Hive/pm2/nginx untouched."
echo "    Next: cd /opt/mt5 && docker compose up -d  (fresh MT5 install, ~5-10 min)"
