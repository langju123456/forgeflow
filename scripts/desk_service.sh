#!/usr/bin/env bash
# Start the 年报问答 desk service on this Mac: Redis + RabbitMQ (Docker), then retrieval server, workers, gateway.
#   bash scripts/desk_service.sh            # then open http://127.0.0.1:8790/
# Needs: Docker, .venv with `pip install -e '.[pdf,service]'`, web/dist (npm ci && npm run build in web/),
# Ollama with bge-m3 and forgeflow-qwen3-8b-ctx16k (scripts/mac_real_reports_eval.sh creates the latter),
# and the annual-report PDFs in ~/code/finreports (scripts/fetch_real_reports.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose -f compose.desk-service.yaml up -d
for _ in $(seq 1 30); do
  (exec 3<>/dev/tcp/127.0.0.1/5672) 2>/dev/null && break
  sleep 1
done
exec .venv/bin/python -m forgeflow.desk_service all --workers "${WORKERS:-2}" "$@"
