from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.risk.risk_manager import (
    AccountState,
    Approved,
    LastTradeInfo,
    OpenTrade,
    Rejected,
    SymbolInfo,
    effective_risk_per_trade,
    evaluate,
)
from src.strategy.common import Signal

PARAMS = SimpleNamespace(
    risk_per_trade=0.01,
    max_open_trades=2,
    max_per_symbol=1,
    daily_loss_limit=0.04,
    max_drawdown_kill=0.12,
    cooldown_after_loss_min=15,
    session_utc=["07:00", "16:00"],
    max_spread_pips={"EURUSD": 1.5, "GBPUSD": 2.0},
    kill_switch_enabled=True,
)

NOW = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)  # Tuesday, well inside session


def make_signal(symbol: str = "EURUSD", side: str = "LONG", sl_pips: float = 20.0) -> Signal:
    entry = 1.1050
    sl = entry - sl_pips * 0.0001 if side == "LONG" else entry + sl_pips * 0.0001
    tp = entry + 2 * (entry - sl) if side == "LONG" else entry - 2 * (sl - entry)
    return Signal(symbol=symbol, side=side, entry=entry, sl=sl, tp=tp, sl_pips=sl_pips, context=None)


def make_account(**overrides) -> AccountState:
    base = dict(
        equity=4000.0,
        balance=4000.0,
        day_start_equity=4000.0,
        hwm=4000.0,
        kill_switch_triggered=False,
        now_utc=NOW,
        spread_pips=1.0,
        symbol_info=SymbolInfo(pip_value_per_lot=100.0, volume_step=0.01, volume_min=0.01),
        open_trades=[],
        last_trade_by_symbol={},
        last_entry_time_by_symbol={},
    )
    base.update(overrides)
    return AccountState(**base)


def test_baseline_signal_is_approved():
    verdict = evaluate(make_signal(), make_account(), PARAMS)
    assert isinstance(verdict, Approved)
    assert verdict.lots == pytest.approx(0.02)
    assert verdict.risk_amount == pytest.approx(40.0)


def test_kill_switch_trips_on_drawdown_from_hwm():
    account = make_account(hwm=5000.0, equity=5000.0 * (1 - 0.12))  # exactly at threshold
    verdict = evaluate(make_signal(), account, PARAMS)
    assert isinstance(verdict, Rejected)
    assert verdict.reason == "kill_switch"


def test_kill_switch_latch_rejects_even_if_equity_recovered():
    account = make_account(equity=4000.0, hwm=4000.0, kill_switch_triggered=True)
    verdict = evaluate(make_signal(), account, PARAMS)
    assert isinstance(verdict, Rejected)
    assert verdict.reason == "kill_switch"


def test_kill_switch_disabled_allows_trade_through_drawdown():
    params = deepcopy(PARAMS)
    params.kill_switch_enabled = False
    account = make_account(hwm=5000.0, equity=5000.0 * (1 - 0.12), kill_switch_triggered=True)
    verdict = evaluate(make_signal(), account, params)
    assert isinstance(verdict, Approved)


def test_daily_cap_breach_rejects():
    account = make_account(day_start_equity=4000.0, equity=4000.0 * (1 - 0.04))
    verdict = evaluate(make_signal(), account, PARAMS)
    assert isinstance(verdict, Rejected)
    assert verdict.reason == "daily_cap"


def test_daily_cap_not_breached_just_above_threshold():
    account = make_account(day_start_equity=4000.0, equity=4000.0 * (1 - 0.04) + 0.01)
    verdict = evaluate(make_signal(), account, PARAMS)
    assert isinstance(verdict, Approved)


def test_session_rejects_outside_window():
    outside = NOW.replace(hour=18)
    verdict = evaluate(make_signal(), make_account(now_utc=outside), PARAMS)
    assert isinstance(verdict, Rejected)
    assert verdict.reason == "session"


def test_session_boundaries_are_inclusive():
    at_open = NOW.replace(hour=7, minute=0)
    at_close = NOW.replace(hour=16, minute=0)
    assert isinstance(evaluate(make_signal(), make_account(now_utc=at_open), PARAMS), Approved)
    assert isinstance(evaluate(make_signal(), make_account(now_utc=at_close), PARAMS), Approved)


def test_spread_too_wide_rejects():
    account = make_account(spread_pips=1.6)  # max for EURUSD is 1.5
    verdict = evaluate(make_signal(), account, PARAMS)
    assert isinstance(verdict, Rejected)
    assert verdict.reason == "spread"


