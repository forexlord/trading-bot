#!/usr/bin/env bash
# Remove ONLY forex-bot + MT5 stack. Does NOT touch Sol3Hive/pm2/nginx/swap/ufw.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/deploy"

echo "==> Stopping systemd units..."
systemctl stop forex-bot mt5-bridge 2>/dev/null || true
systemctl disable forex-bot mt5-bridge 2>/dev/null || true
rm -f /etc/systemd/system/forex-bot.service /etc/systemd/system/mt5-bridge.service
systemctl daemon-reload 2>/dev/null || true

echo "==> Stopping MT5 compose stack..."
if [[ -f "$DEPLOY_DIR/mt5.env" ]]; then
  docker compose -f "$DEPLOY_DIR/docker-compose.yml" --env-file "$DEPLOY_DIR/mt5.env" down 2>/dev/null || true
else
  docker rm -f mt5 2>/dev/null || true
fi

if [[ "${WIPE_MT5_VOLUME:-0}" == "1" ]]; then
  echo "==> Removing Wine/MT5 volume (login state wiped)..."
  docker volume rm forex-bot-mt5-config 2>/dev/null || true
else
  echo "==> Keeping volume forex-bot-mt5-config (set WIPE_MT5_VOLUME=1 to delete)"
fi

# Legacy path from earlier ad-hoc installs
if [[ -d /opt/mt5 ]]; then
  echo "==> Removing legacy /opt/mt5..."
  (cd /opt/mt5 && docker compose down 2>/dev/null) || true
  rm -rf /opt/mt5
fi

if [[ "${WIPE_BOT:-0}" == "1" ]]; then
  echo "==> Removing /opt/forex-bot..."
  rm -rf /opt/forex-bot
else
  echo "==> Keeping /opt/forex-bot (set WIPE_BOT=1 to delete)"
fi

echo "==> Teardown complete."
