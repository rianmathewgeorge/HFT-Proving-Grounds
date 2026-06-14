"""
Tests for the reference matching engine.

These tests double as a specification: any contestant engine claiming
price-time priority correctness must pass equivalent scenarios. The
Telemetry & Validation service (backend/validator.py) runs a superset
of these scenarios against contestant engines and diffs the trade output
against this reference implementation.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from matching_engine.engine import MatchingEngine, Order, Side, OrderType


def test_simple_cross():
    eng = MatchingEngine()
    # Resting sell at 100
    eng.submit(Order(1, Side.SELL, OrderType.LIMIT, 100.0, 10, ts=1))
    # Incoming buy at 100 -> should fully match
    res = eng.submit(Order(2, Side.BUY, OrderType.LIMIT, 100.0, 10, ts=2))

    assert len(res.trades) == 1
    assert res.trades[0].price == 100.0
    assert res.trades[0].qty == 10
    assert res.remaining_qty == 0
    print("test_simple_cross PASSED")


def test_price_time_priority():
    eng = MatchingEngine()
    # Two resting sells at same price, different times -> FIFO
    eng.submit(Order(1, Side.SELL, OrderType.LIMIT, 100.0, 5, ts=1))
    eng.submit(Order(2, Side.SELL, OrderType.LIMIT, 100.0, 5, ts=2))

    # Incoming buy for 5 should match order 1 first (earlier timestamp)
    res = eng.submit(Order(3, Side.BUY, OrderType.LIMIT, 100.0, 5, ts=3))

    assert len(res.trades) == 1
    assert res.trades[0].resting_order_id == 1, "Time priority violated: order 2 filled before order 1"
    print("test_price_time_priority PASSED")


def test_price_priority_over_time():
    eng = MatchingEngine()
    # Sell at 101 placed first, sell at 100 placed second
    eng.submit(Order(1, Side.SELL, OrderType.LIMIT, 101.0, 5, ts=1))
    eng.submit(Order(2, Side.SELL, OrderType.LIMIT, 100.0, 5, ts=2))

    # Buy crosses both -> better price (100) must fill first despite later timestamp
    res = eng.submit(Order(3, Side.BUY, OrderType.LIMIT, 101.0, 5, ts=3))

    assert res.trades[0].price == 100.0, "Price priority violated: worse price filled first"
    assert res.trades[0].resting_order_id == 2
    print("test_price_priority_over_time PASSED")


def test_partial_fill_and_resting():
    eng = MatchingEngine()
    eng.submit(Order(1, Side.SELL, OrderType.LIMIT, 100.0, 5, ts=1))
    res = eng.submit(Order(2, Side.BUY, OrderType.LIMIT, 100.0, 10, ts=2))

    assert len(res.trades) == 1
    assert res.trades[0].qty == 5
    assert res.remaining_qty == 5  # remaining 5 should rest on the book

    snap = eng.book_snapshot()
    assert snap["bids"] == [(100.0, 5)]
    print("test_partial_fill_and_resting PASSED")


def test_cancel():
    eng = MatchingEngine()
    eng.submit(Order(1, Side.BUY, OrderType.LIMIT, 99.0, 10, ts=1))
    res = eng.cancel(1)
    assert res.accepted is True

    snap = eng.book_snapshot()
    assert snap["bids"] == []
    print("test_cancel PASSED")


def test_market_order_sweeps_book():
    eng = MatchingEngine()
    eng.submit(Order(1, Side.SELL, OrderType.LIMIT, 100.0, 5, ts=1))
    eng.submit(Order(2, Side.SELL, OrderType.LIMIT, 101.0, 5, ts=2))

    res = eng.submit(Order(3, Side.BUY, OrderType.MARKET, None, 8, ts=3))

    assert len(res.trades) == 2
    assert res.trades[0].price == 100.0
    assert res.trades[0].qty == 5
    assert res.trades[1].price == 101.0
    assert res.trades[1].qty == 3
    assert res.remaining_qty == 0
    print("test_market_order_sweeps_book PASSED")


def test_self_trade_not_special_cased():
    # Reference engine does not implement self-trade prevention by default.
    # This documents the behaviour explicitly; contestants may add STP and
    # the validator should be configured to expect it if so.
    eng = MatchingEngine()
    eng.submit(Order(1, Side.SELL, OrderType.LIMIT, 100.0, 5, ts=1))
    res = eng.submit(Order(2, Side.BUY, OrderType.LIMIT, 100.0, 5, ts=2))
    assert len(res.trades) == 1
    print("test_self_trade_not_special_cased PASSED (documents baseline behaviour)")


if __name__ == "__main__":
    test_simple_cross()
    test_price_time_priority()
    test_price_priority_over_time()
    test_partial_fill_and_resting()
    test_cancel()
    test_market_order_sweeps_book()
    test_self_trade_not_special_cased()
    print("\nAll matching engine tests passed.")
