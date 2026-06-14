# HFT Proving Grounds

A distributed benchmarking and hosting platform for evaluating contestant-submitted trading infrastructure.

## Overview

HFT Proving Grounds allows contestants to upload matching engine implementations which are then deployed in isolated environments and stress-tested using a fleet of simulated trading bots.

The platform measures:

* Latency (p50, p90, p99)
* Throughput (TPS)
* Correctness
* Stability

Results are streamed to a live leaderboard and analyzed automatically.

## Core Components

### Submission & Hosting

* Contestant Adapter
* Matching Engine Hosting

### Distributed Bot Fleet

* Noise Traders
* Momentum Traders
* Market Makers

### Telemetry & Validation

* Latency Measurement
* Throughput Tracking
* Price-Time Priority Validation
* Trade Reconciliation

### Analytics

* Composite Scoring
* Automated Engineering Reports

## Tech Stack

Backend:

* Python
* FastAPI

Frontend:

* React
* Vite

Infrastructure:

* Docker
* Docker Compose

## Repository Structure

backend/
frontend/
docs/

## Running

```bash
pip install -r requirements.txt
python e2e_test.py
```

## Authors

IICPC Summer Hackathon 2026 Submission
