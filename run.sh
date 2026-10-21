#!/usr/bin/env bash
# Arranque local en macOS/Linux: ./run.sh  ->  http://localhost:8000
set -e
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env
python3 -m venv .venv 2>/dev/null || true
source .venv/bin/activate
pip install -q -r backend/requirements-dev.txt
cd backend
python -m pytest -q
uvicorn app.main:app --reload --port 8000
