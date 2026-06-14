"""
End-to-end pipeline test.

Runs the contestant adapter (reference exchange) and bot fleet in the same
event loop, feeds the resulting telemetry through ContestantMetrics and
the validator, and prints a leaderboard-style summary. This validates that
all modules integrate correctly without requiring separate server
processes (useful in sandboxed environments where background processes
don't persist across tool calls).
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot_fleet.fleet import BotFleet, RegimeConfig
from backend.telemetry import ContestantMetrics, compute_composite_scores
from backend.validator import diff_against_reference
from matching_engine.engine import MatchingEngine, Order, Side, OrderType
from sandbox.buggy_engine import BuggyMatchingEngine


class InProcessAdapter:
    """Wraps a matching engine and mimics the HTTP adapter's response
    shape, but is called directly (no network) for fast integration
    testing. Adds simulated processing latency to make the latency
    metrics non-trivial."""

    def __init__(self, engine):
        self.engine = engine

    async def handle_order(self, payload: dict) -> dict:
        import time
        import random

        # simulate variable processing latency (50-300us)
        await asyncio.sleep(random.uniform(0.00005, 0.0003))

        order = Order(
            order_id=payload["order_id"],
            side=Side(payload["side"]),
            order_type=OrderType(payload["type"]),
            price=payload.get("price"),
            qty=payload["qty"],
            ts=time.perf_counter(),
        )
        result = self.engine.submit(order)
        return {
            "accepted": result.accepted,
            "order_id": result.order_id,
            "trades": [
                {"trade_id": t.trade_id, "resting_order_id": t.resting_order_id,
                 "incoming_order_id": t.incoming_order_id, "price": t.price, "qty": t.qty}
                for t in result.trades
            ],
            "remaining_qty": result.remaining_qty,
            "error": result.error,
        }


class InProcessBotFleet(BotFleet):
    """Variant of BotFleet that calls an InProcessAdapter directly instead
    of making HTTP requests, for fast in-process integration testing."""

    def __init__(self, adapter: InProcessAdapter, **kwargs):
        super().__init__(target_url="in-process", **kwargs)
        self.adapter = adapter

    async def _agent_loop(self, agent, regime, client=None):
        import time
        import random

        try:
            while True:
                wait = random.expovariate(regime.lambda_per_agent)
                await asyncio.sleep(wait)

                order_payload = agent.next_order(regime)
                if order_payload is None:
                    continue

                seq = next(self.seq_gen)
                send_ts = time.perf_counter()

                body = await self.adapter.handle_order({
                    "order_id": order_payload["order_id"],
                    "side": order_payload["side"],
                    "type": order_payload["type"],
                    "price": order_payload.get("price"),
                    "qty": order_payload["qty"],
                })

                recv_ts = time.perf_counter()
                latency_us = (recv_ts - send_ts) * 1e6

                if body.get("trades"):
                    last_trade_price = body["trades"][-1]["price"]
                    self.market.update_from_trade(last_trade_price)

                await self.telemetry_queue.put({
                    "event": "order_response",
                    "seq": seq,
                    "bot_id": agent.bot_id,
                    "agent_type": agent.agent_type,
                    "order_id": order_payload["order_id"],
                    "side": order_payload["side"],
                    "type": order_payload["type"],
                    "price": order_payload.get("price"),
                    "qty": order_payload["qty"],
                    "send_ts": send_ts,
                    "recv_ts": recv_ts,
                    "latency_us": latency_us,
                    "status_code": 200,
                    "accepted": body.get("accepted"),
                    "trades": body.get("trades", []),
                    "ts": time.time(),
                })

        except asyncio.CancelledError:
            pass


async def run_benchmark(label: str, engine):
    print(f"\n{'='*70}\nRunning benchmark: {label}\n{'='*70}")

    adapter = InProcessAdapter(engine)
    regimes = [
        RegimeConfig("calm_open", duration_s=2, lambda_per_agent=3.0, laplace_scale=0.5),
        RegimeConfig("volatility_spike", duration_s=2, lambda_per_agent=10.0, laplace_scale=2.5),
        RegimeConfig("calm_close", duration_s=2, lambda_per_agent=3.0, laplace_scale=0.5),
    ]
    fleet = InProcessBotFleet(adapter, n_noise=8, n_momentum=4, n_market_makers=2, regimes=regimes)

    metrics = ContestantMetrics(contestant_id=label)
    order_events = []

    async def consumer():
        while True:
            event = await fleet.telemetry_queue.get()
            if event["event"] in ("order_response", "regime_change", "order_error"):
                metrics.record(event)
                if event["event"] == "order_response":
                    order_events.append(event)
            elif event["event"] == "run_complete":
                return

    await asyncio.gather(fleet.run(), consumer())

    # Run validator
    correctness_rate, violations = diff_against_reference(order_events)
    metrics.correctness_rate = correctness_rate
    metrics.correctness_violations = [
        {"seq": v.seq, "order_id": v.order_id, "category": v.category, "detail": v.detail}
        for v in violations
    ]

    summary = metrics.summary()
    print(f"  Total orders:          {summary['total_orders']}")
    print(f"  Throughput (TPS):      {summary['throughput_tps']:.1f}")
    print(f"  p50 latency (us):      {summary['p50_latency_us']:.1f}")
    print(f"  p90 latency (us):      {summary['p90_latency_us']:.1f}")
    print(f"  p99 latency (us):      {summary['p99_latency_us']:.1f}")
    print(f"  p99 degradation ratio: {summary['p99_degradation_ratio']:.2f}")
    print(f"  Correctness rate:      {summary['correctness_rate']:.2%}")
    print(f"  Violations found:      {len(summary['correctness_violations'])}")
    for v in summary['correctness_violations'][:3]:
        print(f"    - [{v['category']}] order_id={v['order_id']}: {v['detail']}")

    return metrics


async def main():
    correct_metrics = await run_benchmark("reference_engine (correct)", MatchingEngine())
    buggy_metrics = await run_benchmark("buggy_engine (size-priority bug)", BuggyMatchingEngine())

    print(f"\n{'='*70}\nComposite Leaderboard\n{'='*70}")
    scores = compute_composite_scores([correct_metrics, buggy_metrics])
    ranked = sorted(scores.items(), key=lambda x: -x[1]["score"])
    for rank, (cid, data) in enumerate(ranked, 1):
        print(f"  #{rank} {cid:35s} score={data['score']:.3f}")

    print("\nAI Engineering Report (buggy engine):")
    from backend.analyzer import generate_report
    report = await generate_report(buggy_metrics.summary())
    print(report)


if __name__ == "__main__":
    asyncio.run(main())
