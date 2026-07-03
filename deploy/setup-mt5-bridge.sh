#!/usr/bin/env bash
# Start the mt5linux RPyC bridge inside the gmag11/metatrader5_vnc container.
# Prerequisites:
#   - MT5 terminal logged in via VNC (http://localhost:3300)
#   - Tools → Options → Community → "Python integration" enabled
#   - 64-bit Windows Python installed in Wine (this script installs it if missing)
set -euo pipefail

CONTAINER="${MT5_CONTAINER:-mt5}"
PY64_WIN='C:\users\abc\AppData\Local\Programs\Python\Python39\python.exe'
PY64="/config/.wine/drive_c/users/abc/AppData/Local/Programs/Python/Python39/python.exe"
PY64_URL='https://www.python.org/ftp/python/3.9.13/python-3.9.13-amd64.exe'
LINUX_VENV='/config/mt5venv'

echo "==> Stopping any existing bridge..."
docker exec "$CONTAINER" sh -c 'pkill -f server.py 2>/dev/null; pkill -f mt5linux 2>/dev/null; sleep 2' || true

echo "==> Ensuring Linux-side launcher venv..."
docker exec -u abc "$CONTAINER" bash -lc "
  if [ ! -x $LINUX_VENV/bin/python ]; then
    python3 -m venv $LINUX_VENV
    $LINUX_VENV/bin/pip install --no-deps mt5linux==0.1.9 rpyc==5.0.1 plumbum numpy
  fi
"

if ! docker exec "$CONTAINER" test -f "$PY64"; then
  echo "==> Installing 64-bit Windows Python in Wine (required for terminal64.exe IPC)..."
  docker exec -u abc -e HOME=/config -e WINEPREFIX=/config/.wine "$CONTAINER" bash -lc "
    cd /tmp
    curl -LO $PY64_URL
    wine64 python-3.9.13-amd64.exe /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1
  "
fi

echo "==> Installing MetaTrader5 + rpyc on 64-bit Wine Python..."
docker exec -u abc -e HOME=/config -e WINEPREFIX=/config/.wine "$CONTAINER" bash -lc "
  wine64 '$PY64' -m pip install -q MetaTrader5 rpyc==5.0.1 plumbum
"

echo "==> Quick IPC test (MT5 must be running and logged in)..."
docker exec -u abc -e HOME=/config -e WINEPREFIX=/config/.wine -e DISPLAY=:1 "$CONTAINER" bash -lc "
  wine64 '$PY64' -c \"
import MetaTrader5 as mt5
ok = mt5.initialize(path='C:/Program Files/MetaTrader 5/terminal64.exe', timeout=120000)
print('initialize:', ok, 'error:', mt5.last_error())
import sys; sys.exit(0 if ok else 1)
\"
"

docker exec "$CONTAINER" sh -c 'rm -rf /config/mt5linux; : > /config/mt5srv.log'

echo "==> Starting bridge on 0.0.0.0:8001..."
docker exec -d -u abc -e HOME=/config -e WINEPREFIX=/config/.wine -e DISPLAY=:1 "$CONTAINER" bash -lc "
  $LINUX_VENV/bin/python -m mt5linux \
    '$PY64' \
    --host 0.0.0.0 -p 8001 -s /config/mt5linux -w wine64 \
    >> /config/mt5srv.log 2>&1
"

sleep 5
docker exec "$CONTAINER" tail -5 /config/mt5srv.log
echo "==> Done. Test from host: cd /opt/forex-bot && .venv/bin/python -c \"from src.config import load_secrets; from src.data.mt5_client import MT5Client; s=load_secrets(); c=MT5Client(s.mt5_host,s.mt5_port,s.mt5_login,s.mt5_password,s.mt5_server); c.connect(); print(c.account_info()); c.shutdown()\""
