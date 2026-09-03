"""Map an MT4/MT5 forex symbol + trade direction onto an Alpaca-tradable
US equity ETF + direction.

This is the one genuinely unsolvable-in-general problem of the whole
project, and the module where it would be easiest to cheat. The rule
here is: never invent a mapping that isn't defensible, and never hide a
degraded mapping behind a clean-looking one. When there is no honest
proxy, say so (resolution == NONE) and let the bridge log
"would copy X, no proxy available" instead of forcing a trade.

Why this is hard, concretely
----------------------------
A forex pair XXXYYY is a bet on two currencies at once (long XXX, short
YYY). A currency ETF (FXE, FXB, ...) tracks exactly one currency
against the US dollar. So:

* USD-quoted majors (EURUSD, GBPUSD, AUDUSD, ...): the pair *is*
  "foreign currency vs USD", which is exactly what FXx tracks. Clean-ish
  1:1 proxy, direction preserved.
* USD-base pairs (USDJPY, USDCHF, USDCAD): FXx tracks JPY/USD, i.e. the
  INVERSE of USDJPY. Direction flips.
* Crosses with no USD leg (EURGBP, EURJPY, GBPJPY): two non-USD
  currencies. No single currency ETF captures it. -> NONE.

The second, uglier problem: Alpaca shortability
-----------------------------------------------
FXE, FXY, FXB, FXC are NOT shortable on Alpaca (verified against the
paper account). FXA, FXF, UUP, UDN ARE. So a mapping that says "short
FXE" is not executable. Rather than silently drop those trades, we fall
back to the dollar-basket ETFs UUP (bullish USD) / UDN (bearish USD),
which ARE fully shortable/buyable and move the right direction for any
USD-leg bet -- at the cost of specificity (they track a USD basket, not
the specific cross). That fallback is marked `degraded=True` so the
bridge and the assessment can count exactly how often we had to reach
for it.

Resolution tiers returned:
  DIRECT   -- currency-specific ETF, required direction executable
  DEGRADED -- fell back to UUP/UDN dollar basket (right sign, wrong
              specificity) because the specific ETF couldn't be traded
              in the required direction
  NONE     -- no honest proxy (non-USD cross, or unknown symbol)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Resolution(str, Enum):
    DIRECT = "DIRECT"
    DEGRADED = "DEGRADED"
    NONE = "NONE"


@dataclass(frozen=True)
class MappedOrder:
    resolution: Resolution
    etf: str | None          # Alpaca symbol to trade, or None if NONE
    side: str | None         # "BUY" | "SELL" for the ETF, or None
    degraded: bool
    reason: str              # human-readable explanation, always populated


# Currency ETFs that track FOREIGN_CCY / USD.
# value = (etf, shortable_on_alpaca) -- shortability verified live.
_CCY_ETF = {
    "EUR": ("FXE", False),
    "GBP": ("FXB", False),
    "JPY": ("FXY", False),
    "CHF": ("FXF", True),
    "CAD": ("FXC", False),
    "AUD": ("FXA", True),
}

# Dollar-basket fallback ETFs (both shortable + buyable + fractionable).
_USD_BULL = "UUP"   # up when USD strengthens
_USD_BEAR = "UDN"   # up when USD weakens

_MAJORS = set(_CCY_ETF.keys()) | {"USD"}


def _split_pair(symbol: str) -> tuple[str, str] | None:
    """EURUSD -> ('EUR','USD'). Tolerates broker suffixes like
    'EURUSD.m', 'EURUSDpro', 'EURUSD-5' by stripping to the first 6
    alpha chars. Returns None if it doesn't look like a 6-letter pair.
    """
    s = "".join(ch for ch in symbol.upper() if ch.isalpha())
    if len(s) < 6:
        return None
    base, quote = s[:3], s[3:6]
    return base, quote


def _usd_leg_side(base: str, quote: str, forex_side: str) -> str | None:
    """Is this trade net-long or net-short the US dollar? Returns the
    ETF side to express USD exposure via UUP (bullish USD):
      long USD  -> BUY UUP
      short USD -> SELL UUP
    Only defined when exactly one leg is USD.
    """
    if quote == "USD":
        # XXXUSD: long pair = long XXX = SHORT usd; short pair = long usd
        long_usd = (forex_side == "SELL")
    elif base == "USD":
        # USDXXX: long pair = long usd; short pair = short usd
        long_usd = (forex_side == "BUY")
    else:
        return None
    return "BUY" if long_usd else "SELL"


def map_symbol(symbol: str, forex_side: str) -> MappedOrder:
    """symbol: MT4 symbol e.g. 'EURUSD'. forex_side: 'BUY' or 'SELL'
    (the direction of the MT4 trade). Returns a MappedOrder.
    """
    forex_side = forex_side.upper()
    if forex_side not in ("BUY", "SELL"):
        return MappedOrder(Resolution.NONE, None, None, False,
                           f"invalid forex side {forex_side!r}")

    parts = _split_pair(symbol)
    if parts is None:
        return MappedOrder(Resolution.NONE, None, None, False,
                           f"{symbol!r} is not a recognizable 6-letter FX pair")
    base, quote = parts

    # Non-USD cross (e.g. EURGBP, GBPJPY): no single-ETF proxy.
    if base != "USD" and quote != "USD":
        return MappedOrder(
            Resolution.NONE, None, None, False,
            f"{base}{quote} is a non-USD cross; no single currency ETF "
            f"captures a two-foreign-currency bet")

    # Unknown/exotic majors we don't have an ETF for (e.g. USDSEK, NZDUSD).
    foreign = quote if base == "USD" else base
    if foreign not in _CCY_ETF:
        # We can still express the pure USD leg via UUP/UDN -- it's the
        # honest dollar-basket approximation, so DEGRADED not NONE.
        usd_side = _usd_leg_side(base, quote, forex_side)
        if usd_side is None:
            return MappedOrder(Resolution.NONE, None, None, False,
                               f"no ETF for {foreign} and no USD leg to fall back on")
        etf = _USD_BULL if usd_side == "BUY" else _USD_BEAR
        return MappedOrder(
            Resolution.DEGRADED, etf, "BUY", True,
            f"no currency ETF for {foreign}; expressing USD leg only via "
            f"{etf} (dollar basket, not the specific cross)")

    etf, shortable = _CCY_ETF[foreign]

    # Direction on the currency ETF (which tracks FOREIGN/USD):
    #   XXXUSD: pair side == ETF side (long EURUSD -> BUY FXE)
    #   USDXXX: pair side flips     (long USDJPY -> SELL FXY, short the yen ETF)
    if quote == "USD":
        etf_side = forex_side
    else:  # base == "USD"
        etf_side = "SELL" if forex_side == "BUY" else "BUY"

    # If we can execute the currency ETF in the required direction, done.
    if etf_side == "BUY" or shortable:
        return MappedOrder(
            Resolution.DIRECT, etf, etf_side, False,
            f"{base}{quote} {forex_side} -> {etf_side} {etf} "
            f"({'tracks ' + foreign + '/USD directly' if quote == 'USD' else 'inverse of ' + base + quote})")

    # Required direction is a SHORT of a non-shortable currency ETF.
    # Fall back to the shortable dollar-basket ETF for the USD leg.
    usd_side = _usd_leg_side(base, quote, forex_side)
    etf_fb = _USD_BULL if usd_side == "BUY" else _USD_BEAR
    return MappedOrder(
        Resolution.DEGRADED, etf_fb, "BUY", True,
        f"{base}{quote} {forex_side} needs SHORT {etf} but {etf} is not "
        f"shortable on Alpaca; falling back to BUY {etf_fb} (USD basket, "
        f"correct sign, loses {foreign}-specific exposure)")
