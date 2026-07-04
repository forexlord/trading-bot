#!/usr/bin/env bash
# Install systemd units for MT5 supervisor + forex-bot (run as root on the VPS).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/deploy"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

if [[ ! -f "$DEPLOY_DIR/mt5.env" ]]; then
  echo "Missing $DEPLOY_DIR/mt5.env — copy mt5.env.example and set MT5_VNC_PASSWORD." >&2
  exit 1
fi

if [[ ! -f "$REPO_ROOT/.env" ]]; then
  echo "Missing $REPO_ROOT/.env — copy .env.example and set MT5/Telegram secrets." >&2
  exit 1
fi

# The unit files hardcode /opt/forex-bot in ExecStart/WorkingDirectory.
if [[ "$REPO_ROOT" != "/opt/forex-bot" ]]; then
  echo "WARNING: repo is at $REPO_ROOT but the .service files hardcode /opt/forex-bot." >&2
  echo "         Move the repo or edit the units before enabling them." >&2
fi

chmod +x "$DEPLOY_DIR"/*.sh

if ! id forexbot &>/dev/null; then
  useradd -r -s /usr/sbin/nologin forexbot
fi

mkdir -p "$REPO_ROOT/logs" "$REPO_ROOT/state" "$REPO_ROOT/data"
# Bot needs read access to code + .env; keep secrets readable only by root+forexbot.
chown -R root:forexbot "$REPO_ROOT"
chmod 750 "$REPO_ROOT"
chmod 640 "$REPO_ROOT/.env"
# Writable dirs LAST — the repo-wide chown above would revert them to root.
chown -R forexbot:forexbot "$REPO_ROOT/logs" "$REPO_ROOT/state" "$REPO_ROOT/data"
# Scripts/compose still run as root via mt5-bridge.service.
chmod 755 "$DEPLOY_DIR"/*.sh

cp "$DEPLOY_DIR/mt5-bridge.service" /etc/systemd/system/
cp "$DEPLOY_DIR/forex-bot.service" /etc/systemd/system/
systemctl daemon-reload

systemctl enable mt5-bridge
systemctl restart mt5-bridge

echo "==> mt5-bridge started. Check: journalctl -u mt5-bridge -f"
echo "    When ./deploy/status.sh shows Bot → MT5 OK, enable the bot:"
echo "      systemctl enable --now forex-bot"
