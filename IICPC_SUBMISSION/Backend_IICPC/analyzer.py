"""
LLM Engineering Report Analyzer
==================================

Generates a natural-language engineering report from a contestant's
telemetry summary and correctness violations, using the Anthropic API.

This is intentionally NOT "sentiment analysis on a chat log" -- it's a
structured-data-to-engineering-narrative agent: the input is a JSON
summary of quantitative metrics (latency percentiles, throughput,
degradation ratio, specific correctness violations with order IDs), and
the output is a grounded technical explanation a contestant could act on.

Design choices
---------------
- The prompt explicitly forbids the model from inventing root causes that
  aren't supported by the provided metrics (e.g. it shouldn't claim "this
  is caused by garbage collection pauses" unless GC-related signals were
  actually provided -- in the MVP we don't have GC instrumentation, so the
  prompt is scoped to what we *can* observe: latency percentiles,
  degradation under load, and correctness violations with concrete order
  IDs/prices).
- Output is structured as: (1) one-paragraph summary, (2) up to 3 ranked
  findings each tied to a specific metric or violation, (3) one concrete
  next step per finding. This keeps the report actionable rather than
  generic praise/criticism.
- In the production architecture, this analyzer would additionally
  receive eBPF-derived latency decompositions (network vs. queueing vs.
  application time) and could then make causal claims like "your p99
  spikes correlate with time spent in the kernel socket buffer, suggesting
  backpressure rather than slow matching logic." That capability is
  documented as future work in docs/design_doc.md Section 5.
"""

from __future__ import annotations

import json
import os


REPORT_SYSTEM_PROMPT = """You are an engineering reviewer for a trading \
exchange benchmarking platform. You will be given a JSON object containing \
quantitative telemetry for a contestant's matching engine submission: \
latency percentiles (microseconds), throughput, a p99 degradation ratio \
(p99 latency under high load vs. low load -- values much greater than 1 \
indicate the system does not degrade gracefully under load), and a list of \
correctness violations detected via deterministic replay against a \
reference engine (each violation includes the specific order IDs and \
prices involved).

Write a short engineering report with exactly this structure:

1. One paragraph (2-3 sentences) summarizing overall performance.
2. Up to 3 findings, each one sentence, each explicitly referencing a \
specific number or violation from the input data. Do not state findings \
that aren't directly supported by the provided numbers -- do not \
speculate about causes (e.g. garbage collection, lock contention) unless \
the data explicitly indicates them.
3. For each finding, one concrete, actionable next step.

Be direct and technical. Do not use superlatives or encouragement \
language. Write for an audience of systems engineers who will use this \
to debug their code."""


def build_report_prompt(metrics_summary: dict) -> str:
    return (
        "Here is the telemetry summary for this submission:\n\n"
        f"{json.dumps(metrics_summary, indent=2)}\n\n"
        "Write the engineering report as specified."
    )


async def generate_report(metrics_summary: dict, api_key: str | None = None) -> str:
    """
    Calls the Anthropic API to generate the engineering report.

    Requires the `anthropic` package and an API key (passed explicitly or
    via the ANTHROPIC_API_KEY environment variable). If no key is
    available, falls back to a deterministic template-based report so the
    pipeline remains demoable without network access / API credentials.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    if not api_key:
        return _fallback_report(metrics_summary)

    try:
        import anthropic
    except ImportError:
        return _fallback_report(metrics_summary)

    client = anthropic.AsyncAnthropic(api_key=api_key)
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=REPORT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_report_prompt(metrics_summary)}],
    )

    text_parts = [block.text for block in response.content if block.type == "text"]
    return "\n".join(text_parts)


def _fallback_report(m: dict) -> str:
    """
    Deterministic, template-based report used when no API key is
    configured. Mirrors the structure the LLM is prompted to produce,
    using simple threshold rules instead of a model -- this keeps the
    demo runnable offline and makes the LLM's value-add legible (compare
    this template output against the LLM's output in the demo).
    """
    lines = []

    p99 = m.get("p99_latency_us")
    tput = m.get("throughput_tps")
    degr = m.get("p99_degradation_ratio")
    correctness = m.get("correctness_rate")
    violations = m.get("correctness_violations", [])

    lines.append(
        f"This submission processed {m.get('total_orders', 0)} orders at "
        f"{tput:.1f} TPS with a p99 latency of {p99:.0f}us "
        f"and a correctness rate of {correctness:.1%}."
        if p99 is not None and tput is not None and correctness is not None
        else "Insufficient telemetry to summarize this run."
    )

    findings = []

    if degr is not None and degr > 1.5:
        findings.append((
            f"p99 latency degraded by {degr:.2f}x between the lowest- and "
            f"highest-load regimes, indicating the system does not degrade "
            f"gracefully under load.",
            "Profile the order-processing path under sustained high message "
            "rates to identify the operation whose cost scales with queue "
            "depth or book size."
        ))

    if violations:
        first = violations[0]
        findings.append((
            f"A {first['category']} was detected at order_id={first['order_id']}: "
            f"{first['detail']}.",
            "Review the matching logic for the affected price level, "
            "specifically how resting orders at the same price are ordered "
            "and selected for matching."
        ))

    if correctness is not None and correctness == 1.0 and (degr is None or degr <= 1.5):
        findings.append((
            "No correctness violations were detected and latency remained "
            "stable across load regimes.",
            "Consider stress-testing with a larger bot fleet or longer "
            "high-load regime duration to surface less common failure modes."
        ))

    for i, (finding, action) in enumerate(findings[:3], 1):
        lines.append(f"\n{i}. {finding}\n   Next step: {action}")

    return "\n".join(lines)
