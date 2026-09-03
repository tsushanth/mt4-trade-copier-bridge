import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from symbol_map import map_symbol, Resolution


def test_eurusd_long_direct_buy_fxe():
    m = map_symbol("EURUSD", "BUY")
    assert m.resolution == Resolution.DIRECT
    assert (m.etf, m.side) == ("FXE", "BUY")
    assert not m.degraded


def test_audusd_short_direct_because_fxa_shortable():
    m = map_symbol("AUDUSD", "SELL")
    assert m.resolution == Resolution.DIRECT
    assert (m.etf, m.side) == ("FXA", "SELL")


def test_gbpusd_short_degrades_because_fxb_not_shortable():
    # SELL GBPUSD would need SHORT FXB, which Alpaca won't allow ->
    # fall back to the shortable dollar basket. Short GBP = long USD.
    m = map_symbol("GBPUSD", "SELL")
    assert m.resolution == Resolution.DEGRADED
    assert m.degraded
    assert (m.etf, m.side) == ("UUP", "BUY")


def test_usdjpy_long_degrades_because_fxy_not_shortable():
    # BUY USDJPY = SHORT the yen = SHORT FXY (not shortable) -> UUP.
    m = map_symbol("USDJPY", "BUY")
    assert m.resolution == Resolution.DEGRADED
    assert (m.etf, m.side) == ("UUP", "BUY")


def test_usdchf_short_direct_buy_fxf():
    # SELL USDCHF = long CHF = BUY FXF (buyable, no short needed).
    m = map_symbol("USDCHF", "SELL")
    assert m.resolution == Resolution.DIRECT
    assert (m.etf, m.side) == ("FXF", "BUY")


def test_usdcad_long_degrades():
    # BUY USDCAD = short CAD = SHORT FXC (not shortable) -> long USD -> UUP.
    m = map_symbol("USDCAD", "BUY")
    assert m.resolution == Resolution.DEGRADED
    assert (m.etf, m.side) == ("UUP", "BUY")


def test_non_usd_cross_has_no_proxy():
    for sym in ("EURGBP", "GBPJPY", "EURJPY"):
        m = map_symbol(sym, "BUY")
        assert m.resolution == Resolution.NONE
        assert m.etf is None and m.side is None


def test_unknown_major_uses_usd_leg_degraded():
    # NZDUSD: no NZD ETF in our table, but there IS a USD leg -> degraded
    # dollar-basket, not NONE. Long NZDUSD = short USD -> UDN.
    m = map_symbol("NZDUSD", "BUY")
    assert m.resolution == Resolution.DEGRADED
    assert (m.etf, m.side) == ("UDN", "BUY")


def test_broker_suffix_is_tolerated():
    for sym in ("EURUSD.m", "EURUSDpro", "EURUSD-5", "eurusd"):
        m = map_symbol(sym, "BUY")
        assert (m.etf, m.side) == ("FXE", "BUY"), sym


def test_garbage_symbol_is_none():
    m = map_symbol("XYZ", "BUY")
    assert m.resolution == Resolution.NONE


def test_invalid_side_is_none():
    m = map_symbol("EURUSD", "HOLD")
    assert m.resolution == Resolution.NONE
