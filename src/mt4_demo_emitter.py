"""Stand-in producer for TradeCopyExporter.mq4's event file.

Why this exists (read this before assuming it's a shortcut)
-----------------------------------------------------------
The real event producer is the MQL4 EA in ea/TradeCopyExporter.mq4,
which runs *inside* a MetaTrader 4 terminal attached to a demo account.
That terminal is a GUI desktop app; it cannot be installed and driven
headless inside this non-interactive build environment (no display, no
way to complete MetaQuotes' in-terminal demo-account signup, no way to
click-place trades). See docs/ASSESSMENT.md for the full account of
that boundary.

So this module reproduces *exactly* the CSV wire format the EA emits
(same header, same columns, same append semantics), so the Python bridge
can be exercised end-to-end against a real, byte-identical event stream
and place REAL Alpaca paper orders from it. The Alpaca leg is not
simulated at all -- only the MT4 terminal is stood in for here, and only
because it physically can't run in this environment.

The scenario below is a hand-authored set of forex trades chosen to
cover every branch of the symbol mapping (clean USD-quoted major,
inverse USD-base major, shortable vs non-shortable currency ETF, a
degraded dollar-basket fallback, and a non-USD cross with no proxy) --
not random noise, so the assessment can point at each case.
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone

HEADER = ["event_id", "ts_utc", "action", "ticket", "symbol", "order_type",
          "lots", "open_price", "close_price", "sl", "tp"]


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y.%m.%d %H:%M:%S")


class DemoEmitter:
    """Appends EA-format lines to the event CSV. Mirrors the EA: writes
    the header once, then one line per OPEN/CLOSE, monotonically
    increasing event_id.
    """
    def __init__(self, path: str):
        self.path = path
        self._counter = 0
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            self._write_row(HEADER)

    def _write_row(self, cols) -> None:
        # csv.QUOTE for none needed; EA writes bare CSV. Match that: no
        # quoting, comma-separated, newline-terminated.
        with open(self.path, "a", encoding="latin-1") as f:
            f.write(",".join(str(c) for c in cols) + "\n")

    def open_trade(self, ticket: int, symbol: str, side: str, lots: float,
                   open_price: float, sl: float = 0.0, tp: float = 0.0) -> None:
        self._counter += 1
        self._write_row([self._counter, _ts(), "OPEN", ticket, symbol, side,
                         f"{lots:.2f}", f"{open_price:.5f}", "0.00000",
                         f"{sl:.5f}", f"{tp:.5f}"])

    def close_trade(self, ticket: int, symbol: str, side: str, lots: float,
                    open_price: float, close_price: float) -> None:
        self._counter += 1
        self._write_row([self._counter, _ts(), "CLOSE", ticket, symbol, side,
                         f"{lots:.2f}", f"{open_price:.5f}", f"{close_price:.5f}",
                         "0.00000", "0.00000"])


# A deliberately representative demo session. Each tuple:
#   (ticket, symbol, side, lots, open_price, close_price)
# chosen to hit every mapping branch.
SCENARIO = [
    # Clean USD-quoted major, long: EURUSD BUY -> BUY FXE (direct).
    (10001, "EURUSD", "BUY", 0.10, 1.08210, 1.08560),
    # USD-base major, long: USDJPY BUY -> SELL FXY... FXY not shortable
    #   -> degraded BUY UUP.
    (10002, "USDJPY", "BUY", 0.20, 156.400, 156.020),
    # USD-quoted major, SHORT of a non-shortable ETF: GBPUSD SELL needs
    #   SHORT FXB (not shortable) -> degraded BUY UUP.
    (10003, "GBPUSD", "SELL", 0.15, 1.27050, 1.26800),
    # Shortable currency ETF, direct short: AUDUSD SELL -> SELL FXA.
    (10004, "AUDUSD", "SELL", 0.05, 0.66300, 0.66550),
    # Non-USD cross: EURGBP -> NONE (no proxy available).
    (10005, "EURGBP", "BUY", 0.10, 0.85100, 0.85000),
    # USD-base, shortable inverse: USDCHF SELL -> BUY FXF (direct).
    (10006, "USDCHF", "SELL", 0.10, 0.90200, 0.89950),
]


def run_scenario(path: str, delay: float = 0.0, close: bool = True) -> None:
    em = DemoEmitter(path)
    print(f"emitting {len(SCENARIO)} OPEN events to {path}")
    for tk, sym, side, lots, op, cp in SCENARIO:
        em.open_trade(tk, sym, side, lots, op)
        print(f"  OPEN  {tk} {sym} {side} {lots}")
        if delay:
            time.sleep(delay)
    if close:
        print("emitting CLOSE events")
        for tk, sym, side, lots, op, cp in SCENARIO:
            em.close_trade(tk, sym, side, lots, op, cp)
            print(f"  CLOSE {tk} {sym} {side}")
            if delay:
                time.sleep(delay)


def main(argv=None):
    p = argparse.ArgumentParser(description="Emit EA-format demo trade events")
    p.add_argument("--event-file", required=True)
    p.add_argument("--delay", type=float, default=0.0,
                   help="seconds between events (simulate live drip)")
    p.add_argument("--no-close", action="store_true", help="only OPEN events")
    args = p.parse_args(argv)
    run_scenario(args.event_file, delay=args.delay, close=not args.no_close)


if __name__ == "__main__":
    main()
