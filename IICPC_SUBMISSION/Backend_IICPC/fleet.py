"""
Bot Fleet — Market Microstructure Simulation
==============================================

This module generates synthetic order flow that mimics real limit order
book dynamics, rather than uniform-random load. The goal is twofold:

  1. Stress-test contestant exchanges with traffic that has realistic
     statistical properties (clustering, fat-tailed price offsets,
     inventory-driven quoting), so latency/throughput numbers are
     meaningful for "could this survive a real market open."
  2. Provide a deterministic, replayable order stream (each bot logs
     every message with a sequence number) so the Telemetry &
     Validation layer can feed the *exact same* sequence into the
     reference engine and a contestant's engine and diff outputs.

Quantitative design
--------------------
Order arrivals
    Each agent's order submissions follow a Poisson process with rate
    lambda (orders per second). Inter-arrival times are therefore
    exponential: T ~ Exp(lambda). This is the standard first-order
    approximation for order arrival in LOB models (see e.g. Cont,
    Stoikov & Talreja 2010, "A stochastic model for order book
    dynamics").

Agent archetypes (mixture model)
    - NoiseTrader: submits limit orders at prices offset from the
      current mid-price by an amount drawn from a Laplace (double
      exponential) distribution. The Laplace distribution has fatter
      tails than Gaussian, which better matches the empirically
      observed concentration of orders near the touch with a long
      tail of orders deep in the book.
    - MomentumTrader: tracks the sign of the last K mid-price changes;
      submits orders in the direction of the recent trend with
      probability proportional to the strength of that trend. This
      injects autocorrelation into the synthetic price path, which
      is necessary to test whether a contestant's engine handles
      directional order flow (one-sided pressure) without latency
      degradation -- a uniform random book never one-sidedly empties.
    - MarketMaker: maintains a two-sided quote (bid and ask) around
      the mid-price with a configurable spread, and skews its quote
      based on its current inventory (inventory-skew quoting, a
      simplified Avellaneda-Stoikov style heuristic): if the MM is
      net long, it lowers both quotes slightly to encourage selling
      and discourage further buying, and vice versa. This is what
      keeps the book populated with resting liquidity throughout the
      test (without it, aggressive orders would quickly empty a thin
      book and throughput would be artificially limited by liquidity
      rather than by the contestant's engine).

Regime switching
    The overall arrival rate lambda and the Laplace scale parameter
    are stepped through "regimes" (calm -> volatile -> calm) during a
    test run. This produces the latency-under-load ramp that the
    Telemetry layer plots (p99 vs. instantaneous throughput), which is
    the single most informative chart for diagnosing whether a system
    degrades gracefully or falls off a cliff.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import random
import time
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional

import httpx


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class BotOrderMessage:
    """A single message a bot sends to the exchange. Logged verbatim so
    the validator can replay the exact sequence later."""
    seq: int
    bot_id: str
    agent_type: str
    order_id: int
    side: str
    type: str            # LIMIT | MARKET | CANCEL
    price: Optional[float]
    qty: int
    send_ts: float        # wall-clock send time (perf_counter)


@dataclass
class RegimeConfig:
    name: str
    duration_s: float
    lambda_per_agent: float   # Poisson rate, orders/sec, per agent
    laplace_scale: float      # price offset scale (in price ticks)


DEFAULT_REGIMES = [
    RegimeConfig("calm_open", duration_s=10, lambda_per_agent=1.0, laplace_scale=0.5),
    RegimeConfig("volatility_spike", duration_s=10, lambda_per_agent=6.0, laplace_scale=2.5),
    RegimeConfig("calm_close", duration_s=10, lambda_per_agent=1.0, laplace_scale=0.5),
]


class MarketState:
    """Shared, approximate view of the market that agents condition on.
    Updated from trade broadcasts; agents do not have perfect information
    (mirrors real participants observing a delayed/partial book)."""

    def __init__(self, initial_mid: float = 100.0):
        self.mid = initial_mid
        self.mid_history: list[float] = [initial_mid]
        self.last_trade_price: Optional[float] = None

    def update_from_trade(self, price: float):
        self.last_trade_price = price
        self.mid = price
        self.mid_history.append(price)
        if len(self.mid_history) > 200:
            self.mid_history.pop(0)

    def recent_trend(self, k: int = 5) -> float:
        """Returns mean of last k mid-price changes. Positive = uptrend."""
        if len(self.mid_history) < k + 1:
            return 0.0
        diffs = [
            self.mid_history[-i] - self.mid_history[-i - 1]
            for i in range(1, k + 1)
        ]
        return sum(diffs) / len(diffs)


class Agent:
    """Base class for all bot agent archetypes."""

    def __init__(self, bot_id: str, market: MarketState, order_id_gen: itertools.count):
        self.bot_id = bot_id
        self.market = market
        self.order_id_gen = order_id_gen

    def next_order(self, regime: RegimeConfig) -> Optional[dict]:
        raise NotImplementedError

    def _round_to_tick(self, price: float, tick: float = 0.5) -> float:
        return round(price / tick) * tick


def _laplace_sample(scale: float) -> float:
    """Inverse-CDF sampler for a Laplace(0, scale) distribution.

    For U ~ Uniform(-1/2, 1/2):
        X = -scale * sign(U) * ln(1 - 2|U|)
    is Laplace(0, scale) distributed. Avoids a numpy dependency in the
    hot path of the bot fleet, which matters when spawning thousands of
    agents at high message rates.
    """
    import math
    u = random.random() - 0.5
    sign = -1.0 if u < 0 else 1.0
    magnitude = max(1 - 2 * abs(u), 1e-9)  # guard against log(0)
    return -scale * sign * math.log(magnitude)


class NoiseTrader(Agent):
    """Submits limit orders at prices offset from mid by a Laplace draw.

    The Laplace (double-exponential) distribution is used instead of a
    Gaussian because empirical LOB studies show order placement is sharply
    concentrated near the touch with a fat tail of orders placed deep in
    the book -- a Laplace distribution captures this peakedness/fat-tail
    combination with a single scale parameter, which is convenient for the
    regime-switching mechanism (see RegimeConfig.laplace_scale).
    """

    agent_type = "noise"

    def next_order(self, regime: RegimeConfig) -> Optional[dict]:
        side = random.choice([Side.BUY, Side.SELL])
        offset = _laplace_sample(regime.laplace_scale)
        price = self.market.mid + offset
        price = max(0.5, self._round_to_tick(price))
        qty = random.choice([1, 2, 5, 10])

        return {
            "order_id": next(self.order_id_gen),
            "side": side.value,
            "type": "LIMIT",
            "price": price,
            "qty": qty,
        }


class MomentumTrader(Agent):
    """Submits orders in the direction of the recent price trend."""

    agent_type = "momentum"

    def next_order(self, regime: RegimeConfig) -> Optional[dict]:
        trend = self.market.recent_trend(k=5)

        if trend == 0:
            side = random.choice([Side.BUY, Side.SELL])
        else:
            # Follow the trend with probability proportional to its strength,
            # capped at 0.9 to avoid fully deterministic behaviour.
            follow_prob = min(0.9, 0.5 + abs(trend) * 0.5)
            trend_side = Side.BUY if trend > 0 else Side.SELL
            side = trend_side if random.random() < follow_prob else (
                Side.SELL if trend_side == Side.BUY else Side.BUY
            )

        # Momentum traders are more aggressive: price closer to or
        # crossing the touch, larger size during trends.
        offset = _laplace_sample(regime.laplace_scale * 0.3)
        price = self.market.mid + (offset if side == Side.BUY else -abs(offset))
        price = max(0.5, self._round_to_tick(price))
        qty = random.choice([5, 10, 20])

        return {
            "order_id": next(self.order_id_gen),
            "side": side.value,
            "type": "LIMIT",
            "price": price,
            "qty": qty,
        }


class MarketMaker(Agent):
    """Two-sided quoter with inventory-skew (simplified Avellaneda-Stoikov)."""

    agent_type = "market_maker"

    def __init__(self, bot_id: str, market: MarketState, order_id_gen: itertools.count,
                 base_spread: float = 1.0, inventory_skew_factor: float = 0.05):
        super().__init__(bot_id, market, order_id_gen)
        self.inventory = 0  # net position (positive = long)
        self.base_spread = base_spread
        self.inventory_skew_factor = inventory_skew_factor
        self._toggle = 0

    def next_order(self, regime: RegimeConfig) -> Optional[dict]:
        # Alternate between posting bid and ask each tick to keep both
        # sides of the book populated without doubling message rate.
        self._toggle ^= 1
        skew = -self.inventory * self.inventory_skew_factor

        if self._toggle == 0:
            price = self._round_to_tick(self.market.mid - self.base_spread / 2 + skew)
            side = Side.BUY
        else:
            price = self._round_to_tick(self.market.mid + self.base_spread / 2 + skew)
            side = Side.SELL

        price = max(0.5, price)
        qty = 10

        return {
            "order_id": next(self.order_id_gen),
            "side": side.value,
            "type": "LIMIT",
            "price": price,
            "qty": qty,
        }

    def record_fill(self, side: str, qty: int):
        self.inventory += qty if side == "BUY" else -qty


# ----------------------------------------------------------------------
# Fleet orchestration
# ----------------------------------------------------------------------

class BotFleet:
    """
    Spawns N concurrent agents, each running an async loop that:
      1. Sleeps for an Exp(lambda) inter-arrival time.
      2. Generates an order via its archetype's model.
      3. Sends it to the target exchange via HTTP POST /order.
      4. Logs the message + response for telemetry.

    All telemetry is pushed onto an asyncio.Queue, which a separate
    consumer drains and forwards to the ingestion pipeline (Kafka in
    production; an in-memory list for the MVP, see backend/telemetry.py).
    """

    def __init__(self, target_url: str, n_noise: int = 20, n_momentum: int = 10,
                 n_market_makers: int = 5, regimes: Optional[list[RegimeConfig]] = None):
        self.target_url = target_url.rstrip("/")
        self.market = MarketState()
        self.order_id_gen = itertools.count(1)
        self.seq_gen = itertools.count(1)
        self.regimes = regimes or DEFAULT_REGIMES
        self.telemetry_queue: asyncio.Queue = asyncio.Queue()

        self.agents: list[Agent] = []
        for i in range(n_noise):
            self.agents.append(NoiseTrader(f"noise_{i}", self.market, self.order_id_gen))
        for i in range(n_momentum):
            self.agents.append(MomentumTrader(f"momentum_{i}", self.market, self.order_id_gen))
        for i in range(n_market_makers):
            self.agents.append(MarketMaker(f"mm_{i}", self.market, self.order_id_gen))

        self._stop = asyncio.Event()

    async def run(self):
        """Runs the full regime sequence, spawning one task per agent."""
        async with httpx.AsyncClient(timeout=2.0) as client:
            for regime in self.regimes:
                await self.telemetry_queue.put({
                    "event": "regime_change",
                    "regime": regime.name,
                    "ts": time.time(),
                })
                tasks = [
                    asyncio.create_task(self._agent_loop(agent, regime, client))
                    for agent in self.agents
                ]
                await asyncio.sleep(regime.duration_s)
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        await self.telemetry_queue.put({"event": "run_complete", "ts": time.time()})

    async def _agent_loop(self, agent: Agent, regime: RegimeConfig, client: httpx.AsyncClient):
        try:
            while True:
                # Poisson process: exponential inter-arrival times
                wait = random.expovariate(regime.lambda_per_agent)
                await asyncio.sleep(wait)

                order_payload = agent.next_order(regime)
                if order_payload is None:
                    continue

                seq = next(self.seq_gen)
                send_ts = time.perf_counter()

                msg = BotOrderMessage(
                    seq=seq,
                    bot_id=agent.bot_id,
                    agent_type=agent.agent_type,
                    order_id=order_payload["order_id"],
                    side=order_payload["side"],
                    type=order_payload["type"],
                    price=order_payload.get("price"),
                    qty=order_payload["qty"],
                    send_ts=send_ts,
                )

                try:
                    resp = await client.post(f"{self.target_url}/order", json={
                        "order_id": msg.order_id,
                        "side": msg.side,
                        "type": msg.type,
                        "price": msg.price,
                        "qty": msg.qty,
                    })
                    recv_ts = time.perf_counter()
                    latency_us = (recv_ts - send_ts) * 1e6

                    body = resp.json() if resp.status_code == 200 else {}

                    if body.get("trades"):
                        last_trade_price = body["trades"][-1]["price"]
                        self.market.update_from_trade(last_trade_price)
                        if isinstance(agent, MarketMaker):
                            for t in body["trades"]:
                                if t["resting_order_id"] == msg.order_id or t["incoming_order_id"] == msg.order_id:
                                    agent.record_fill(msg.side, t["qty"])

                    await self.telemetry_queue.put({
                        "event": "order_response",
                        "seq": msg.seq,
                        "bot_id": msg.bot_id,
                        "agent_type": msg.agent_type,
                        "order_id": msg.order_id,
                        "side": msg.side,
                        "type": msg.type,
                        "price": msg.price,
                        "qty": msg.qty,
                        "send_ts": msg.send_ts,
                        "recv_ts": recv_ts,
                        "latency_us": latency_us,
                        "status_code": resp.status_code,
                        "accepted": body.get("accepted"),
                        "trades": body.get("trades", []),
                        "ts": time.time(),
                    })

                except (httpx.RequestError, httpx.TimeoutException) as e:
                    await self.telemetry_queue.put({
                        "event": "order_error",
                        "seq": msg.seq,
                        "bot_id": msg.bot_id,
                        "error": str(e),
                        "ts": time.time(),
                    })

        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8001"

    async def main():
        fleet = BotFleet(target_url=target, n_noise=10, n_momentum=5, n_market_makers=3,
                          regimes=[RegimeConfig("smoke_test", duration_s=5, lambda_per_agent=2.0, laplace_scale=1.0)])

        async def consumer():
            count = 0
            errors = 0
            while True:
                item = await fleet.telemetry_queue.get()
                if item["event"] == "order_response":
                    count += 1
                elif item["event"] == "order_error":
                    errors += 1
                elif item["event"] == "run_complete":
                    print(f"\nRun complete. {count} orders processed, {errors} errors.")
                    return

        await asyncio.gather(fleet.run(), consumer())

    asyncio.run(main())
