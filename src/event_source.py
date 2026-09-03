"""Tail the append-only CSV that TradeCopyExporter.mq4 writes, yielding
one TradeEvent per new line.

Design constraints that matter for a copier you'd trust with (paper)
money:

* Restart-safe: we persist the byte offset we've consumed to a sidecar
  `<file>.offset` file, so restarting the bridge does not re-copy every
  trade that ever happened.
* Idempotent per event: even if the offset file is lost, each event has
  a natural dedupe key (action:ticket) that the bridge tracks, so a
  re-read can't open the same copy twice.
* Tolerant of partial writes: MT4 appends a full line via FileWrite, but
  if we happen to read mid-write we only advance the offset past
  complete newline-terminated lines.
"""
from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass


EXPECTED_HEADER = [
    "event_id", "ts_utc", "action", "ticket", "symbol", "order_type",
    "lots", "open_price", "close_price", "sl", "tp",
]


@dataclass(frozen=True)
class TradeEvent:
    event_id: int
    ts_utc: str
    action: str        # OPEN | CLOSE
    ticket: int
    symbol: str        # forex symbol, e.g. EURUSD
    order_type: str    # BUY | SELL
    lots: float
    open_price: float
    close_price: float
    sl: float
    tp: float

    @property
    def dedupe_key(self) -> str:
        return f"{self.action}:{self.ticket}"


def _parse_row(row: list[str]) -> TradeEvent | None:
    if len(row) < len(EXPECTED_HEADER):
        return None
    if row[0] == "event_id":       # header line
        return None
    try:
        return TradeEvent(
            event_id=int(row[0]),
            ts_utc=row[1],
            action=row[2].strip().upper(),
            ticket=int(row[3]),
            symbol=row[4].strip(),
            order_type=row[5].strip().upper(),
            lots=float(row[6]),
            open_price=float(row[7]),
            close_price=float(row[8]),
            sl=float(row[9]),
            tp=float(row[10]),
        )
    except (ValueError, IndexError):
        return None


class EventFileTail:
    def __init__(self, path: str, offset_path: str | None = None):
        self.path = path
        self.offset_path = offset_path or (path + ".offset")

    def _read_offset(self) -> int:
        try:
            with open(self.offset_path) as f:
                return int(f.read().strip() or "0")
        except (FileNotFoundError, ValueError):
            return 0

    def _write_offset(self, offset: int) -> None:
        tmp = self.offset_path + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(offset))
        os.replace(tmp, self.offset_path)

    def poll(self) -> list[TradeEvent]:
        """Return every complete new event line since the last poll and
        advance the persisted offset past them.
        """
        if not os.path.exists(self.path):
            return []
        offset = self._read_offset()
        size = os.path.getsize(self.path)
        if size < offset:
            # File was truncated/rotated -- start over from the top.
            offset = 0
        if size == offset:
            return []

        with open(self.path, "rb") as f:
            f.seek(offset)
            chunk = f.read()

        # Only consume up to the last complete newline; keep the offset
        # before any trailing partial line so we re-read it next time.
        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            return []
        complete = chunk[: last_nl + 1]
        new_offset = offset + len(complete)

        # EA writes FILE_ANSI; latin-1 round-trips any byte without error.
        text = complete.decode("latin-1")
        events: list[TradeEvent] = []
        for row in csv.reader(io.StringIO(text)):
            if not row:
                continue
            ev = _parse_row(row)
            if ev is not None:
                events.append(ev)

        self._write_offset(new_offset)
        return events

    def reset(self) -> None:
        """Forget consumed offset (re-read the whole file next poll)."""
        try:
            os.remove(self.offset_path)
        except FileNotFoundError:
            pass
