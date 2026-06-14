# HFT Proving Grounds

HFT Proving Grounds is a distributed benchmarking and hosting platform designed to evaluate contestant-submitted trading infrastructure.

The platform enables contestants to deploy exchange or matching-engine implementations, stress-test them using a realistic market simulation bot fleet, validate correctness using deterministic replay, and compare performance through a live leaderboard.

Core Features:

* Matching Engine Benchmarking
* Market Microstructure Simulation
* Distributed Bot Fleet
* Telemetry Collection
* Correctness Validation
* Composite Scoring System
* AI-Based Performance Analysis

Tech Stack:

* Python
* FastAPI
* React
* WebSockets
* Docker
* Market Microstructure Models
* Deterministic Replay Validation

Built for the IICPC Summer Trading Hackathon 2026.
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
