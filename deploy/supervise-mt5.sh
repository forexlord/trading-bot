#!/usr/bin/env bash
# Long-running supervisor: keep the MT5 container, terminal64, and RPyC bridge up.
# Used by mt5-bridge.service (Type=simple). Safe to run interactively for debugging.
#
# On reboot, MT5 may auto-login from the Wine volume. We retry IPC for a while
# before starting the bridge; if IPC never succeeds we keep retrying (do not exit).
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

POLL_SECONDS="${POLL_SECONDS:-20}"
IPC_RETRY_SECONDS="${IPC_RETRY_SECONDS:-15}"
# How long to wait for IPC after terminal start before starting bridge anyway is never —
# we only start the bridge after a successful IPC test (unless SKIP_IPC_TEST=1).

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

ensure_env() {
  if [[ ! -f "$MT5_ENV_FILE" ]]; then
    log "ERROR: missing $MT5_ENV_FILE"
    exit 1
  fi
}

ensure_container() {
  if ! container_running; then
    log "starting container..."
    compose up -d
    wait_for_container 90
  fi
}

wait_for_terminal_binary() {
  local i
  for i in $(seq 1 120); do
    if docker exec "$CONTAINER" test -f "/config/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe"; then
      return 0
    fi
    sleep 5
  done
  log "WARN: terminal64.exe not installed yet"
  return 1
}

ensure_terminal() {
  if terminal_running; then
    return 0
  fi
  log "starting terminal64.exe..."
  "$DEPLOY_DIR/start-mt5-terminal.sh" || true
}

ipc_ok() {
  exec_abc "
    wine64 '$PY64' -c \"
import MetaTrader5 as mt5
import sys
ok = mt5.initialize(path='$TERMINAL_WIN', timeout=60000)
sys.exit(0 if ok else 1)
\"
  " >/dev/null 2>&1
}

ensure_wine_python() {
  docker exec "$CONTAINER" chown -R abc:abc /config 2>/dev/null || true

  exec_abc "
    if [ ! -x $LINUX_VENV/bin/python ]; then
      python3 -m venv $LINUX_VENV
      $LINUX_VENV/bin/pip install --no-deps mt5linux==0.1.9 rpyc==5.0.1 plumbum numpy
    fi
  "

  if ! docker exec "$CONTAINER" test -f "$PY64"; then
    log "installing 64-bit Wine Python (one-time)..."
    exec_abc "
      cd /tmp
      curl -fsSL -o python-3.9.13-amd64.exe $PY64_URL
      wine64 python-3.9.13-amd64.exe /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1
    "
  fi

  # numpy<2: the MetaTrader5 wheel is built against the numpy 1.x ABI; with
  # numpy 2.x "import MetaTrader5" fails and IPC retries forever.
  exec_abc "wine64 '$PY64' -m pip install -q \"numpy<2\" MetaTrader5 rpyc==5.0.1 plumbum" || true
}

start_bridge() {
  log "stopping stale bridge processes..."
  docker exec "$CONTAINER" sh -c 'pkill -f "[s]erver.py" 2>/dev/null; pkill -f "[m]t5linux" 2>/dev/null; sleep 1' || true
  # chown the log: this exec runs as root, but the bridge appends as abc — a
  # root-owned log makes the detached bridge die on the redirect.
  docker exec "$CONTAINER" sh -c 'rm -rf /config/mt5linux; : > /config/mt5srv.log; chown abc:abc /config/mt5srv.log' || true

  log "starting RPyC bridge on :8001..."
  exec_abc_d "
    $LINUX_VENV/bin/python -m mt5linux \
      '$PY64' \
      --host 0.0.0.0 -p 8001 -s /config/mt5linux -w wine64 \
      >> /config/mt5srv.log 2>&1
  "
  sleep 4
}

ensure_bridge() {
  if bridge_listening; then
    return 0
  fi

  ensure_wine_python

  if [[ "${SKIP_IPC_TEST:-0}" == "1" ]]; then
    start_bridge
    return 0
  fi

  if ipc_ok; then
    start_bridge
    if bridge_listening; then
      log "bridge up"
    else
      log "WARN: bridge start failed — see /config/mt5srv.log"
    fi
  else
    log "IPC not ready (login / Python integration). Will retry."
  fi
}

# --- main loop ---
ensure_env
log "supervisor starting (poll=${POLL_SECONDS}s)"

# First-boot install can take minutes; do not exit the unit.
while true; do
  ensure_container || true
  wait_for_terminal_binary || true
  ensure_terminal || true
  ensure_bridge || true
  sleep "$POLL_SECONDS"
done
