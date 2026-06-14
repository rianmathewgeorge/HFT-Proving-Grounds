"""
Validator — Deterministic Replay Diffing
==========================================

Takes the exact ordered sequence of order messages that the bot fleet sent
to a contestant's exchange (captured as telemetry events) and replays that
same sequence, in the same order, against the reference MatchingEngine.
The resulting trade sequence is then diffed against what the contestant's
exchange actually reported.

Why this matters
-----------------
A naive correctness check ("did the server return 200 OK") cannot catch
the failure modes that actually matter for an exchange: a matching engine
can be "up" and "fast" while silently violating price-time priority,
double-filling an order, or computing the wrong trade price. Those bugs
only show up when you compare the *trade sequence*, not the *response
codes*.

Determinism requirement
-------------------------
For this diff to be meaningful, the bot fleet's order stream must be
processed in the same logical order by both engines. The bot fleet
assigns a monotonically increasing `seq` to every message at send time
(see bot_fleet/fleet.py), and the validator replays messages in seq order.
This sidesteps network-level reordering: even if two bots' HTTP requests
arrive at the contestant's server in a different order than `seq`, the
validator's reference replay uses `seq` order as ground truth, and any
discrepancy this causes is itself informative (it suggests the
contestant's engine is sensitive to arrival order in ways the reference
is not -- itself worth surfacing in the report).

Violation categories detected
-------------------------------
- TRADE_COUNT_MISMATCH: different number of trades for the same order.
- PRICE_MISMATCH: a trade executed at a different price than the
  reference (most commonly indicates a price-priority bug).
- QTY_MISMATCH: a trade filled a different quantity than the reference.
- MISSING_TRADE / EXTRA_TRADE: contestant produced fewer/more trades
  than the reference for the same input.
"""

from __future__ import annotations

import sys
import os
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from matching_engine.engine import MatchingEngine, Order, Side, OrderType


@dataclass
class Violation:
    seq: int
    order_id: int
    category: str
    detail: str


def replay_against_reference(order_events: list[dict]) -> list[dict]:
    """
    Replays a sequence of order events (as logged by the bot fleet) against
    a fresh reference MatchingEngine and returns, for each event, the
    reference engine's trade output in the same shape as the contestant's
    response ("trades": [...]).
    """
    engine = MatchingEngine()
    reference_outputs = []

    for event in order_events:
        if event["event"] != "order_response":
            continue

        order = Order(
            order_id=event["order_id"],
            side=Side(event["side"]),
            order_type=OrderType(event["type"]),
            price=event.get("price"),
            qty=event["qty"],
            ts=event["seq"],  # use seq as the logical clock for determinism
        )
        result = engine.submit(order)

        reference_outputs.append({
            "seq": event["seq"],
            "order_id": event["order_id"],
            "trades": [
                {
                    "trade_id": t.trade_id,
                    "resting_order_id": t.resting_order_id,
                    "incoming_order_id": t.incoming_order_id,
                    "price": t.price,
                    "qty": t.qty,
                }
                for t in result.trades
            ],
            "remaining_qty": result.remaining_qty,
        })

    return reference_outputs


def diff_against_reference(order_events: list[dict]) -> tuple[float, list[Violation]]:
    """
    Returns (correctness_rate, violations).

    correctness_rate = fraction of orders whose trade output (count, prices,
    quantities, and the specific resting order matched against) agrees with
    the reference engine's output for the same order. Checking
    resting_order_id specifically is what catches price-time priority
    violations: two engines can report identical (price, qty) for a trade
    while having matched against a different resting order, which is
    exactly the "size priority instead of time priority" class of bug.
    """
    reference_outputs = {r["seq"]: r for r in replay_against_reference(order_events)}

    violations: list[Violation] = []
    checked = 0
    correct = 0

    for event in order_events:
        if event["event"] != "order_response":
            continue

        seq = event["seq"]
        ref = reference_outputs.get(seq)
        if ref is None:
            continue

        checked += 1
        contestant_trades = event.get("trades", []) or []
        ref_trades = ref["trades"]

        if len(contestant_trades) != len(ref_trades):
            violations.append(Violation(
                seq=seq,
                order_id=event["order_id"],
                category="TRADE_COUNT_MISMATCH",
                detail=f"contestant produced {len(contestant_trades)} trades, "
                       f"reference produced {len(ref_trades)}",
            ))
            continue

        order_ok = True
        for c_trade, r_trade in zip(contestant_trades, ref_trades):
            if abs(c_trade.get("price", -1) - r_trade["price"]) > 1e-9:
                violations.append(Violation(
                    seq=seq,
                    order_id=event["order_id"],
                    category="PRICE_MISMATCH",
                    detail=f"contestant price {c_trade.get('price')} != "
                           f"reference price {r_trade['price']}",
                ))
                order_ok = False
            if c_trade.get("qty", -1) != r_trade["qty"]:
                violations.append(Violation(
                    seq=seq,
                    order_id=event["order_id"],
                    category="QTY_MISMATCH",
                    detail=f"contestant qty {c_trade.get('qty')} != "
                           f"reference qty {r_trade['qty']}",
                ))
                order_ok = False
            if c_trade.get("resting_order_id") != r_trade["resting_order_id"]:
                violations.append(Violation(
                    seq=seq,
                    order_id=event["order_id"],
                    category="TIME_PRIORITY_VIOLATION",
                    detail=f"contestant matched against resting order "
                           f"{c_trade.get('resting_order_id')}, but reference "
                           f"(price-time priority) requires resting order "
                           f"{r_trade['resting_order_id']} to be filled first "
                           f"at this price level",
                ))
                order_ok = False

        if order_ok:
            correct += 1

    correctness_rate = (correct / checked) if checked > 0 else 1.0
    return correctness_rate, violations
