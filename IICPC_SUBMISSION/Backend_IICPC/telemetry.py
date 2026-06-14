"""
Telemetry & Scoring
====================

Consumes the raw event stream produced by the bot fleet (one event per
order response / error) and computes:

  1. Latency percentiles (p50, p90, p99) — application-level latency as
     reported by the contestant's server_ts minus the bot's send_ts.
     (In the production architecture, these are cross-checked against
     eBPF-captured kernel timestamps to decompose latency into
     network / queueing / application components — see docs/design_doc.md
     Section 5. The MVP reports application-level latency only.)

  2. Throughput — orders processed per second, both as an overall average
     and as a time series (for the "latency vs instantaneous load" chart).

  3. Correctness — fraction of orders accepted without error, plus (when
     a reference run is available) a trade-by-trade diff against the
     reference engine's output for the same input sequence.

  4. Composite score — a weighted combination of normalized latency,
     throughput, and correctness metrics.

Composite scoring methodology
-------------------------------
Each raw metric is converted to a z-score relative to the distribution of
that metric across all contestants in the current competition, then
combined with fixed weights:

    score = w_throughput * z(throughput)
          + w_latency     * z(-p99_latency)      # negated: lower is better
          + w_correctness * correctness_rate
          + w_stability   * z(-p99_degradation)  # negated: lower is better

Weights (w_throughput=0.30, w_latency=0.30, w_correctness=0.30,
w_stability=0.10) are intentionally exposed as a config rather than
hardcoded constants, both so the platform operator can re-weight
between competitions and so contestants can see exactly how their score
is constructed (transparency is itself a design requirement for any
benchmarking platform that wants to be trusted).

p99_degradation is computed as (p99_latency in the highest-load regime) /
(p99_latency in the lowest-load regime). A system that "falls off a
cliff" under load will show a large ratio even if its average latency
looks fine; this is the single metric most informative about whether a
contestant's engine would survive a real market open, and is the
operationalization of the project's "risk modeling" component (it is a
crude proxy for tail-risk-under-stress, analogous to stress-testing a
portfolio under a shocked scenario rather than just looking at average
VaR).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ContestantMetrics:
    contestant_id: str
    latencies_us: list[float] = field(default_factory=list)
    timestamps: list[float] = field(default_factory=list)  # wall-clock recv_ts per order
    accepted_count: int = 0
    error_count: int = 0
    total_count: int = 0
    regime_latencies: dict[str, list[float]] = field(default_factory=dict)
    current_regime: str = "unknown"

    # populated by the validator after a reference-engine diff
    correctness_rate: Optional[float] = None
    correctness_violations: list[dict] = field(default_factory=list)

    def record(self, event: dict):
        if event["event"] == "order_response":
            self.total_count += 1
            if event.get("accepted"):
                self.accepted_count += 1
            lat = event["latency_us"]
            self.latencies_us.append(lat)
            self.timestamps.append(event["ts"])
            self.regime_latencies.setdefault(self.current_regime, []).append(lat)
        elif event["event"] == "order_error":
            self.total_count += 1
            self.error_count += 1
        elif event["event"] == "regime_change":
            self.current_regime = event["regime"]

    # ------------------------------------------------------------------
    # Derived statistics
    # ------------------------------------------------------------------

    def percentile(self, p: float) -> Optional[float]:
        if not self.latencies_us:
            return None
        data = sorted(self.latencies_us)
        k = (len(data) - 1) * (p / 100)
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return data[int(k)]
        return data[f] + (data[c] - data[f]) * (k - f)

    def p50(self) -> Optional[float]:
        return self.percentile(50)

    def p90(self) -> Optional[float]:
        return self.percentile(90)

    def p99(self) -> Optional[float]:
        return self.percentile(99)

    def throughput_tps(self) -> float:
        if len(self.timestamps) < 2:
            return 0.0
        span = self.timestamps[-1] - self.timestamps[0]
        return self.total_count / span if span > 0 else 0.0

    def acceptance_rate(self) -> float:
        if self.total_count == 0:
            return 0.0
        return self.accepted_count / self.total_count

    def p99_degradation_ratio(self) -> Optional[float]:
        """Ratio of p99 latency in the highest-rate regime vs the
        lowest-rate regime. >1 means latency got worse under load;
        a value much greater than 1 indicates non-graceful degradation."""
        regimes_with_data = {
            name: sorted(lats) for name, lats in self.regime_latencies.items() if lats
        }
        if len(regimes_with_data) < 2:
            return None

        def p99_of(lats):
            k = (len(lats) - 1) * 0.99
            f, c = math.floor(k), math.ceil(k)
            if f == c:
                return lats[int(k)]
            return lats[f] + (lats[c] - lats[f]) * (k - f)

        p99s = {name: p99_of(lats) for name, lats in regimes_with_data.items()}
        baseline = min(p99s.values())
        worst = max(p99s.values())
        if baseline <= 0:
            return None
        return worst / baseline

    def summary(self) -> dict:
        return {
            "contestant_id": self.contestant_id,
            "p50_latency_us": self.p50(),
            "p90_latency_us": self.p90(),
            "p99_latency_us": self.p99(),
            "throughput_tps": self.throughput_tps(),
            "acceptance_rate": self.acceptance_rate(),
            "total_orders": self.total_count,
            "error_count": self.error_count,
            "p99_degradation_ratio": self.p99_degradation_ratio(),
            "correctness_rate": self.correctness_rate,
            "correctness_violations": self.correctness_violations,
        }


@dataclass
class ScoringWeights:
    throughput: float = 0.30
    latency: float = 0.30
    correctness: float = 0.30
    stability: float = 0.10

    def validate(self):
        total = self.throughput + self.latency + self.correctness + self.stability
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            raise ValueError(f"Scoring weights must sum to 1.0, got {total}")


def _zscore(value: float, values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (value - mean) / std


def compute_composite_scores(
    all_metrics: list[ContestantMetrics],
    weights: ScoringWeights = ScoringWeights(),
) -> dict[str, dict]:
    """
    Computes a composite score for each contestant relative to the field.

    Returns a dict mapping contestant_id -> {score, components, summary}.
    """
    weights.validate()

    throughputs = [m.throughput_tps() for m in all_metrics]
    p99s = [m.p99() or 0.0 for m in all_metrics]
    degradations = [m.p99_degradation_ratio() or 1.0 for m in all_metrics]

    results = {}
    for m in all_metrics:
        z_throughput = _zscore(m.throughput_tps(), throughputs)
        z_neg_p99 = -_zscore(m.p99() or 0.0, p99s)
        z_neg_degradation = -_zscore(m.p99_degradation_ratio() or 1.0, degradations)
        correctness = m.correctness_rate if m.correctness_rate is not None else m.acceptance_rate()

        score = (
            weights.throughput * z_throughput
            + weights.latency * z_neg_p99
            + weights.correctness * correctness
            + weights.stability * z_neg_degradation
        )

        results[m.contestant_id] = {
            "score": score,
            "components": {
                "z_throughput": z_throughput,
                "z_neg_p99_latency": z_neg_p99,
                "correctness_rate": correctness,
                "z_neg_degradation": z_neg_degradation,
            },
            "summary": m.summary(),
        }

    return results
