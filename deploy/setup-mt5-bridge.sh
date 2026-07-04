#!/usr/bin/env bash
# Install 64-bit Wine Python (once) and start the mt5linux RPyC bridge on :8001.
#
# The stock gmag11 image auto-starts a broken mt5linux (Unknown switch -w) and
# ships 32-bit Python, which cannot IPC to terminal64.exe. This script is the
# supported path.
#
# Prerequisites (one-time, via VNC http://localhost:3300):
#   - File → Login to Trade Account (Exness)
#   - Tools → Options → Community → Python integration ON
#   - Tools → Options → Expert Advisors → Allow algorithmic trading ON
#   - Restart MT5 once after enabling Python integration
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

SKIP_IPC_TEST="${SKIP_IPC_TEST:-0}"
QUIET="${QUIET:-0}"

wait_for_container 30
docker exec "$CONTAINER" chown -R abc:abc /config 2>/dev/null || true

echo "==> Stopping any existing bridge..."
docker exec "$CONTAINER" sh -c 'pkill -f "[s]erver.py" 2>/dev/null; pkill -f "[m]t5linux" 2>/dev/null; sleep 2' || true

echo "==> Ensuring Linux-side launcher venv..."
exec_abc "
  if [ ! -x $LINUX_VENV/bin/python ]; then
    python3 -m venv $LINUX_VENV
    $LINUX_VENV/bin/pip install --no-deps mt5linux==0.1.9 rpyc==5.0.1 plumbum numpy
  fi
"

if ! docker exec "$CONTAINER" test -f "$PY64"; then
  echo "==> Installing 64-bit Windows Python in Wine (one-time)..."
  exec_abc "
    cd /tmp
    curl -fsSL -o python-3.9.13-amd64.exe $PY64_URL
    wine64 python-3.9.13-amd64.exe /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1
  "
fi

echo "==> Installing MetaTrader5 + rpyc==5.0.1 on 64-bit Wine Python..."
# numpy<2: the MetaTrader5 wheel is built against the numpy 1.x ABI; with
# numpy 2.x "import MetaTrader5" fails with numpy.core.multiarray errors.
exec_abc "wine64 '$PY64' -m pip install -q \"numpy<2\" MetaTrader5 rpyc==5.0.1 plumbum"

if ! terminal_running; then
  echo "==> Terminal not running — starting it..."
  "$DEPLOY_DIR/start-mt5-terminal.sh"
fi

if [[ "$SKIP_IPC_TEST" != "1" ]]; then
  echo "==> IPC test (terminal must be logged in + Python integration enabled)..."
  if ! exec_abc "
    wine64 '$PY64' -c \"
import MetaTrader5 as mt5
import sys
ok = mt5.initialize(path='$TERMINAL_WIN', timeout=120000)
print('initialize:', ok, 'error:', mt5.last_error())
sys.exit(0 if ok else 1)
\"
  "; then
    if [[ "$QUIET" != "1" ]]; then
      cat >&2 <<'EOF'

IPC failed. One-time GUI steps (SSH tunnel → http://localhost:3300):
  1. Open MT5, File → Login to Trade Account (same credentials as .env)
  2. Tools → Options → Community → enable "Python integration"
  3. Tools → Options → Expert Advisors → enable "Allow algorithmic trading"
  4. File → Exit, then: ./deploy/start-mt5-terminal.sh
  5. Log in again, wait for green connection bars
  6. Re-run: ./deploy/setup-mt5-bridge.sh

To start the bridge without the IPC gate (not recommended):
  SKIP_IPC_TEST=1 ./deploy/setup-mt5-bridge.sh
EOF
    fi
    exit 1
  fi
fi

# chown the log: this exec runs as root, but the bridge appends as abc — on a
# fresh volume a root-owned log makes the detached bridge die on redirect.
docker exec "$CONTAINER" sh -c 'rm -rf /config/mt5linux; : > /config/mt5srv.log; chown abc:abc /config/mt5srv.log'

echo "==> Starting RPyC bridge on 0.0.0.0:8001..."
exec_abc_d "
  $LINUX_VENV/bin/python -m mt5linux \
    '$PY64' \
    --host 0.0.0.0 -p 8001 -s /config/mt5linux -w wine64 \
    >> /config/mt5srv.log 2>&1
"

sleep 5
docker exec "$CONTAINER" tail -8 /config/mt5srv.log || true

if bridge_listening; then
  echo "==> Bridge listening on :8001"
else
  echo "==> Bridge may not be listening — check: docker exec $CONTAINER cat /config/mt5srv.log" >&2
  exit 1
fi
