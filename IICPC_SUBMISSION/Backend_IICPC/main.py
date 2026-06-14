"""
Orchestration Backend
=======================

FastAPI service that ties together the bot fleet, telemetry, validator,
and scoring modules into a runnable platform with a leaderboard API.

In the production architecture (see docs/design_doc.md), this service is
split into separate microservices (Submission Service, Orchestrator,
Telemetry Ingester, Leaderboard Service) communicating over Kafka/Redpanda
with state in TimescaleDB/Redis/Postgres. For the MVP, all of this logic
runs in a single process with in-memory state, which is sufficient to
demonstrate the full pipeline end-to-end and is explicitly documented as
the "MVP collapse" of the microservice architecture.

Endpoints
----------
POST /runs                  -> start a benchmark run against a target URL
GET  /runs/{run_id}         -> get run status + current metrics
GET  /leaderboard           -> composite scores across all completed runs
GET  /runs/{run_id}/report  -> LLM-generated engineering report
WS   /runs/{run_id}/live    -> live telemetry stream for the frontend
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_fleet.fleet import BotFleet, RegimeConfig, DEFAULT_REGIMES
from backend.telemetry import ContestantMetrics, ScoringWeights, compute_composite_scores
from backend.validator import diff_against_reference
from backend.analyzer import generate_report


app = FastAPI(title="HFT Proving Grounds — Orchestration Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class RunRequest(BaseModel):
    contestant_id: str
    target_url: str
    n_noise: int = 20
    n_momentum: int = 10
    n_market_makers: int = 5
    # If omitted, DEFAULT_REGIMES (calm -> volatile -> calm) is used.
    fast_demo: bool = False  # shortens regimes to ~5s each for live demos


class RunState:
    def __init__(self, run_id: str, contestant_id: str, target_url: str):
        self.run_id = run_id
        self.contestant_id = contestant_id
        self.target_url = target_url
        self.status = "pending"  # pending | running | completed | failed
        self.metrics = ContestantMetrics(contestant_id=contestant_id)
        self.order_events: list[dict] = []  # raw events, for validator replay
        self.created_at = time.time()
        self.completed_at: Optional[float] = None
        self.error: Optional[str] = None
        self._ws_clients: set[WebSocket] = set()

    async def broadcast(self, message: dict):
        dead = set()
        for ws in self._ws_clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        self._ws_clients.difference_update(dead)


# In-memory run registry. In production this is backed by Postgres
# (run metadata) + TimescaleDB (telemetry time series).
_runs: dict[str, RunState] = {}


@app.post("/runs")
async def start_run(req: RunRequest):
    run_id = str(uuid.uuid4())[:8]
    state = RunState(run_id, req.contestant_id, req.target_url)
    _runs[run_id] = state

    regimes = None
    if req.fast_demo:
        regimes = [
            RegimeConfig("calm_open", duration_s=4, lambda_per_agent=1.0, laplace_scale=0.5),
            RegimeConfig("volatility_spike", duration_s=4, lambda_per_agent=8.0, laplace_scale=2.5),
            RegimeConfig("calm_close", duration_s=4, lambda_per_agent=1.0, laplace_scale=0.5),
        ]

    fleet = BotFleet(
        target_url=req.target_url,
        n_noise=req.n_noise,
        n_momentum=req.n_momentum,
        n_market_makers=req.n_market_makers,
        regimes=regimes,
    )

    asyncio.create_task(_run_benchmark(state, fleet))
    return {"run_id": run_id, "status": "started"}


async def _run_benchmark(state: RunState, fleet: BotFleet):
    state.status = "running"

    async def consumer():
        while True:
            event = await fleet.telemetry_queue.get()

            if event["event"] == "order_response":
                state.metrics.record(event)
                state.order_events.append(event)
            elif event["event"] == "regime_change":
                state.metrics.record(event)
            elif event["event"] == "order_error":
                state.metrics.record(event)
            elif event["event"] == "run_complete":
                await state.broadcast({"type": "run_complete"})
                return

            await state.broadcast({
                "type": "telemetry",
                "event": event,
                "summary": state.metrics.summary(),
            })

    try:
        await asyncio.gather(fleet.run(), consumer())

        # Run the validator on the captured order stream
        correctness_rate, violations = diff_against_reference(state.order_events)
        state.metrics.correctness_rate = correctness_rate
        state.metrics.correctness_violations = [
            {"seq": v.seq, "order_id": v.order_id, "category": v.category, "detail": v.detail}
            for v in violations
        ]

        state.status = "completed"
        state.completed_at = time.time()
        await state.broadcast({"type": "completed", "summary": state.metrics.summary()})

    except Exception as e:
        state.status = "failed"
        state.error = str(e)
        await state.broadcast({"type": "failed", "error": str(e)})


@app.get("/runs/{run_id}")
async def get_run(run_id: str):
    state = _runs.get(run_id)
    if not state:
        raise HTTPException(404, "run not found")
    return {
        "run_id": state.run_id,
        "contestant_id": state.contestant_id,
        "status": state.status,
        "created_at": state.created_at,
        "completed_at": state.completed_at,
        "error": state.error,
        "summary": state.metrics.summary(),
    }


@app.get("/leaderboard")
async def leaderboard():
    completed = [s.metrics for s in _runs.values() if s.status == "completed"]
    if not completed:
        return {"entries": []}

    scores = compute_composite_scores(completed)
    entries = sorted(
        [{"contestant_id": cid, **data} for cid, data in scores.items()],
        key=lambda x: -x["score"],
    )
    return {"entries": entries}


@app.websocket("/runs/{run_id}/live")
async def run_live(ws: WebSocket, run_id: str):
    state = _runs.get(run_id)
    if not state:
        await ws.close(code=4004)
        return

    await ws.accept()
    state._ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        state._ws_clients.discard(ws)


@app.get("/runs/{run_id}/report")
async def get_report(run_id: str):
    state = _runs.get(run_id)
    if not state:
        raise HTTPException(404, "run not found")
    if state.status != "completed":
        raise HTTPException(409, "run not yet completed")

    report = await generate_report(state.metrics.summary())
    return {"run_id": run_id, "report": report}


@app.get("/health")
async def health():
    return {"status": "ok", "ts": time.time()}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
