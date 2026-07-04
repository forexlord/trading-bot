#!/usr/bin/env bash
# Print MT5 stack health (container, terminal, bridge, optional bot connect).
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

echo "Container:"
if container_running; then
  docker ps --filter "name=^${CONTAINER}$" --format "  {{.Names}}  {{.Status}}  {{.Ports}}"
else
  echo "  not running"
fi

echo "terminal64.exe:"
if terminal_running; then
  docker exec "$CONTAINER" sh -c 'ps aux | grep -i terminal64 | grep -v grep' | sed 's/^/  /'
else
  echo "  not running"
fi

echo "RPyC :8001:"
if bridge_listening; then
  echo "  listening"
else
  echo "  not listening"
fi

if [[ -f "$REPO_ROOT/.env" && -x "$REPO_ROOT/.venv/bin/python" ]]; then
  echo "Bot → MT5:"
  # subshell cd: "from src.config import ..." only resolves with CWD=repo root
  if (cd "$REPO_ROOT" && .venv/bin/python -c "
from src.config import load_secrets
from src.data.mt5_client import MT5Client
s = load_secrets()
c = MT5Client(s.mt5_host, s.mt5_port, s.mt5_login, s.mt5_password, s.mt5_server)
c.connect()
a = c.account_info()
print(f\"  OK login={a['login']} balance={a['balance']} equity={a['equity']}\")
c.shutdown()
" 2>/dev/null); then
    :
  else
    echo "  connect failed (bridge down, or MT5 not logged in)"
  fi
fi
