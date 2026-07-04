# forex-bot

Personal-use, rule-based (non-AI) forex trading bot. Trend-following
pullback strategy on EURUSD/GBPUSD, H1 trend filter + M15 entries. Trades
via MetaTrader 5 through the [`mt5linux`](https://pypi.org/project/mt5linux/)
RPyC bridge, against an Exness Standard Cent demo account.

Architecture: **Market Data → Strategy Engine → Risk Manager → Execution**.
Strategies live under [`src/strategy/`](src/strategy/) and are selected via
`strategy:` in [`config/settings.yaml`](config/settings.yaml). Risk
([src/risk/risk_manager.py](src/risk/risk_manager.py)) is a pure function of
data with zero MT5 imports, so the backtester and the live bot run the *exact
same* strategy/risk code — they only differ in data source and executor.

### Strategies

- `h4_trend` (default) — Turtle-adapted H4 Donchian trend-following: EMA(50)
  regime filter, entry on a 20-bar H4 channel break at H4 closes only,
  2×ATR initial stop, 3×ATR chandelier trail (broker SL is ratcheted up,
  never loosened), far 8R cap. Holds for days. Rationale: on M15, spread +
  slippage consumed ~19% of R per trade and both M15 strategies tested at
  break-even gross / strongly negative net; H4 stops cut the cost drag to
  ~4-5% of R, and trend-following evidence lives at daily-ish horizons.
- `breakout_trend` — M15 Donchian breakout, H1 filter. Tested on 2y
  EURUSD/GBPUSD: PF 0.73, −75.7% max DD. Kept for comparison runs only.
- `trend_pullback` — v1 M15 pullback. Kept for comparison runs only.

## Prerequisites

- Linux VPS with Docker (compose v2).
- An Exness Standard Cent demo (or live) account. Account currency is
  cents — e.g. a $4000 account reports `balance=400000`. The code always
  reads `account_info()`; nothing is hardcoded in dollars.
- Python 3.11+ (developed against 3.12).

## VPS deploy (reproducible)

Everything for MT5 lives under [`deploy/`](deploy/): compose file, scripts,
and systemd units. Do **not** hand-edit a separate `/opt/mt5` tree.

**Docker footprint:** exactly **one image** (`gmag11/metatrader5_vnc:latest`)
and **one container** (`mt5`). The trading bot is **not** containerized — it
runs in a host venv and connects to `127.0.0.1:8001`.

```bash
git clone <this repo> /opt/forex-bot
cd /opt/forex-bot

# Bot secrets
cp .env.example .env
# edit .env: MT5_HOST=127.0.0.1, MT5_PORT=8001, MT5_LOGIN, MT5_PASSWORD,
# MT5_SERVER, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# VNC password for the MT5 desktop (never commit)
cp deploy/mt5.env.example deploy/mt5.env
# edit deploy/mt5.env: MT5_VNC_PASSWORD=...

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# mt5linux pins broken ancient deps — install client-side only with --no-deps.
# Use rpyc 5.0.1 to match the Wine-side RPyC server (5.x wire protocol).
.venv/bin/pip install --no-deps mt5linux==0.1.9 rpyc==5.0.1
mkdir -p data logs state
chmod +x deploy/*.sh

# Interactive bring-up (retries IPC for a few minutes)
./deploy/up.sh
```

**One-time GUI steps** (cannot be fully automated — Exness login + Python IPC
flag). From your PC:

```bash
ssh -L 3300:127.0.0.1:3300 user@your-vps
# open http://localhost:3300  (VNC user/password from deploy/mt5.env)
```

In MT5:

1. File → Login to Trade Account (same credentials as `.env`)
2. Tools → Options → Community → enable **Python integration**
3. Tools → Options → Expert Advisors → enable **Allow algorithmic trading**
4. File → Exit, then on the VPS: `./deploy/start-mt5-terminal.sh`
5. Log in again, wait for green connection bars
6. Re-run: `./deploy/setup-mt5-bridge.sh` (or `./deploy/up.sh`)

After that, login is stored in the Docker volume `forex-bot-mt5-config`.
Production path (supervisor keeps terminal + bridge alive across reboots):

```bash
sudo ./deploy/install-systemd.sh
./deploy/status.sh
# when Bot → MT5 is OK:
sudo systemctl enable --now forex-bot
```

| Script | Purpose |
|--------|---------|
| [`deploy/up.sh`](deploy/up.sh) | one-shot compose + terminal + bridge |
| [`deploy/supervise-mt5.sh`](deploy/supervise-mt5.sh) | long-running keep-alive (systemd) |
| [`deploy/down.sh`](deploy/down.sh) | stop supervisor + container (keep volume) |
| [`deploy/start-mt5-terminal.sh`](deploy/start-mt5-terminal.sh) | start `terminal64.exe` via `wine64` |
| [`deploy/setup-mt5-bridge.sh`](deploy/setup-mt5-bridge.sh) | 64-bit Python + RPyC on `:8001` |
| [`deploy/status.sh`](deploy/status.sh) | health check |
| [`deploy/install-systemd.sh`](deploy/install-systemd.sh) | install units + permissions |
| [`deploy/vps-teardown.sh`](deploy/vps-teardown.sh) | remove stack (`WIPE_MT5_VOLUME=1` / `WIPE_BOT=1`) |

The stock `gmag11/metatrader5_vnc` image auto-starts a **broken** mt5linux
(`Unknown switch -w`) and ships **32-bit** Python. Always use
`deploy/setup-mt5-bridge.sh` / `supervise-mt5.sh` — never rely on the image's
step `[7/7]`.

Strategy/risk parameters live in [config/settings.yaml](config/settings.yaml)
(not `.env`) — loaded via pydantic-settings. There is intentionally no
runtime-editable flag to disable the kill switch; see below.

## Run the tests

```bash
pytest
```

Risk manager tests (every rejection path + lot-sizing edge cases) are the
priority — they should pass before anything touches the execution layer.

## Pull historical data

```bash
python pull_data.py --days 730 --db data/forex_bot.db
```

Fetches 2 years of M15+H1 candles for the configured pairs into SQLite and
reports any unexpected gaps (normal Friday-close/Sunday-open weekend gaps
are not flagged).

## Backtest

```bash
python run_backtest.py --db data/forex_bot.db
# or restrict to a date range, e.g. to check two halves of history separately:
python run_backtest.py --db data/forex_bot.db --start 2023-01-01 --end 2023-12-31
python run_backtest.py --db data/forex_bot.db --start 2024-01-01 --end 2024-12-31
```

Prints trade count, win rate, avg win/loss, profit factor, max drawdown,
longest loss streak, monthly returns, MAE/MFE distributions (winners vs
losers), and a breakdown of risk-manager rejections by reason.

The backtester is event-driven over closed candles only: fills are
simulated at candle close + configured spread + 0.5 pip slippage, and if a
candle touches both SL and TP, SL is assumed hit first (pessimistic).

## Paper trade, then go live

```bash
python run_live.py --paper   # simulates fills against the live/demo feed, no real orders
python run_live.py           # sends real orders
```

`run_live.py`:
- Connects via `mt5linux`, verifies the terminal is connected and the
  account matches `MT5_LOGIN` before doing anything else.
- Reconciles local state (`state/state.json`) against MT5's own open
  positions on startup — it never assumes local state is correct after a
  restart or crash.
- Sends a Telegram alert and halts new entries if the terminal disconnects,
  and never trades blind.
- Sends Telegram alerts for: trade opened, trade closed (with R result),
  daily loss cap hit, kill switch triggered, terminal disconnected, and an
  hourly heartbeat.

### Telegram setup

1. Create a bot via [@BotFather](https://t.me/BotFather), copy the token
   into `TELEGRAM_BOT_TOKEN`.
2. Message your bot once, then hit
   `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your numeric
   chat id; put it in `TELEGRAM_CHAT_ID`.
3. Alerts are silently skipped (logged at debug level only) if either value
   is blank — useful for local testing without spamming a chat.

## Logs

JSONL, one file per day per log type, 90-day retention, in `logs/`:

- `decisions-YYYY-MM-DD.jsonl` — one record per pair per closed M15 candle,
  always written even when nothing happens (regime, indicators, pullback
  state, signal, risk verdict, reject reason).
- `trades-YYYY-MM-DD.jsonl` — entry/exit events, including MAE/MFE.
- `equity-YYYY-MM-DD.jsonl` — hourly + on every trade close.

These are designed to be pasted to an AI for trade-by-trade diagnosis —
completeness over brevity. Every rejection logs its reason.

## Kill switch

If equity drops to `HWM × (1 − max_drawdown_kill)`, trading is disabled and
`state/state.json`'s `kill_switch_triggered` is latched to `true`. This is a
one-way latch: it survives restarts, and there is **no config option or
command to clear it automatically** — by design. To resume trading you must
manually inspect what happened and edit `kill_switch_triggered` back to
`false` in `state/state.json` yourself.

## systemd deployment

Two units:

- [`deploy/mt5-bridge.service`](deploy/mt5-bridge.service) — `Type=simple`,
  runs [`deploy/supervise-mt5.sh`](deploy/supervise-mt5.sh) forever: brings
  the container up, starts `terminal64` / RPyC if they die, retries IPC until
  login works
- [`deploy/forex-bot.service`](deploy/forex-bot.service) — paper/live bot
  (`Wants=mt5-bridge`, does not hard-fail boot if IPC is still warming up)

The bot never opens an inbound port — only outbound to `127.0.0.1:8001` and
`api.telegram.org`.

```bash
sudo ./deploy/install-systemd.sh
sudo journalctl -u mt5-bridge -f
# Drop --paper in forex-bot.service once paper mode is validated.
sudo systemctl enable --now forex-bot
```

If IPC is not ready yet, `mt5-bridge` keeps retrying (see journal). Fix via
VNC once; after credentials are saved in the volume, reboots recover alone.

## What this bot deliberately does not do

No martingale, no averaging down, no re-entry-because-of-drawdown, no order
sent without SL+TP attached in the same request, no lot size computed
outside the risk manager, no runtime config option that disables the kill
switch.
