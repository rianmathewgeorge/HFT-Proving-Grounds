"""
Integration Test / Demo Script
=================================

Demonstrates the full validation pipeline without needing a live HTTP
server: generates a sequence of order events (as the bot fleet would),
runs that sequence through (a) the correct reference engine and (b) the
deliberately buggy engine, and shows that the validator's diff catches the
price-time-priority violation -- with the exact order IDs and prices.

This is the script the judge demo (Section 6 of the design doc) is built
around: "upload a buggy engine -> bot fleet stress test -> validator
flags a specific price-time priority violation with order IDs."
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from matching_engine.engine import MatchingEngine, Order, Side, OrderType
from sandbox.buggy_engine import BuggyMatchingEngine
from backend.validator import diff_against_reference


def run_engine_and_capture(engine, order_specs: list[dict]) -> list[dict]:
    """Runs a sequence of order specs through `engine` and produces
    telemetry-shaped 'order_response' events, exactly as the bot fleet
    would log them (minus latency fields, which aren't relevant here)."""
    events = []
    for spec in order_specs:
        order = Order(
            order_id=spec["order_id"],
            side=Side(spec["side"]),
            order_type=OrderType(spec["type"]),
            price=spec.get("price"),
            qty=spec["qty"],
            ts=spec["seq"],
        )
        result = engine.submit(order)
        events.append({
            "event": "order_response",
            "seq": spec["seq"],
            "order_id": spec["order_id"],
            "side": spec["side"],
            "type": spec["type"],
            "price": spec.get("price"),
            "qty": spec["qty"],
            "accepted": result.accepted,
            "trades": [
                {"trade_id": t.trade_id, "resting_order_id": t.resting_order_id,
                 "incoming_order_id": t.incoming_order_id, "price": t.price, "qty": t.qty}
                for t in result.trades
            ],
        })
    return events


def main():
    # Scenario: two resting sell orders at the same price (100.0), placed
    # in order. order_id=1 (qty=5) is placed BEFORE order_id=2 (qty=10).
    # Time priority says order_id=1 must be filled first.
    #
    # The buggy engine sorts by size descending, so it will fill order_id=2
    # (qty=10, larger) before order_id=1 (qty=5) -- a clear price-time
    # priority violation that the validator should flag.
    order_specs = [
        {"seq": 1, "order_id": 1, "side": "SELL", "type": "LIMIT", "price": 100.0, "qty": 5},
        {"seq": 2, "order_id": 2, "side": "SELL", "type": "LIMIT", "price": 100.0, "qty": 10},
        {"seq": 3, "order_id": 3, "side": "BUY", "type": "LIMIT", "price": 100.0, "qty": 5},
    ]

    print("=" * 70)
    print("Reference engine (correct price-time priority):")
    print("=" * 70)
    ref_engine = MatchingEngine()
    ref_events = run_engine_and_capture(ref_engine, order_specs)
    for e in ref_events:
        if e["trades"]:
            for t in e["trades"]:
                print(f"  Order {e['order_id']} (seq={e['seq']}) traded with "
                      f"resting order {t['resting_order_id']} @ {t['price']} x{t['qty']}")

    print()
    print("=" * 70)
    print("Contestant's (buggy) engine -- 'size priority' instead of 'time priority':")
    print("=" * 70)
    buggy_engine = BuggyMatchingEngine()
    buggy_events = run_engine_and_capture(buggy_engine, order_specs)
    for e in buggy_events:
        if e["trades"]:
            for t in e["trades"]:
                print(f"  Order {e['order_id']} (seq={e['seq']}) traded with "
                      f"resting order {t['resting_order_id']} @ {t['price']} x{t['qty']}")

    print()
    print("=" * 70)
    print("Validator diff (replaying contestant's order stream against reference):")
    print("=" * 70)
    correctness_rate, violations = diff_against_reference(buggy_events)
    print(f"  Correctness rate: {correctness_rate:.2%}")
    if violations:
        for v in violations:
            print(f"  VIOLATION [{v.category}] seq={v.seq} order_id={v.order_id}: {v.detail}")
    else:
        print("  No violations detected.")

    print()
    print("=" * 70)
    print("Explanation for engineering report:")
    print("=" * 70)
    print("  Order #1 (qty=5) was placed at $100.00 BEFORE Order #2 (qty=10).")
    print("  Price-time priority requires Order #1 to be filled first when")
    print("  Order #3 arrives. The contestant's engine instead filled")
    print("  Order #2 first because it has larger remaining quantity --")
    print("  a 'size priority' bug. This is exactly the class of subtle")
    print("  correctness bug that response-code-only health checks cannot")
    print("  detect, but deterministic replay against a reference engine")
    print("  catches immediately.")


if __name__ == "__main__":
    main()
