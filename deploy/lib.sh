#!/usr/bin/env bash
# Shared helpers for deploy/*.sh — source this file, do not execute it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/deploy"
COMPOSE_FILE="$DEPLOY_DIR/docker-compose.yml"
MT5_ENV_FILE="$DEPLOY_DIR/mt5.env"
CONTAINER="${MT5_CONTAINER:-mt5}"

PY64="/config/.wine/drive_c/users/abc/AppData/Local/Programs/Python/Python39/python.exe"
PY64_URL="https://www.python.org/ftp/python/3.9.13/python-3.9.13-amd64.exe"
LINUX_VENV="/config/mt5venv"
TERMINAL_WIN="C:/Program Files/MetaTrader 5/terminal64.exe"

compose() {
  if [[ ! -f "$MT5_ENV_FILE" ]]; then
    echo "Missing $MT5_ENV_FILE — copy deploy/mt5.env.example and set MT5_VNC_PASSWORD." >&2
    exit 1
  fi
  docker compose -f "$COMPOSE_FILE" --env-file "$MT5_ENV_FILE" "$@"
}

container_running() {
  docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true
}

wait_for_container() {
  local tries="${1:-60}"
  local i=0
  until container_running; do
    i=$((i + 1))
    if (( i >= tries )); then
      echo "Container $CONTAINER not running after ${tries}s." >&2
      exit 1
    fi
    sleep 1
  done
}

# Foreground command as user abc with Wine/DISPLAY env.
exec_abc() {
  docker exec -u abc \
    -e HOME=/config \
    -e WINEPREFIX=/config/.wine \
    -e DISPLAY=:1 \
    "$CONTAINER" bash -lc "$*"
}

# Detached command as user abc (terminal / long-running bridge).
exec_abc_d() {
  docker exec -d -u abc \
    -e HOME=/config \
    -e WINEPREFIX=/config/.wine \
    -e DISPLAY=:1 \
    "$CONTAINER" bash -lc "$*"
}

terminal_running() {
  docker exec "$CONTAINER" sh -c 'ps aux | grep -i terminal64 | grep -v grep' >/dev/null 2>&1
}

bridge_listening() {
  # ss/netstat may be absent from the image; /proc/net/tcp always works
  # (8001 = 0x1F41, state 0A = LISTEN).
  docker exec "$CONTAINER" sh -c 'ss -tlnp 2>/dev/null | grep -q ":8001 "' || \
    docker exec "$CONTAINER" sh -c 'netstat -tlnp 2>/dev/null | grep -q ":8001 "' || \
    docker exec "$CONTAINER" sh -c 'grep -qi ":1F41 .* 0A " /proc/net/tcp /proc/net/tcp6 2>/dev/null'
}
