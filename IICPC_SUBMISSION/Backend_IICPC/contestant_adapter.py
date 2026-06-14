"""
Contestant Adapter
===================

Wraps a MatchingEngine instance behind a REST + WebSocket interface that
mimics what a contestant's "simulated exchange" submission must expose.

This serves a dual purpose:
  1. It's the reference exchange contestants are benchmarked against
     (used by the validator for diffing).
  2. It's a runnable example of the exact API contract the bot fleet
     expects, so contestants know what shape their endpoints must have.

API Contract (the contract every contestant submission must implement)
------------------------------------------------------------------------
POST /order
    body: {"order_id": int, "side": "BUY"|"SELL", "type": "LIMIT"|"MARKET",
            "price": float|null, "qty": int}
    returns: {"accepted": bool, "order_id": int, "trades": [...],
              "remaining_qty": int, "server_ts": float}

POST /cancel
    body: {"order_id": int}
    returns: {"accepted": bool, "order_id": int}

GET /book
    returns: {"bids": [[price, qty], ...], "asks": [[price, qty], ...]}

WS /stream
    Pushes trade events and book updates as they occur (used for
    correctness diffing without polling).

Every response includes server_ts (server-side processing timestamp,
set as late as possible before serialization) which the telemetry layer
uses to compute application-level latency. In the production version,
this timestamp is cross-validated against eBPF kernel-level timestamps
captured at the socket layer (see docs/design_doc.md, Section 5).
"""

from __future__ import annotations

import time
import asyncio
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
import uvicorn

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from matching_engine.engine import MatchingEngine, Order, Side, OrderType


app = FastAPI(title="Reference Exchange (Contestant Adapter)")
engine = MatchingEngine()

# Connected WebSocket clients for live trade/book broadcast
_ws_clients: set[WebSocket] = set()


class OrderRequest(BaseModel):
    order_id: int
    side: str          # "BUY" | "SELL"
    type: str          # "LIMIT" | "MARKET"
    price: Optional[float] = None
    qty: int


class CancelRequest(BaseModel):
    order_id: int


def _seq_clock():
    """Logical clock for FIFO ordering. Wall-clock time is used here for
    simplicity; in the distributed bot fleet version, each bot stamps
    its own send-time and the ingester re-sequences by arrival order at
    the exchange, which is the only ordering that matters for price-time
    priority."""
    return time.monotonic()


@app.post("/order")
async def submit_order(req: OrderRequest):
    recv_ts = time.perf_counter()

    order = Order(
        order_id=req.order_id,
        side=Side(req.side),
        order_type=OrderType(req.type),
        price=req.price,
        qty=req.qty,
        ts=_seq_clock(),
    )
    result = engine.submit(order)

    response = {
        "accepted": result.accepted,
        "order_id": result.order_id,
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
        "error": result.error,
        "server_ts": time.perf_counter(),
        "processing_latency_us": (time.perf_counter() - recv_ts) * 1e6,
    }

    if result.trades:
        await _broadcast({"type": "trades", "data": response["trades"]})

    return response


@app.post("/cancel")
async def cancel_order(req: CancelRequest):
    recv_ts = time.perf_counter()
    result = engine.cancel(req.order_id)
    return {
        "accepted": result.accepted,
        "order_id": result.order_id,
        "error": result.error,
        "server_ts": time.perf_counter(),
        "processing_latency_us": (time.perf_counter() - recv_ts) * 1e6,
    }


@app.get("/book")
async def get_book(depth: int = 10):
    return engine.book_snapshot(depth=depth)


@app.get("/health")
async def health():
    return {"status": "ok", "ts": time.time()}


@app.websocket("/stream")
async def stream(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        while True:
            # Keep the connection alive; broadcasts are pushed from _broadcast
            await ws.receive_text()
    except WebSocketDisconnect:
        _ws_clients.discard(ws)


async def _broadcast(message: dict):
    dead = set()
    for client in _ws_clients:
        try:
            await client.send_json(message)
        except Exception:
            dead.add(client)
    _ws_clients.difference_update(dead)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
