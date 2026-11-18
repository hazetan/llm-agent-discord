#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

echo "=== 1. Creating Python virtual environment (.venv) ==="
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate

echo "=== 2. Installing Python dependencies ==="
pip install --upgrade pip
pip install -r requirements.txt

echo "=== 3. Preparing .env file ==="
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    echo "[Info] Created .env from .env.example. Please edit .env before running."
fi

echo "=== 4. Building Docker sandbox image ==="
if [ ! -f "Dockerfile.sandbox" ] && [ -f "Dockerfile.sandbox.example" ]; then
    cp Dockerfile.sandbox.example Dockerfile.sandbox
    echo "[Info] Created Dockerfile.sandbox from Dockerfile.sandbox.example."
fi

if [ -f "Dockerfile.sandbox" ]; then
    docker build -f Dockerfile.sandbox -t sier-sandbox:latest .
else
    docker pull python:3.12-slim
fi

echo "=== 5. Pulling Alpine image for Samba NAS ==="
docker pull alpine:latest

echo "=== Setup Complete! ==="
echo "Next steps:"
echo "  1. Edit .env with your Discord Token, Channel IDs, Ollama URL, and Model names."
echo "  2. Run './run.sh' to start the bot."