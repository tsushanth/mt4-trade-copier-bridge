import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bridge import Bridge, CopyState, size_shares
from event_source import EventFileTail
from mt4_demo_emitter import DemoEmitter


class FakeOrder:
    _n = 0

    def __init__(self, qty, filled=True):
        FakeOrder._n += 1
        self.id = f"fake-order-{FakeOrder._n}"
        self.status = "accepted"
        self.filled_qty = str(qty) if filled else "0"


class FakeRouter:
    """Records submitted orders instead of hitting Alpaca. `fill_entries`
    controls whether entry orders report as filled (default) or resting.
    """
    def __init__(self, fill_entries=True):
        self.submitted = []
        self.cancelled = []
        self.fill_entries = fill_entries
        self._orders = {}
        self.prices = {"FXE": 100.0, "FXA": 65.0, "UUP": 28.0, "UDN": 18.0,
                       "FXF": 110.0, "FXY": 60.0}

    def latest_price(self, symbol):
        return self.prices.get(symbol, 50.0)

    def submit_copy_order(self, symbol, side, qty, ref_price, slippage_bps=25.0):
        self.submitted.append((symbol, side, qty))
        o = FakeOrder(qty, filled=self.fill_entries)
        self._orders[o.id] = o
        return o

    def get_order(self, order_id):
        return self._orders[order_id]

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)


def _log():
    lg = logging.getLogger("test")
    lg.addHandler(logging.NullHandler())
    return lg


def test_size_shares_minimum_one():
    assert size_shares(0.01, 10.0) == 1   # rounds to 0 -> clamped to 1
    assert size_shares(0.10, 10.0) == 1
    assert size_shares(1.0, 10.0) == 10


def test_open_then_close_roundtrip_live(tmp_path):
    f = str(tmp_path / "events.csv")
    em = DemoEmitter(f)
    em.open_trade(555, "EURUSD", "BUY", 1.0, 1.0821)

    router = FakeRouter()
    state = CopyState(str(tmp_path / "state.json"))
    bridge = Bridge(router, state, _log(), dry_run=False, shares_per_lot=10.0)

    tail = EventFileTail(f)
    for ev in tail.poll():
        bridge.handle(ev)

    assert router.submitted == [("FXE", "BUY", 10)]
    assert "555" in state.open_copies

    # Now close it -> should flatten with the opposite side, same qty.
    em.close_trade(555, "EURUSD", "BUY", 1.0, 1.0821, 1.0850)
    for ev in tail.poll():
        bridge.handle(ev)

    assert router.submitted[-1] == ("FXE", "SELL", 10)
    assert "555" not in state.open_copies   # copy removed after flatten


def test_close_cancels_entry_when_never_filled(tmp_path):
    # Market-closed case: the entry rests unfilled. A CLOSE must CANCEL
    # the entry, never fire an opposing (wash-trade) order.
    f = str(tmp_path / "events.csv")
    em = DemoEmitter(f)
    em.open_trade(42, "EURUSD", "BUY", 1.0, 1.0821)

    router = FakeRouter(fill_entries=False)   # entry never fills
    state = CopyState(str(tmp_path / "state.json"))
    bridge = Bridge(router, state, _log(), dry_run=False, shares_per_lot=10.0)

    tail = EventFileTail(f)
    for ev in tail.poll():
        bridge.handle(ev)
    assert router.submitted == [("FXE", "BUY", 10)]   # entry submitted

    em.close_trade(42, "EURUSD", "BUY", 1.0, 1.0821, 1.0850)
    for ev in tail.poll():
        bridge.handle(ev)

    # No second (opposing) order was submitted; the entry was cancelled.
    assert router.submitted == [("FXE", "BUY", 10)]
    assert len(router.cancelled) == 1
    assert "42" not in state.open_copies


def test_no_proxy_cross_is_logged_not_traded(tmp_path):
    f = str(tmp_path / "events.csv")
    em = DemoEmitter(f)
    em.open_trade(700, "EURGBP", "BUY", 0.5, 0.851)

    router = FakeRouter()
    bridge = Bridge(router, CopyState(str(tmp_path / "s.json")), _log(),
                    dry_run=False, shares_per_lot=10.0)
    for ev in EventFileTail(f).poll():
        bridge.handle(ev)

    assert router.submitted == []            # nothing traded
    assert bridge.stats["none"] == 1


def test_dedupe_prevents_double_open(tmp_path):
    f = str(tmp_path / "events.csv")
    em = DemoEmitter(f)
    em.open_trade(900, "EURUSD", "BUY", 1.0, 1.0821)

    router = FakeRouter()
    state = CopyState(str(tmp_path / "s.json"))
    bridge = Bridge(router, state, _log(), dry_run=False, shares_per_lot=10.0)

    ev = EventFileTail(f).poll()[0]
    bridge.handle(ev)
    bridge.handle(ev)   # same event again
    assert len(router.submitted) == 1


def test_dry_run_records_state_but_places_nothing(tmp_path):
    f = str(tmp_path / "events.csv")
    em = DemoEmitter(f)
    em.open_trade(111, "AUDUSD", "SELL", 1.0, 0.663)

    router = FakeRouter()
    state = CopyState(str(tmp_path / "s.json"))
    bridge = Bridge(router, state, _log(), dry_run=True, shares_per_lot=10.0)
    for ev in EventFileTail(f).poll():
        bridge.handle(ev)

    assert router.submitted == []          # dry-run: nothing submitted
    assert "111" in state.open_copies       # but intent recorded
    assert bridge.stats["dry_run"] == 1
    assert bridge.stats["direct"] == 1