def test_spread_at_exact_max_is_allowed():
    account = make_account(spread_pips=1.5)
    verdict = evaluate(make_signal(), account, PARAMS)
    assert isinstance(verdict, Approved)


def test_max_open_trades_rejects_at_global_cap():
    account = make_account(
        open_trades=[OpenTrade(symbol="EURUSD", side="LONG"), OpenTrade(symbol="GBPUSD", side="SHORT")]
    )
    # new signal on a third, uncorrelated symbol would still be blocked by the global cap
    verdict = evaluate(make_signal(symbol="EURUSD", side="LONG"), account, PARAMS)
    assert isinstance(verdict, Rejected)
    assert verdict.reason == "max_open"


def test_max_per_symbol_rejects_even_under_global_cap():
    account = make_account(open_trades=[OpenTrade(symbol="EURUSD", side="LONG")])
    verdict = evaluate(make_signal(symbol="EURUSD", side="SHORT"), account, PARAMS)
    assert isinstance(verdict, Rejected)
    assert verdict.reason == "max_open"


def test_correlation_rejects_same_direction_usd_bet():
    # existing long EURUSD (long EUR / short USD) + new long GBPUSD (long GBP / short USD)
    # both bet short-USD -> correlated
    account = make_account(open_trades=[OpenTrade(symbol="EURUSD", side="LONG")])
    verdict = evaluate(make_signal(symbol="GBPUSD", side="LONG"), account, PARAMS)
    assert isinstance(verdict, Rejected)
    assert verdict.reason == "correlation"


def test_correlation_allows_opposite_usd_bet_as_a_hedge():
    # existing long EURUSD (short USD) + new short GBPUSD (long USD) -> not correlated
    account = make_account(open_trades=[OpenTrade(symbol="EURUSD", side="LONG")])
    verdict = evaluate(make_signal(symbol="GBPUSD", side="SHORT"), account, PARAMS)
    assert isinstance(verdict, Approved)


def test_correlation_cap_allows_second_bet_then_blocks_third():
    # max_same_currency_bets=2: one open short-USD trade -> second allowed,
    # two open short-USD trades -> third rejected.
    params = deepcopy(PARAMS)
    params.max_same_currency_bets = 2
    params.max_open_trades = 4
    params.max_spread_pips = {"EURUSD": 1.5, "GBPUSD": 2.0, "AUDUSD": 2.0}

    one_open = make_account(open_trades=[OpenTrade(symbol="EURUSD", side="LONG")])
    assert isinstance(evaluate(make_signal(symbol="GBPUSD", side="LONG"), one_open, params), Approved)

    two_open = make_account(
        open_trades=[OpenTrade(symbol="EURUSD", side="LONG"), OpenTrade(symbol="GBPUSD", side="LONG")]
    )
    verdict = evaluate(make_signal(symbol="AUDUSD", side="LONG"), two_open, params)
    assert isinstance(verdict, Rejected)
    assert verdict.reason == "correlation"


def test_cooldown_rejects_within_window_after_a_loss():
    account = make_account(
        last_trade_by_symbol={
            "EURUSD": LastTradeInfo(closed_at=NOW - timedelta(minutes=10), was_loss=True)
        }
    )
    verdict = evaluate(make_signal(), account, PARAMS)
    assert isinstance(verdict, Rejected)
    assert verdict.reason == "cooldown"


def test_cooldown_allows_after_window_elapses_post_loss():
    account = make_account(
        last_trade_by_symbol={
            "EURUSD": LastTradeInfo(closed_at=NOW - timedelta(minutes=16), was_loss=True)
        }
    )
    verdict = evaluate(make_signal(), account, PARAMS)
    assert isinstance(verdict, Approved)


def test_cooldown_does_not_apply_after_a_win():
    account = make_account(
        last_trade_by_symbol={
            "EURUSD": LastTradeInfo(closed_at=NOW - timedelta(minutes=1), was_loss=False)
        },
        last_entry_time_by_symbol={"EURUSD": NOW - timedelta(minutes=20)},
    )
    verdict = evaluate(make_signal(), account, PARAMS)
    assert isinstance(verdict, Approved)


def test_cooldown_rejects_re_entry_within_one_m15_candle():
    account = make_account(last_entry_time_by_symbol={"EURUSD": NOW - timedelta(minutes=5)})
    verdict = evaluate(make_signal(), account, PARAMS)
    assert isinstance(verdict, Rejected)
    assert verdict.reason == "cooldown"


