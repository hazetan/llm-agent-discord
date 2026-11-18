#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

if [ ! -f ".env" ]; then
    echo "[Error] .env file not found. Run './setup.sh' first."
    exit 1
fi

if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

export PYTHONPATH="$(pwd)/src"
exec python3 src/main.py