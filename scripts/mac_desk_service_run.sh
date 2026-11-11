#!/bin/bash
# The desk service on the real annual reports with the local model, on a Mac; commits and pushes the run.
# Meant to be sent through mac-bridge from a clone under ~/code on branch feat/desk-service:
#   bash scripts/mac_desk_service_run.sh
# Needs Docker (Redis + RabbitMQ from compose.desk-service.yaml), Ollama with bge-m3 and forgeflow-qwen3-8b-ctx16k
# (scripts/mac_real_reports_eval.sh creates it), and the PDFs in ~/code/finreports. Reuses the parsed-report and
# embedding caches of the main clone (~/code/forgeflow/.workbench/reports) when they exist, by copying them.
set -euo pipefail
export NO_PROXY="localhost,127.0.0.1,::1${NO_PROXY:+,$NO_PROXY}" no_proxy="localhost,127.0.0.1,::1"
cd "$(dirname "$0")/.."
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "[desk] code at $(git log --oneline -1) on $BRANCH"

py=""
for candidate in python3.12 /opt/homebrew/bin/python3.12 /opt/homebrew/opt/python@3.12/bin/python3.12; do
  if command -v "$candidate" >/dev/null 2>&1; then py="$(command -v "$candidate")"; break; fi
done
[ -n "$py" ] || { echo "[desk] needs python3.12"; exit 1; }
[ -x .venv-mac/bin/python ] || "$py" -m venv .venv-mac
.venv-mac/bin/pip install -q --upgrade pip
.venv-mac/bin/pip install -q -r requirements.lock
.venv-mac/bin/pip install -q -e '.[pdf,service]'

docker info >/dev/null 2>&1 || { open -a Docker 2>/dev/null || true; for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 3; done; }
docker info >/dev/null 2>&1 || { echo "[desk] Docker is not running; needed for Redis and RabbitMQ"; exit 1; }
docker compose -f compose.desk-service.yaml up -d
for _ in $(seq 1 60); do (exec 3<>/dev/tcp/127.0.0.1/5672) 2>/dev/null && break; sleep 2; done
sleep 5

cache_src="$HOME/code/forgeflow/.workbench/reports"
mkdir -p .desk-service/reports
if [ -d "$cache_src/ingest-cache" ] && [ ! -d .desk-service/reports/ingest-cache ]; then
  cp -R "$cache_src/ingest-cache" .desk-service/reports/
fi
if [ -f "$cache_src/embeddings-bge-m3.pkl" ] && [ ! -f .desk-service/reports/embeddings-bge-m3.pkl ]; then
  cp "$cache_src/embeddings-bge-m3.pkl" .desk-service/reports/
fi

status=0
PYTHONDONTWRITEBYTECODE=1 .venv-mac/bin/python preparation/s5/desk-service/run_real.py --reports "$HOME/code/finreports" \
  || status=$?
docker compose -f compose.desk-service.yaml down >/dev/null 2>&1 || true

git add preparation/s5/desk-service/real-model-run.json 2>/dev/null || true
if ! git diff --cached --quiet; then
  git -c user.name="Lang Ju" -c user.email="langju123456@gmail.com" commit -q \
    -m "desk service on the real annual reports with qwen3:8b, from the Mac (smoke run with timings)"
  git pull -q --rebase origin "$BRANCH" || true
  git push -q origin "$BRANCH"
fi
exit $status
