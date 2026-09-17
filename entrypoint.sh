#!/bin/bash
set -e

# Start headless FastAPI operational service on internal port 8000 in background
echo "Starting SCD2 Copilot Operational API on internal port 8000..."
uvicorn src.scd2_copilot.api:app --host 127.0.0.1 --port 8000 &

# Start Streamlit in foreground on public container port
PORT="${PORT:-8501}"
echo "Starting SCD2 Copilot Streamlit UI on port ${PORT}..."
exec streamlit run app/streamlit_app.py --server.address=0.0.0.0 --server.port="${PORT}"
