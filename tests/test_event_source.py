import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from event_source import EventFileTail
from mt4_demo_emitter import DemoEmitter


def test_tail_reads_new_events_and_advances_offset(tmp_path):
    f = str(tmp_path / "events.csv")
    em = DemoEmitter(f)
    tail = EventFileTail(f)

    assert tail.poll() == []  # only header so far

    em.open_trade(1, "EURUSD", "BUY", 0.10, 1.0821)
    evs = tail.poll()
    assert len(evs) == 1
    assert evs[0].action == "OPEN"
    assert evs[0].symbol == "EURUSD"
    assert evs[0].order_type == "BUY"
    assert evs[0].ticket == 1

    # No new lines -> nothing.
    assert tail.poll() == []

    em.close_trade(1, "EURUSD", "BUY", 0.10, 1.0821, 1.0850)
    evs = tail.poll()
    assert len(evs) == 1 and evs[0].action == "CLOSE"


def test_offset_persists_across_new_tail_instances(tmp_path):
    f = str(tmp_path / "events.csv")
    em = DemoEmitter(f)
    em.open_trade(1, "EURUSD", "BUY", 0.10, 1.0821)

    assert len(EventFileTail(f).poll()) == 1
    # A fresh tail (simulating a bridge restart) must NOT re-read it.
    assert EventFileTail(f).poll() == []


def test_partial_trailing_line_is_not_consumed(tmp_path):
    f = str(tmp_path / "events.csv")
    DemoEmitter(f)  # writes header + newline
    # Append a complete line and a partial (no newline) line.
    with open(f, "a") as fh:
        fh.write("1,2026.09.02 10:00:00,OPEN,1,EURUSD,BUY,0.10,1.08210,0.00000,0.00000,0.00000\n")
        fh.write("2,2026.09.02 10:00:01,OPEN,2,GBPUS")  # partial, no newline
    tail = EventFileTail(f)
    evs = tail.poll()
    assert len(evs) == 1 and evs[0].ticket == 1
    # Now complete the partial line.
    with open(f, "a") as fh:
        fh.write("D,SELL,0.15,1.27050,0.00000,0.00000,0.00000\n")
    evs = tail.poll()
    assert len(evs) == 1 and evs[0].ticket == 2 and evs[0].symbol == "GBPUSD"


def test_dedupe_key():
    from event_source import TradeEvent
    ev = TradeEvent(1, "t", "OPEN", 42, "EURUSD", "BUY", 0.1, 1.0, 0.0, 0.0, 0.0)
    assert ev.dedupe_key == "OPEN:42"
