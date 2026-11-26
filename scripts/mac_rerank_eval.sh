#!/bin/bash
# Rerank after hybrid retrieval, on a Mac with Ollama: first the 47-question guardrail, then retrieval only
# over the four real annual reports (hybrid vs hybrid + rerank). One command; commits the two runs. Meant to
# be sent through mac-bridge, from a clone under ~/code checked out at the commit that holds the registration:
#   bash scripts/mac_rerank_eval.sh
# Embeddings (bge-m3) come from the local Ollama server. The reranker (BAAI/bge-reranker-v2-m3) runs inside
# the Python process on MPS; its weights (about 2.3 GB) are downloaded once from Hugging Face (set HF_ENDPOINT
# to use a mirror), and the runs then load them from the local cache with the hub switched off. Questions,
# passages and embeddings never leave the machine. No answer or gate model is called.
# Each runner first reproduces the sealed hybrid result and stops (exit 2) if it cannot; a run that stopped
# is still committed, so the difference is on record.
set -euo pipefail
export NO_PROXY="localhost,127.0.0.1,::1${NO_PROXY:+,$NO_PROXY}" no_proxy="localhost,127.0.0.1,::1"
cd "$(dirname "$0")/.."
echo "[rerank] code at $(git log --oneline -1) on $(git rev-parse --abbrev-ref HEAD)"
# The reports record the commit they ran at, so what runs has to be exactly that commit.
git diff --quiet HEAD -- src preparation scripts pyproject.toml \
  || { echo "[rerank] tracked files differ from HEAD; commit or stash them first"; exit 1; }

py=""
for candidate in python3.12 /opt/homebrew/bin/python3.12 /opt/homebrew/opt/python@3.12/bin/python3.12; do
  if command -v "$candidate" >/dev/null 2>&1; then py="$(command -v "$candidate")"; break; fi
done
if [ -z "$py" ]; then
  echo "[rerank] installing python@3.12 with Homebrew"; brew install -q python@3.12
  py=/opt/homebrew/opt/python@3.12/bin/python3.12
fi
echo "[rerank] python: $("$py" --version)"
[ -x .venv-mac/bin/python ] || "$py" -m venv .venv-mac
.venv-mac/bin/pip install -q --upgrade pip
.venv-mac/bin/pip install -q -e '.[pdf,rerank]'

REPORTS="$HOME/code/finreports"
bash scripts/fetch_real_reports.sh "$REPORTS"

curl -fsS --noproxy '*' http://127.0.0.1:11434/api/version >/dev/null \
  || { echo "[rerank] starting ollama"; (ollama serve >/dev/null 2>&1 &); sleep 5; }
# A model already present is not pulled again: a registry update would change the vectors, and the sealed
# hybrid results could then not be reproduced. Downloads through a desktop proxy sometimes drop; pull resumes.
if ! ollama show bge-m3 >/dev/null 2>&1; then
  pulled=false
  for attempt in 1 2 3 4 5 6; do
    if ollama pull bge-m3 >/tmp/ollama-pull.log 2>&1; then pulled=true; break; fi
    echo "[rerank] pull bge-m3 attempt $attempt failed: $(tail -c 200 /tmp/ollama-pull.log | tr '\r' '\n' | tail -1)"
    sleep 20
  done
  $pulled || { echo "[rerank] could not pull bge-m3"; exit 1; }
fi
echo "[rerank] models:"; ollama list | grep -E '^bge-m3' || true

# Reranker weights: fetched once (a dropped download resumes), then nothing below talks to the hub.
RERANKER="BAAI/bge-reranker-v2-m3"
fetched=false
for attempt in 1 2 3 4 5 6; do
  if .venv-mac/bin/python - "$RERANKER" <<'PY'
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

snapshot = Path(snapshot_download(sys.argv[1], ignore_patterns=["onnx/*", "openvino/*", "*.onnx"]))
print(f"[rerank] reranker snapshot {snapshot.name} at {snapshot}")
PY
  then fetched=true; break; fi
  echo "[rerank] reranker download attempt $attempt failed"; sleep 20
done
$fetched || { echo "[rerank] could not download $RERANKER"; exit 1; }
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false

status=0
PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 .venv-mac/bin/python preparation/s2/evaluation/rerank/run_guardrail.py \
  --local-files-only || status=$?
if [ "$status" = 0 ]; then
  GUARDRAIL="$(ls -td preparation/s2/evaluation/rerank/runs/*-real-hybrid-rerank-*/ | head -1)report.json"
  echo "[rerank] guardrail report: $GUARDRAIL"
  PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 .venv-mac/bin/python preparation/s4/real-reports/rerank/run_rerank.py \
    --reports "$REPORTS" --guardrail-report "$GUARDRAIL" --local-files-only || status=$?
else
  echo "[rerank] the guardrail run stopped with status $status; the annual-report run is not started"
fi

for runs in preparation/s2/evaluation/rerank/runs preparation/s4/real-reports/rerank/runs; do
  if [ -d "$runs" ]; then git add "$runs"; fi
done
if ! git diff --cached --quiet; then
  git commit -q \
    -m "Rerank after hybrid: 47-question guardrail and real annual reports, retrieval only, from the Mac"
  if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    for attempt in 1 2 3; do git pull --rebase -q && git push -q && break; sleep 5; done
  else
    echo "[rerank] this branch has no upstream: the runs are committed here and not pushed"
  fi
fi
git log --oneline -1
exit "$status"
