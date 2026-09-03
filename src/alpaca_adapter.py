"""Thin adapter between the risk-gated order flow and Alpaca's trading
API, via alpaca-py.

Switched from an earlier IBKR-based design (see git history / project
README) once it became clear this session already had working Alpaca
paper-trading credentials from an unrelated prior project (NewsTrader),
with no new account setup needed -- IBKR would have required installing
TWS/Gateway and opening a new account first.

Strategy code should never call alpaca-py directly -- it should only
ever go through GatedOrderRouter, so every order passes the RiskGate
first.
"""
import time

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

from risk_gates import RiskGate


class GatedOrderRouter:
    def __init__(self, risk_gate: RiskGate, api_key: str, secret_key: str, paper: bool = True):
        # paper=True is the default deliberately -- switching to live
        # trading should be an explicit, reviewed change, not a default.
        self.risk_gate = risk_gate
        self.client = TradingClient(api_key, secret_key, paper=paper)
        # Read-only market data client, used to price the ETF leg of a
        # copied trade -- the MT4 open_price is a forex price and is
        # meaningless for sizing/limiting an equity order.
        self._data = StockHistoricalDataClient(api_key, secret_key)

    def get_account(self):
        return self.client.get_account()

    def latest_price(self, symbol: str) -> float:
        """Most recent trade price for an equity symbol. Used both to
        set a marketable limit and to give the risk gate a real notional
        to check against.
        """
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        trade = self._data.get_stock_latest_trade(req)[symbol]
        return float(trade.price)

    def submit_copy_order(self, symbol: str, side: str, qty: float,
                          ref_price: float, slippage_bps: float = 25.0):
        """Copy a trade as a marketable limit order: a BUY is priced
        slightly above and a SELL slightly below the reference price, so
        it fills like a market order during RTH but still carries an
        explicit worst-case price (never an uncapped market order).
        Passes through the risk gate exactly like submit_limit_order.
        """
        buffer = ref_price * (slippage_bps / 10_000.0)
        limit_price = round(ref_price + buffer if side == "BUY" else ref_price - buffer, 2)
        return self.submit_limit_order(symbol, side, qty, limit_price)

    def submit_limit_order(self, symbol: str, side: str, qty: float, limit_price: float):
        now = time.time()
        self.risk_gate.check_order(symbol, side, qty, limit_price, now)  # raises on breach

        order = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
        )
        result = self.client.submit_order(order)
        self.risk_gate.record_order_sent(now)
        return result

    def get_order(self, order_id: str):
        """Fetch current server-side state of a previously-submitted order."""
        return self.client.get_order_by_id(order_id)

    def cancel_order(self, order_id: str) -> None:
        self.client.cancel_order_by_id(order_id)

    def on_fill(self, symbol: str, side: str, qty: float, fill_price: float, entry_price: float | None = None):
        """Call this from a fill notification (e.g. Alpaca's trade
        update stream) to keep the risk gate's position/pnl state in
        sync with reality.
        """
        self.risk_gate.record_fill(symbol, side, qty, fill_price, entry_price)
