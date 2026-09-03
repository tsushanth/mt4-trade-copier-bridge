# mt4-trade-copier-bridge

A real, working bridge that reads trade open/close events exported from an
**MT4 demo account** and translates each into an order on an **Alpaca paper
account**, through a risk-gated router.

It is built on a known, deliberate asset-class mismatch: MT4 trades are
forex/CFD; Alpaca trades US equities/options. The point of this repo is not
to pretend that gap away but to build the bridge honestly and **measure
exactly what the mismatch costs**. See
[`docs/ASSESSMENT.md`](docs/ASSESSMENT.md) for the honest verdict (short
version: mechanically real, practically meaningless as a *copier*).

## Architecture

```
  MT4 demo terminal                     this repo
  ┌───────────────────┐   append-only   ┌──────────────┐   marketable   ┌─────────────┐
  │ TradeCopyExporter │──CSV file──────▶│ bridge.py    │──limit orders─▶│ Alpaca      │
  │ .mq4  (EA)        │  trade_events   │  tail→map→   │  (risk-gated)  │ PAPER acct  │
  └───────────────────┘                 │  size→route  │                └─────────────┘
                                        └──────┬───────┘
                       symbol_map.py ──────────┘  forex pair → equity-ETF proxy
                       risk_gates.py ──────────┘  hard limits before every order
```

- **`ea/TradeCopyExporter.mq4`** — minimal MQL4 EA. Watches the demo
  account's open trades; appends one CSV line per OPEN/CLOSE. (A custom
  exporter, not vobornik/mt4-trade-copy, because we only need the export
  half of a copier, not MT4→MT4 copying.)
- **`src/event_source.py`** — restart-safe, idempotent tail of that CSV.
- **`src/symbol_map.py`** — the hard part: forex pair + direction → a
  defensible Alpaca ETF + direction, or an honest "no proxy available".
- **`src/alpaca_adapter.py` / `src/risk_gates.py`** — risk-gated order
  router (reused from the sibling `alpaca-paper-trader` repo; `paper=True`
  hardcoded, no live-trading switch).
- **`src/bridge.py`** — the executor. **Dry-run by default.**
- **`src/mt4_demo_emitter.py`** — byte-identical stand-in for the EA's
  output, used to exercise the bridge where an MT4 GUI terminal can't run
  (see the assessment's "one honest boundary" section).

## Symbol mapping (the honest core)

A forex pair bets on two currencies; a currency ETF tracks one vs USD. Plus
Alpaca won't let you short FXE/FXY/FXB/FXC. So each trade resolves to one of:

- **DIRECT** — currency-specific ETF, required direction executable
  (e.g. `BUY EURUSD → BUY FXE`, `SELL AUDUSD → SELL FXA`).
- **DEGRADED** — fell back to the dollar-basket ETF UUP/UDN because the
  specific ETF couldn't be traded in the needed direction (e.g. `SELL GBPUSD`
  needs a short of non-shortable FXB → `BUY UUP`). Correct sign, loses the
  specific-currency exposure.
- **NONE** — non-USD cross (EURGBP, GBPJPY…) with no single-ETF proxy →
  logged `"would copy X, no proxy available"`, never faked.

| Forex leg | ETF | Shortable on Alpaca |
|---|---|---|
| EUR | FXE | no |
| GBP | FXB | no |
| JPY | FXY | no |
| CAD | FXC | no |
| CHF | FXF | yes |
| AUD | FXA | yes |
| USD basket | UUP (bull) / UDN (bear) | yes |

## Usage

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in Alpaca PAPER keys

# 1) (stand-in for the MT4 terminal) emit a representative demo session
python src/mt4_demo_emitter.py --event-file logs/trade_events.csv --no-close

# 2) dry-run: resolve + price every event, place NOTHING (safe anytime)
python src/bridge.py --event-file logs/trade_events.csv --once --dry-run

# 3) live PAPER: actually submit the copied orders through the risk gate
python src/bridge.py --event-file logs/trade_events.csv --once --live
```

Run without `--once` to poll the file forever (a real always-on copier).
`--dry-run` is the default; `--live` still only ever touches the paper
account.

## Running the real EA

Copy `ea/TradeCopyExporter.mq4` into an MT4 terminal's `MQL4/Experts`
folder, compile in MetaEditor, attach it to any chart on your **demo**
account (confirm it's a demo/practice account first — fake money only), and
allow file writes. It writes `MQL4/Files/trade_events.csv` in the identical
format the emitter produces; point the bridge's `--event-file` at that path.

## Tests

```bash
python -m pytest tests/ -q   # 21 tests: mapping, tail idempotency, bridge flow
```

## Status / honesty notes

- Verified end-to-end against real Alpaca paper account `PA3D3WJDU6W4`;
  orders confirmed server-side, not just in local logs.
- The live MT4 terminal leg was stood in for because a GUI terminal can't
  run in this build environment — the EA is real code, the emitter
  reproduces its exact output. Full detail and the honest verdict:
  [`docs/ASSESSMENT.md`](docs/ASSESSMENT.md).
