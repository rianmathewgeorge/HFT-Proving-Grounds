#!/bin/bash
# HFT Proving Grounds — Full Pipeline Runner
#
# Starts the reference exchange (contestant adapter) and the orchestration
# backend, then prints instructions for running the frontend and starting
# a benchmark run.
#
# Usage: ./run.sh

set -e

echo "Starting reference exchange (port 8001)..."
python3 matching_engine/contestant_adapter.py &
EXCHANGE_PID=$!

sleep 1

echo "Starting orchestration backend (port 8000)..."
python3 backend/main.py &
BACKEND_PID=$!

sleep 1

echo ""
echo "============================================================"
echo "  HFT Proving Grounds is running."
echo "============================================================"
echo "  Reference exchange : http://localhost:8001"
echo "  Backend API        : http://localhost:8000"
echo ""
echo "  Start a benchmark run:"
echo "  curl -X POST http://localhost:8000/runs -H 'Content-Type: application/json' \\"
echo "    -d '{\"contestant_id\": \"demo\", \"target_url\": \"http://localhost:8001\", \"fast_demo\": true}'"
echo ""
echo "  View leaderboard:"
echo "  curl http://localhost:8000/leaderboard"
echo ""
echo "  Frontend:"
echo "  cd frontend && npm install && npm run dev"
echo "============================================================"
echo ""
echo "Press Ctrl+C to stop all services."

trap "kill $EXCHANGE_PID $BACKEND_PID 2>/dev/null" EXIT
wait
