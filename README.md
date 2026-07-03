# forex-bot

Personal-use, rule-based (non-AI) forex trading bot. Trend-following
pullback strategy on EURUSD/GBPUSD, H1 trend filter + M15 entries. Trades
via MetaTrader 5 through the [`mt5linux`](https://pypi.org/project/mt5linux/)
RPyC bridge, against an Exness Standard Cent demo account.

Architecture: **Market Data → Strategy Engine → Risk Manager → Execution**.
Strategy ([src/strategy/trend_pullback.py](src/strategy/trend_pullback.py))
and Risk ([src/risk/risk_manager.py](src/risk/risk_manager.py)) are pure
functions of data with zero MT5 imports, so the backtester and the live bot
run the *exact same code* — they only differ in data source and executor.

## Prerequisites

- An MT5 terminal running in a Docker container on your Linux VPS, with the
  `mt5linux` RPyC server exposed on `localhost:8001`.
- An Exness Standard Cent demo (or live) account. Account currency is
  cents — e.g. a $40 account reports `balance=4000`. The code always reads
  `account_info()`; nothing is hardcoded in dollars.
- Python 3.11+ (developed against 3.12).

## Setup

```bash
git clone <this repo> forex-bot
cd forex-bot
python3.12 -m venv .venv
source .venv/bin/activate        # .venv\Scripts\activate on Windows

pip install -r requirements.txt
```

`mt5linux`'s published PyPI metadata pins an unrelated, ancient
dev-environment freeze (`cffi==1.15.0`, `cryptography`, `twine`, `keyring`,
`SecretStorage`, ...) that has nothing to do with its runtime behavior — the
client class only actually imports `rpyc` and `numpy`. Installing it
normally on a modern Python requires a C compiler for that old `cffi`.
`requirements.txt` documents this; if `pip install -r requirements.txt`
tries to build `cffi` from source, install `mt5linux`/`rpyc` separately with
`--no-deps`:

```bash
pip install --no-deps mt5linux==0.1.9 rpyc==6.0.1
```

Then configure secrets:

```bash
cp .env.example .env
# edit .env: MT5_HOST, MT5_PORT, MT5_LOGIN, MT5_PASSWORD, MT5_SERVER,
# TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
```

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
python pull_data.py --days 730 --db forex_bot.db
```

Fetches 2 years of M15+H1 candles for the configured pairs into SQLite and
reports any unexpected gaps (normal Friday-close/Sunday-open weekend gaps
are not flagged).

## Backtest

```bash
python run_backtest.py --db forex_bot.db
# or restrict to a date range, e.g. to check two halves of history separately:
python run_backtest.py --db forex_bot.db --start 2023-01-01 --end 2023-12-31
python run_backtest.py --db forex_bot.db --start 2024-01-01 --end 2024-12-31
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

See [deploy/forex-bot.service](deploy/forex-bot.service). It runs the bot
as an unprivileged user, restarts on failure, and never opens an inbound
port — it only makes outbound connections to the local RPyC bridge
(`127.0.0.1:8001`) and to `api.telegram.org` for alerts.

```bash
sudo useradd -r -s /usr/sbin/nologin forexbot
sudo mkdir -p /opt/forex-bot
sudo cp -r . /opt/forex-bot
sudo chown -R forexbot:forexbot /opt/forex-bot
sudo cp deploy/forex-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now forex-bot
sudo journalctl -u forex-bot -f
```

## What this bot deliberately does not do

No martingale, no averaging down, no re-entry-because-of-drawdown, no order
sent without SL+TP attached in the same request, no lot size computed
outside the risk manager, no runtime config option that disables the kill
switch.
