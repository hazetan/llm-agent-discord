#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

export METRICS_PORT="${METRICS_PORT:-11435}"
echo "Starting Desktop Metrics Server on port ${METRICS_PORT}..."
exec python3 desktop_metrics_server.py