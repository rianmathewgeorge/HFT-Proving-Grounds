"""
Reference Matching Engine
==========================

Deterministic, price-time priority (FIFO) limit order book matching engine.
This serves two purposes in the platform:

1. As the "ground truth" reference implementation used to validate
   contestant submissions via deterministic replay diffing.
2. As a runnable example contestant submission for demo purposes
   (exposed via the REST/WebSocket adapter in backend/contestant_adapter.py).

Design notes
------------
- Two sides of the book (bids, asks) are each maintained as a dict keyed by
  price -> deque of orders (FIFO at each price level). This gives O(1)
  amortized insertion at a price level and preserves time priority.
- Best bid / best ask lookups use sorted price keys. For a hackathon-scale
  book (thousands of price levels) this is fine; a production engine would
  use a more cache-friendly structure (e.g. flat arrays indexed by tick,
  or a skip list / red-black tree of price levels with O(log n) best-price
  lookup). This tradeoff is documented in the design doc.
- All matching is deterministic: given the same ordered sequence of inbound
  messages, the engine always produces the same sequence of trades and the
  same final book state. This determinism is what allows the Telemetry &
  Validation layer to replay a bot fleet's order stream against this engine
  and diff the result against a contestant's engine, byte-for-byte.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import itertools


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    CANCEL = "CANCEL"


@dataclass
class Order:
    order_id: int
    side: Side
    order_type: OrderType
    price: Optional[float]   # None for MARKET and CANCEL
    qty: int
    ts: float                # logical timestamp (monotonic sequence number or wall clock)
    remaining: int = field(init=False)

    def __post_init__(self):
        self.remaining = self.qty


@dataclass
class Trade:
    trade_id: int
    resting_order_id: int
    incoming_order_id: int
    price: float
    qty: int
    ts: float


@dataclass
class EngineResult:
    """Returned for every processed message. The validation layer diffs
    sequences of these against a contestant's reported results."""
    accepted: bool
    order_id: int
    trades: list[Trade] = field(default_factory=list)
    remaining_qty: int = 0
    error: Optional[str] = None


class MatchingEngine:
    """
    Single-instrument, price-time priority limit order book.

    Public API:
      - submit(order: Order) -> EngineResult
      - cancel(order_id: int) -> EngineResult
      - best_bid() -> Optional[float]
      - best_ask() -> Optional[float]
      - book_snapshot() -> dict   (for UI / debugging)
    """

    def __init__(self):
        # price -> deque[Order], FIFO within a price level
        self.bids: dict[float, deque[Order]] = {}
        self.asks: dict[float, deque[Order]] = {}
        # order_id -> (side, price) for O(1) cancel lookups
        self._order_index: dict[int, tuple[Side, float]] = {}
        self._trade_id_gen = itertools.count(1)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Internal matching logic
    # ------------------------------------------------------------------

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

        return EngineResult(
            accepted=True,
            order_id=order.order_id,
            trades=trades,
            remaining_qty=order.remaining,
        )

    def _match_market(self, order: Order) -> EngineResult:
        trades: list[Trade] = []
        opposite = self.asks if order.side == Side.BUY else self.bids

        while order.remaining > 0:
            best_price = self.best_ask() if order.side == Side.BUY else self.best_bid()
            if best_price is None:
                break
            trades.extend(self._fill_at_price(order, opposite, best_price))

        # Market orders never rest; any unfilled remainder is dropped (IOC semantics)
        return EngineResult(
            accepted=True,
            order_id=order.order_id,
            trades=trades,
            remaining_qty=order.remaining,
        )

    def _fill_at_price(self, incoming: Order, opposite_book: dict, price: float) -> list[Trade]:
        trades = []
        dq = opposite_book[price]

        while dq and incoming.remaining > 0:
            resting = dq[0]
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
                dq.popleft()
                self._order_index.pop(resting.order_id, None)

        if not dq:
            del opposite_book[price]

        return trades

    def _rest_order(self, order: Order) -> None:
        book = self.bids if order.side == Side.BUY else self.asks
        book.setdefault(order.price, deque()).append(order)
        self._order_index[order.order_id] = (order.side, order.price)
