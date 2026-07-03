import pytest

from src.execution.paper import PaperBroker
from src.risk.risk_manager import Approved
from src.strategy.trend_pullback import Signal


class FakeClient:
    def __init__(self, bid: float, ask: float):
        self._bid = bid
        self._ask = ask

    def symbol_info_tick(self, symbol: str) -> dict:
        return {"bid": self._bid, "ask": self._ask}


def make_signal(side: str) -> Signal:
    entry = 1.1050
    sl = entry - 0.0020 if side == "LONG" else entry + 0.0020
    tp = entry + 0.0040 if side == "LONG" else entry - 0.0040
    return Signal(symbol="EURUSD", side=side, entry=entry, sl=sl, tp=tp, sl_pips=20.0, context=None)


def test_paper_open_long_pays_ask_plus_slippage():
    client = FakeClient(bid=1.1048, ask=1.1050)
    broker = PaperBroker(client)
    signal = make_signal("LONG")
    approved = Approved(lots=0.02, entry=signal.entry, sl=signal.sl, tp=signal.tp, risk_amount=40.0)

    result = broker.open_position(signal, approved)

    assert result.success is True
    assert result.price == pytest.approx(1.1050 + 0.5 * 0.0001)


def test_paper_open_short_receives_bid_minus_slippage():
    client = FakeClient(bid=1.1048, ask=1.1050)
    broker = PaperBroker(client)
    signal = make_signal("SHORT")
    approved = Approved(lots=0.02, entry=signal.entry, sl=signal.sl, tp=signal.tp, risk_amount=40.0)

    result = broker.open_position(signal, approved)

    assert result.price == pytest.approx(1.1048 - 0.5 * 0.0001)


def test_paper_close_long_position_hits_bid_minus_slippage():
    client = FakeClient(bid=1.1090, ask=1.1092)
    broker = PaperBroker(client)

    result = broker.close_position({"symbol": "EURUSD", "side": "LONG"})

    assert result.price == pytest.approx(1.1090 - 0.5 * 0.0001)


def test_paper_order_ids_increment():
    client = FakeClient(bid=1.1048, ask=1.1050)
    broker = PaperBroker(client)
    signal = make_signal("LONG")
    approved = Approved(lots=0.02, entry=signal.entry, sl=signal.sl, tp=signal.tp, risk_amount=40.0)

    r1 = broker.open_position(signal, approved)
    r2 = broker.open_position(signal, approved)

    assert r2.order_id == r1.order_id + 1