def test_lot_sizing_known_inputs_produce_exact_lots():
    # risk_amount = 4000 * 0.01 = 40; pip_value_per_lot = 100 -> 40 / (20*100) = 0.02 lots
    verdict = evaluate(make_signal(sl_pips=20.0), make_account(), PARAMS)
    assert isinstance(verdict, Approved)
    assert verdict.lots == pytest.approx(0.02)


def test_lot_sizing_floors_to_volume_step_never_rounds_up():
    # 40 / (30 * 100) = 0.01333... -> floors to 0.01, not 0.02
    verdict = evaluate(make_signal(sl_pips=30.0), make_account(), PARAMS)
    assert isinstance(verdict, Approved)
    assert verdict.lots == pytest.approx(0.01)


def test_lot_sizing_rejects_when_account_too_small_for_stop():
    account = make_account(balance=50.0)  # risk_amount = 0.5, far below volume_min at any lot
    verdict = evaluate(make_signal(sl_pips=20.0), account, PARAMS)
    assert isinstance(verdict, Rejected)
    assert verdict.reason == "lot_size_too_small"


def test_invalid_stop_on_wrong_side_of_entry_rejects():
    bad_signal = Signal(symbol="EURUSD", side="LONG", entry=1.1050, sl=1.1050, tp=1.1090, sl_pips=0.0, context=None)
    verdict = evaluate(bad_signal, make_account(), PARAMS)
    assert isinstance(verdict, Rejected)
    assert verdict.reason == "invalid_stop"


def test_check_order_kill_switch_beats_daily_cap():
    # both breached simultaneously -> kill_switch must win since it's checked first
    account = make_account(
        hwm=4000.0,
        equity=4000.0 * (1 - 0.12) - 1,
        day_start_equity=4000.0,
    )
    verdict = evaluate(make_signal(), account, PARAMS)
    assert isinstance(verdict, Rejected)
    assert verdict.reason == "kill_switch"


def test_crypto_not_blocked_by_forex_usd_stack():
    """BTC entries must not be rejected because forex pairs already short USD."""
    params = deepcopy(PARAMS)
    params.session_utc = ["00:00", "23:59"]
    params.max_open_trades = 5
    params.max_same_currency_bets = 4
    params.max_spread_pips = {"EURUSD": 1.5, "GBPUSD": 2.0, "AUDUSD": 2.0, "NZDUSD": 2.0, "BTCUSDm": 80.0}
    account = make_account(
        open_trades=[
            OpenTrade(symbol="EURUSD", side="LONG"),
            OpenTrade(symbol="GBPUSD", side="LONG"),
            OpenTrade(symbol="AUDUSD", side="LONG"),
            OpenTrade(symbol="NZDUSD", side="LONG"),
        ]
    )
    btc_short = Signal(
        symbol="BTCUSDm",
        side="SHORT",
        entry=97000.0,
        sl=97500.0,
        tp=93000.0,
        sl_pips=500.0,
        context=None,
    )
    verdict = evaluate(btc_short, account, params)
    if isinstance(verdict, Rejected):
        assert verdict.reason != "correlation"
    else:
        assert isinstance(verdict, Approved)


def test_growth_risk_tiers_scale_with_balance():
    params = SimpleNamespace(
        growth_risk_tiers=[
            {"until_equity": 8000, "risk_per_trade": 0.01},
            {"until_equity": 20000, "risk_per_trade": 0.015},
            {"until_equity": 100000000, "risk_per_trade": 0.025},
        ],
        risk_per_trade=0.025,
    )
    assert effective_risk_per_trade(4000, params) == pytest.approx(0.01)
    assert effective_risk_per_trade(15000, params) == pytest.approx(0.015)
    assert effective_risk_per_trade(50000, params) == pytest.approx(0.025)


def test_allow_min_lot_when_forced_risk_within_cap():
    params = deepcopy(PARAMS)
    params.growth_risk_tiers = []
    params.allow_min_lot = True
    params.max_risk_when_min_lot = 0.05
    params.risk_per_trade = 0.01
    params.kill_switch_enabled = False
    params.session_utc = ["00:00", "23:59"]
    # balance 4000 cents, 1% target = 40 cents; wide stop -> raw lots 0.008 -> min 0.01
    account = make_account(balance=4000.0)
    verdict = evaluate(make_signal(sl_pips=50.0), account, params)
    assert isinstance(verdict, Approved)
    assert verdict.lots == pytest.approx(0.01)


def test_insolvent_account_rejects():
    account = make_account(balance=0.0, equity=0.0)
    verdict = evaluate(make_signal(), account, PARAMS)
    assert isinstance(verdict, Rejected)
    assert verdict.reason == "insolvent"
