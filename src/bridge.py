"""mt4-trade-copier-bridge: read real trade events exported from an MT4
demo account and translate each into an Alpaca paper order.

Run modes
---------
  --dry-run   (default): resolve + size + price every event and LOG what
              it WOULD send to Alpaca. No orders are placed. Safe to run
              against live data any time.
  --live      actually submit the copied orders to the Alpaca *paper*
              account through the risk gate. Still paper money -- there
              is deliberately no live-trading switch in this repo.

The executor is dry-run by default on purpose (same house rule as the
sibling trading repos): a copier that fires orders off an external feed
is exactly the kind of thing that should never auto-submit until a human
flips it on.

What this bridge does NOT pretend to do: it does not claim the Alpaca
ETF order is economically equivalent to the MT4 forex trade. It copies
*direction and rough size* onto the most defensible proxy, and logs
honestly (including "no proxy available") when it can't. See
docs/ASSESSMENT.md.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from event_source import EventFileTail, TradeEvent
from symbol_map import map_symbol, Resolution
from risk_gates import RiskGate, RiskLimits
from alpaca_adapter import GatedOrderRouter


DEFAULT_EVENT_FILE = os.path.join(os.path.dirname(__file__), "..", "logs", "trade_events.csv")
DEFAULT_STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "logs", "copy_state.json")
DEFAULT_LOG_FILE = os.path.join(os.path.dirname(__file__), "..", "logs", "bridge.log")


def _make_logger(log_file: str) -> logging.Logger:
    log = logging.getLogger("bridge")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
    fh = logging.FileHandler(log_file)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    return log


class CopyState:
    """Persistent map: MT4 ticket -> the Alpaca copy we opened for it, so
    a later CLOSE event can flatten exactly what we opened. Also records
    processed event dedupe-keys for idempotency across restarts.
    """
    def __init__(self, path: str):
        self.path = path
        self.open_copies: dict[str, dict] = {}
        self.processed: set[str] = set()
        self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            self.open_copies = data.get("open_copies", {})
            self.processed = set(data.get("processed", []))
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def save(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"open_copies": self.open_copies,
                       "processed": sorted(self.processed)}, f, indent=2)
        os.replace(tmp, self.path)


def size_shares(lots: float, shares_per_lot: float) -> int:
    """MT4 lots -> equity shares. This is an intentionally simple, honest
    fudge: forex-lot notional (100k units/lot) has no clean equivalent in
    ETF shares, so we use a flat, configurable scale and let the risk
    gate's max_position_per_symbol be the real cap. Minimum 1 share so a
    tiny 0.01-lot demo trade still produces a visible copy.
    """
    return max(1, round(lots * shares_per_lot))


class Bridge:
    def __init__(self, router: GatedOrderRouter | None, state: CopyState,
                 log: logging.Logger, dry_run: bool, shares_per_lot: float):
        self.router = router
        self.state = state
        self.log = log
        self.dry_run = dry_run
        self.shares_per_lot = shares_per_lot
        # Counters for the end-of-run honesty summary.
        self.stats = {"open": 0, "close": 0, "direct": 0, "degraded": 0,
                      "none": 0, "submitted": 0, "dry_run": 0, "skipped": 0,
                      "gate_blocked": 0, "errors": 0}

    def handle(self, ev: TradeEvent) -> None:
        if ev.dedupe_key in self.state.processed:
            self.log.info("skip duplicate %s (event_id=%s)", ev.dedupe_key, ev.event_id)
            return

        if ev.action == "OPEN":
            self._handle_open(ev)
        elif ev.action == "CLOSE":
            self._handle_close(ev)
        else:
            self.log.warning("unknown action %r (event_id=%s)", ev.action, ev.event_id)
            self.stats["skipped"] += 1
            return

        self.state.processed.add(ev.dedupe_key)
        self.state.save()

    def _handle_open(self, ev: TradeEvent) -> None:
        self.stats["open"] += 1
        m = map_symbol(ev.symbol, ev.order_type)
        self.log.info("OPEN ticket=%s %s %s %.2f lots -> [%s] %s",
                      ev.ticket, ev.order_type, ev.symbol, ev.lots, m.resolution.value, m.reason)

        if m.resolution == Resolution.NONE:
            self.stats["none"] += 1
            self.log.warning("would copy %s %s (ticket=%s) but NO PROXY AVAILABLE -- not trading",
                             ev.order_type, ev.symbol, ev.ticket)
            return
        self.stats["direct" if m.resolution == Resolution.DIRECT else "degraded"] += 1

        qty = size_shares(ev.lots, self.shares_per_lot)
        self._place(ev, m.etf, m.side, qty, opening=True)

    def _handle_close(self, ev: TradeEvent) -> None:
        self.stats["close"] += 1
        copy = self.state.open_copies.get(str(ev.ticket))
        if copy is None:
            self.log.info("CLOSE ticket=%s: no open copy on record (never copied, "
                          "or already closed) -- nothing to flatten", ev.ticket)
            self.stats["skipped"] += 1
            return

        # Real-broker subtlety learned the hard way: the entry order may
        # still be RESTING (unfilled) -- e.g. the market was closed when we
        # copied it. In that case we never actually took a position, so the
        # right action is to CANCEL the entry, not to fire an opposing
        # order. An opposing order against your own resting order is a wash
        # trade and Alpaca rejects it (code 40310000). Only when the entry
        # actually filled do we place a flattening trade.
        oid = copy.get("alpaca_order_id")
        if not self.dry_run and self.router is not None and oid:
            filled_qty = self._entry_filled_qty(oid)
            if filled_qty <= 0:
                if self._cancel_entry(oid):
                    self.log.info("CLOSE ticket=%s: entry %s never filled -> cancelled, "
                                  "no position to flatten", ev.ticket, oid)
                    self.state.open_copies.pop(str(ev.ticket), None)
                    self.state.save()
                return
            copy = {**copy, "qty": filled_qty}   # flatten only what actually filled

        flat_side = "SELL" if copy["side"] == "BUY" else "BUY"
        self.log.info("CLOSE ticket=%s -> flatten copy: %s %s %s",
                      ev.ticket, flat_side, copy["qty"], copy["etf"])
        placed_ok = self._place(ev, copy["etf"], flat_side, copy["qty"], opening=False)
        # Only forget the copy once we've actually flattened it (or in
        # dry-run, where nothing was placed). A failed flatten keeps the
        # copy on record so it can be retried, rather than silently lost.
        if self.dry_run or placed_ok:
            self.state.open_copies.pop(str(ev.ticket), None)

    def _entry_filled_qty(self, order_id: str) -> int:
        try:
            o = self.router.get_order(order_id)
            return int(float(getattr(o, "filled_qty", 0) or 0))
        except Exception as e:                           # noqa: BLE001
            self.log.warning("could not read entry order %s status: %s", order_id, e)
            return 0

    def _cancel_entry(self, order_id: str) -> bool:
        try:
            self.router.cancel_order(order_id)
            return True
        except Exception as e:                           # noqa: BLE001
            self.log.warning("could not cancel entry order %s: %s", order_id, e)
            return False

    def _place(self, ev: TradeEvent, etf: str, side: str, qty: int, opening: bool) -> bool:
        """Returns True if an order was submitted (or dry-run logged),
        False if it failed. Callers use this to decide bookkeeping.
        """
        # Price the ETF leg. In dry-run with no router we can't fetch a
        # live price, so we note that and still log the intended order.
        ref_price = None
        if self.router is not None:
            try:
                ref_price = self.router.latest_price(etf)
            except Exception as e:                       # noqa: BLE001
                self.log.warning("could not fetch price for %s: %s", etf, e)

        if self.dry_run:
            self.stats["dry_run"] += 1
            px = f"@~{ref_price:.2f}" if ref_price else "@<no price: dry-run w/o data client>"
            self.log.info("DRY-RUN would submit: %s %s %s %s (ticket=%s)",
                          side, qty, etf, px, ev.ticket)
            if opening:
                self.state.open_copies[str(ev.ticket)] = {
                    "etf": etf, "side": side, "qty": qty, "opened_event_id": ev.event_id}
            return True

        if ref_price is None:
            self.stats["errors"] += 1
            self.log.error("cannot submit %s %s %s: no reference price", side, qty, etf)
            return False
        try:
            order = self.router.submit_copy_order(etf, side, qty, ref_price)
            self.stats["submitted"] += 1
            self.log.info("SUBMITTED %s %s %s -> alpaca order id=%s status=%s",
                          side, qty, etf, order.id, order.status)
            if opening:
                self.state.open_copies[str(ev.ticket)] = {
                    "etf": etf, "side": side, "qty": qty, "opened_event_id": ev.event_id,
                    "alpaca_order_id": str(order.id)}
            return True
        except Exception as e:                           # noqa: BLE001
            name = type(e).__name__
            if name == "KillSwitchTripped" or name == "ValueError":
                self.stats["gate_blocked"] += 1
                self.log.warning("RISK GATE blocked %s %s %s: %s", side, qty, etf, e)
            else:
                self.stats["errors"] += 1
                self.log.error("submit failed %s %s %s: %s", side, qty, etf, e)
            return False

    def summary(self) -> str:
        s = self.stats
        return ("run summary -- events: {open} OPEN / {close} CLOSE | "
                "mapping: {direct} direct, {degraded} degraded, {none} no-proxy | "
                "orders: {submitted} submitted, {dry_run} dry-run, {gate_blocked} "
                "gate-blocked, {skipped} skipped, {errors} errors").format(**s)


def build_router(dry_run: bool, log: logging.Logger) -> GatedOrderRouter | None:
    """Create the Alpaca router. Even in dry-run we build it when creds
    exist, so we can fetch real ETF prices; if creds are missing we run
    dry-run with no live prices rather than crashing.
    """
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        if not dry_run:
            raise SystemExit("ALPACA_API_KEY / ALPACA_SECRET_KEY required for --live")
        log.warning("no Alpaca creds in env -- dry-run without live ETF prices")
        return None
    gate = RiskGate(limits=RiskLimits(
        max_position_per_symbol=50, max_total_notional=25_000,
        max_daily_loss=1_000, max_orders_per_minute=20))
    return GatedOrderRouter(gate, key, secret, paper=True)


def main(argv=None):
    p = argparse.ArgumentParser(description="MT4 demo -> Alpaca paper trade copier bridge")
    p.add_argument("--event-file", default=DEFAULT_EVENT_FILE)
    p.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    p.add_argument("--log-file", default=DEFAULT_LOG_FILE)
    p.add_argument("--shares-per-lot", type=float, default=10.0)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    mode.add_argument("--live", dest="dry_run", action="store_false",
                      help="actually place orders on the Alpaca PAPER account")
    p.add_argument("--once", action="store_true",
                   help="drain current events and exit (default: poll forever)")
    p.add_argument("--poll-interval", type=float, default=1.0)
    args = p.parse_args(argv)

    log = _make_logger(args.log_file)
    log.info("bridge starting -- mode=%s event_file=%s shares_per_lot=%s",
             "DRY-RUN" if args.dry_run else "LIVE (paper)", args.event_file, args.shares_per_lot)

    router = build_router(args.dry_run, log)
    if router is not None:
        try:
            acct = router.get_account()
            log.info("alpaca paper account %s status=%s equity=%s",
                     acct.account_number, acct.status, acct.equity)
        except Exception as e:                           # noqa: BLE001
            log.warning("could not reach Alpaca account (continuing): %s", e)

    state = CopyState(args.state_file)
    tail = EventFileTail(args.event_file)
    bridge = Bridge(router, state, log, args.dry_run, args.shares_per_lot)

    try:
        while True:
            for ev in tail.poll():
                bridge.handle(ev)
            if args.once:
                break
            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        log.info(bridge.summary())


if __name__ == "__main__":
    main()
