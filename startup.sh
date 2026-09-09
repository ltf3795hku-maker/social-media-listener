#!/usr/bin/env bash
# Azure App Service (Linux) and Cloud Run entrypoint.
# Azure Portal Startup Command: bash startup.sh
# Cloud Run injects PORT (usually 8080).
set -euo pipefail

PORT="${PORT:-8000}"
exec python -m streamlit run streamlit_app.py \
  --server.port "$PORT" \
  --server.address 0.0.0.0 \
  --server.headless true \
  --server.enableCORS false \
  --server.enableWebsocketCompression false \
  --browser.gatherUsageStats false
