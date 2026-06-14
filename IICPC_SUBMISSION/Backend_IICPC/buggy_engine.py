"""
Buggy Contestant Engine (Demo Fixture)
=========================================

A deliberately flawed matching engine used to demonstrate the validator's
ability to catch price-time priority violations via deterministic replay.

The bug: instead of matching against the best-priced resting order first,
this engine matches against whichever resting order at the touch has the
LARGEST quantity (a "size priority" bug instead of "time priority"). This
is a realistic class of bug -- a naive implementation might iterate a
price level's orders sorted by size for some unrelated reason (e.g. trying
to minimize partial fills) and never test against the time-priority
invariant.

This file is intentionally a near-duplicate of matching_engine/engine.py
with one method changed, so the diff in the demo is easy to narrate.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import itertools

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from matching_engine.engine import Side, OrderType, Order, Trade, EngineResult


class BuggyMatchingEngine:
    """Same interface as MatchingEngine, but _fill_at_price violates
    time priority by matching the largest resting order first."""

    def __init__(self):
        self.bids: dict[float, deque[Order]] = {}
        self.asks: dict[float, deque[Order]] = {}
        self._order_index: dict[int, tuple[Side, float]] = {}
        self._trade_id_gen = itertools.count(1)

    def best_bid(self) -> Optional[float]:
        non_empty = [p for p, dq in self.bids.items() if dq]
        return max(non_empty) if non_empty else None

    def best_ask(self) -> Optional[float]:
        non_empty = [p for p, dq in self.asks.items() if dq]
        return min(non_empty) if non_empty else None

    def submit(self, order: Order) -> EngineResult:
        if order.order_type == OrderType.CANCEL:
            return self.cancel(order.order_id)
        if order.order_type == OrderType.MARKET:
            return self._match_market(order)
        return self._match_limit(order)

    def cancel(self, order_id: int) -> EngineResult:
        loc = self._order_index.get(order_id)
        if loc is None:
            return EngineResult(accepted=False, order_id=order_id, error="UNKNOWN_ORDER")
        side, price = loc
        book = self.bids if side == Side.BUY else self.asks
        dq = book.get(price)
        if dq is None:
            return EngineResult(accepted=False, order_id=order_id, error="UNKNOWN_ORDER")
        for i, o in enumerate(dq):
            if o.order_id == order_id:
                del dq[i]
                del self._order_index[order_id]
                if not dq:
                    del book[price]
                return EngineResult(accepted=True, order_id=order_id, remaining_qty=0)
        return EngineResult(accepted=False, order_id=order_id, error="UNKNOWN_ORDER")

    def book_snapshot(self, depth: int = 10) -> dict:
        bid_levels = sorted(
            ((p, sum(o.remaining for o in dq)) for p, dq in self.bids.items() if dq),
            key=lambda x: -x[0],
        )[:depth]
        ask_levels = sorted(
            ((p, sum(o.remaining for o in dq)) for p, dq in self.asks.items() if dq),
            key=lambda x: x[0],
        )[:depth]
        return {"bids": bid_levels, "asks": ask_levels}

    def _match_limit(self, order: Order) -> EngineResult:
        trades: list[Trade] = []
        opposite = self.asks if order.side == Side.BUY else self.bids
        while order.remaining > 0:
            best_price = self.best_ask() if order.side == Side.BUY else self.best_bid()
            if best_price is None:
                break
            crosses = (
                order.price >= best_price if order.side == Side.BUY
                else order.price <= best_price
            )
            if not crosses:
                break
            trades.extend(self._fill_at_price(order, opposite, best_price))
        if order.remaining > 0:
            self._rest_order(order)
        return EngineResult(accepted=True, order_id=order.order_id, trades=trades, remaining_qty=order.remaining)

    def _match_market(self, order: Order) -> EngineResult:
        trades: list[Trade] = []
        opposite = self.asks if order.side == Side.BUY else self.bids
        while order.remaining > 0:
            best_price = self.best_ask() if order.side == Side.BUY else self.best_bid()
            if best_price is None:
                break
            trades.extend(self._fill_at_price(order, opposite, best_price))
        return EngineResult(accepted=True, order_id=order.order_id, trades=trades, remaining_qty=order.remaining)

    def _fill_at_price(self, incoming: Order, opposite_book: dict, price: float) -> list[Trade]:
        """BUG: sorts resting orders by remaining qty (descending) instead
        of preserving FIFO/time order. This violates price-time priority
        whenever multiple orders rest at the same price level."""
        trades = []
        dq = opposite_book[price]

        # --- BUG: re-order by size instead of respecting FIFO ---
        ordered = sorted(dq, key=lambda o: -o.remaining)

        while ordered and incoming.remaining > 0:
            resting = ordered[0]
            fill_qty = min(incoming.remaining, resting.remaining)

            trade = Trade(
                trade_id=next(self._trade_id_gen),
                resting_order_id=resting.order_id,
                incoming_order_id=incoming.order_id,
                price=price,
                qty=fill_qty,
                ts=incoming.ts,
            )
            trades.append(trade)

            incoming.remaining -= fill_qty
            resting.remaining -= fill_qty

            if resting.remaining == 0:
                dq.remove(resting)
                ordered.pop(0)
                self._order_index.pop(resting.order_id, None)
            else:
                ordered = sorted(dq, key=lambda o: -o.remaining)

        if not dq:
            del opposite_book[price]

        return trades

    def _rest_order(self, order: Order) -> None:
        book = self.bids if order.side == Side.BUY else self.asks
        book.setdefault(order.price, deque()).append(order)
        self._order_index[order.order_id] = (order.side, order.price)
