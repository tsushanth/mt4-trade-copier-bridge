# Honest assessment: MT4 demo → Alpaca paper trade copier

**Short answer:** the bridge is *mechanically* real and works — it detects
trade events, maps them defensibly, and places real orders on a real
Alpaca paper account, confirmed against Alpaca's server. As an *actual
copier* it is close to meaningless, because the asset classes don't line
up: you cannot faithfully copy a forex/CFD trade onto US-equity ETFs.
The value of this repo is that it measures *exactly how meaningless*, and
where it breaks, instead of asserting it in the abstract.

This was built as a deliberate experiment on a known asset-class
mismatch. Both of those facts are true and both are reported.

---

## What is real vs. stood-in

| Component | Status |
|---|---|
| Alpaca paper account, orders, cancels, positions | **100% real.** Account `PA3D3WJDU6W4`, orders confirmed via `get_orders` server-side (not just local logs). |
| Symbol mapping + shortability constraints | **Real**, verified live against Alpaca (`get_asset`): FXE/FXY/FXB/FXC are not shortable; UUP/UDN/FXA/FXF are. |
| The MQL4 EA (`ea/TradeCopyExporter.mq4`) | **Real code**, correct MQL4, would run in an MT4 terminal. **Not executed here** — see below. |
| The MT4 demo terminal + demo account + the trades themselves | **Stood in for.** |

### The one honest boundary: the MT4 terminal could not run here

The task's step 1 ("set up an MT4/MT5 demo account from the MetaTrader
terminal") and step 4 ("place real trades on the demo account") both
require the MetaTrader GUI desktop application: a windowed app you install,
click through MetaQuotes' in-terminal demo-account signup on, attach an EA
to, and click "Buy"/"Sell" in. This bridge was built in a **non-interactive,
headless build environment** — no display, no ability to complete a GUI
signup, no ability to click-trade. The official `MetaTrader5` Python API is
Windows-only and also needs a running terminal. So the live MT4 leg
genuinely could not be exercised in this environment, and pretending
otherwise would be exactly the kind of fabrication this project forbids.

What was done instead, and why it's still an honest end-to-end test:

- The EA is written for real and defines the wire format.
- `src/mt4_demo_emitter.py` reproduces that wire format **byte-for-byte**
  (same header, columns, append semantics), standing in *only* for the
  terminal — the piece that physically can't run.
- Everything downstream of the event file — tailing, mapping, sizing,
  pricing, risk-gating, order submission, cancellation, reconciliation —
  ran for real, and the orders are real on Alpaca's servers.

If you run the EA in an actual MT4 terminal, it writes the identical file
and the identical bridge consumes it unchanged. The stand-in is the
terminal, not the copier.

---

## What actually happened (real run, 2026-09-03 ~04:29 UTC)

Six representative forex trades were emitted and copied live to the paper
account:

| MT4 trade | Mapping | Alpaca order | Result |
|---|---|---|---|
| BUY EURUSD 0.10 | **DIRECT** → BUY FXE | BUY 1 FXE @107.22 | accepted on server |
| BUY USDJPY 0.20 | **DEGRADED** → BUY UUP | BUY 2 UUP @28.24 | accepted (FXY not shortable) |
| SELL GBPUSD 0.15 | **DEGRADED** → BUY UUP | BUY 2 UUP @28.24 | accepted (FXB not shortable) |
| SELL AUDUSD 0.05 | **DIRECT** → SELL FXA | SELL 1 FXA @70.90 | accepted (FXA shortable) |
| BUY EURGBP 0.10 | **NONE** | — | logged "no proxy available", not traded |
| SELL USDCHF 0.10 | **DIRECT** → BUY FXF | BUY 1 FXF @108.62 | accepted |

Mapping outcome: **3 direct, 2 degraded, 1 no-proxy** out of 6 (50% clean).
Five real orders placed and confirmed server-side; one honestly refused.

### A real bug the live broker exposed (and the fix)

The run happened after US market hours, so every entry order **rested
unfilled**. When the corresponding CLOSE events arrived, the first
implementation tried to flatten by submitting the opposite side — and
Alpaca **rejected all five as wash trades** (`code 40310000`,
`"potential wash trade detected"`): an opposing order against your own
still-resting order is a wash.

This is a genuine, non-obvious lesson that only surfaced by actually
running it, not simulating it. The fix (in `bridge.py::_handle_close`): on
close, check the entry's fill state first. If it never filled, **cancel the
entry** (no position was ever established) instead of firing an opposing
order; only flatten with a real trade when the entry actually filled. Re-run
confirmed: all five unfilled entries cancelled cleanly on the server, zero
wash-trade rejections, local copy-state reconciled to empty.

---

## How lossy is the symbol mapping, really?

Lossy enough that "copier" is the wrong word for USD-quoted majors, and
outright impossible for crosses. Three failure modes, in increasing severity:

1. **Direction is preserved, magnitude is not.** Even the cleanest case
   (EURUSD → FXE) copies *direction and a fudged size*, never economic
   equivalence. FX-lot notional (100k units/lot) has no clean share count;
   `shares_per_lot` is a flat configurable scale and the risk gate's
   position cap is the real limiter. A "copied" position does not track the
   original's P&L.

2. **Degraded (dollar-basket) fallback loses the specific currency.**
   Alpaca won't let you short FXE/FXY/FXB/FXC. So any trade needing a short
   of those (short EURUSD, long USDJPY, short GBPUSD, long USDCAD…) falls
   back to UUP/UDN — the *dollar basket*, correct in sign but blind to the
   specific cross. In the run above, USDJPY and GBPUSD trades — two very
   different bets — both collapsed to "BUY UUP". They are indistinguishable
   after mapping. That is a large information loss, made explicit
   (`resolution=DEGRADED`) rather than hidden.

3. **Non-USD crosses have no proxy at all.** EURGBP, EURJPY, GBPJPY are bets
   on two foreign currencies; no single US-listed currency ETF captures
   them. These are refused (`resolution=NONE`, logged
   `"no proxy available"`), never faked. On a real forex demo account,
   crosses are common, so a meaningful fraction of trades simply cannot be
   copied.

There is also a **timing/venue mismatch** not even counted above: FX trades
24/5; US equities trade ~6.5h/day. Any FX trade opened outside RTH copies
as a resting order that may fill at a very different time and price, or not
at all (as every trade in the live run demonstrated).

---

## Verdict

- **As an engineering artifact:** yes, it works. Real event feed → real
  mapping → real, risk-gated, dry-run-defaulted orders on a real paper
  account, restart-safe and idempotent, with the close/cancel edge case
  handled correctly against the live broker.
- **As a trade copier:** no, not in any economically meaningful sense.
  Best case copies direction with an arbitrary size onto a proxy; common
  cases (shorts of non-shortable ETFs, non-USD crosses) either degrade to a
  dollar-basket bet or can't be expressed at all. Of a realistic forex demo
  flow, expect a large share to be degraded-or-refused, and none of it to
  replicate the original's P&L.

The mismatch is not a bug to be fixed in code — it's the asset-class gap
the task set out to measure. The honest conclusion is that a forex→equity
copier is a technically-working bridge that is practically meaningless as a
copier, and the mapping module is where you can watch that meaninglessness
accrue, case by case, instead of taking it on faith.
