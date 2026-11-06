#!/bin/bash
# Cited answers over the four real annual reports (hybrid vs hybrid + answerability gate), on a Mac with
# Ollama; commits and pushes the run. Meant to be sent through mac-bridge, from a clone under ~/code:
#   bash scripts/mac_real_reports_eval.sh
# PDFs come from ~/code/finreports (fetched and hash-checked here); embeddings (bge-m3) and answers
# (qwen3:8b) come from the local Ollama server; nothing leaves the machine.
set -euo pipefail
export NO_PROXY="localhost,127.0.0.1,::1${NO_PROXY:+,$NO_PROXY}" no_proxy="localhost,127.0.0.1,::1"
cd "$(dirname "$0")/.."
git checkout -q master
git pull -q --ff-only
echo "[qa] code at $(git log --oneline -1)"

py=""
for candidate in python3.12 /opt/homebrew/bin/python3.12 /opt/homebrew/opt/python@3.12/bin/python3.12; do
  if command -v "$candidate" >/dev/null 2>&1; then py="$(command -v "$candidate")"; break; fi
done
if [ -z "$py" ]; then
  echo "[qa] installing python@3.12 with Homebrew"; brew install -q python@3.12
  py=/opt/homebrew/opt/python@3.12/bin/python3.12
fi
echo "[qa] python: $("$py" --version)"
[ -x .venv-mac/bin/python ] || "$py" -m venv .venv-mac
.venv-mac/bin/pip install -q --upgrade pip
.venv-mac/bin/pip install -q -e '.[pdf]'

REPORTS="$HOME/code/finreports"
bash scripts/fetch_real_reports.sh "$REPORTS"

curl -fsS --noproxy '*' http://127.0.0.1:11434/api/version >/dev/null \
  || { echo "[qa] starting ollama"; (ollama serve >/dev/null 2>&1 &); sleep 5; }
# A model already present is not pulled again: a registry update would change the weights the
# preregistration describes. Downloads through a desktop proxy sometimes drop ("unexpected EOF"); pull resumes.
for model in bge-m3 qwen3:8b; do
  if ollama show "$model" >/dev/null 2>&1; then continue; fi
  pulled=false
  for attempt in 1 2 3 4 5 6; do
    if ollama pull "$model" >/tmp/ollama-pull.log 2>&1; then pulled=true; break; fi
    echo "[qa] pull $model attempt $attempt failed: $(tail -c 200 /tmp/ollama-pull.log | tr '\r' '\n' | tail -1)"
    sleep 20
  done
  $pulled || { echo "[qa] could not pull $model"; exit 1; }
done
# qwen3:8b loads with a 4096-token context here and silently truncates longer prompts; the registered model is
# the same weights served under a local tag with num_ctx 16384 (no download, no copy of the weights).
SERVED=forgeflow-qwen3-8b-ctx16k
if ! ollama show "$SERVED" >/dev/null 2>&1; then
  modelfile="$(mktemp)"
  printf 'FROM qwen3:8b\nPARAMETER num_ctx 16384\n' >"$modelfile"
  ollama create "$SERVED" -f "$modelfile"
  rm -f "$modelfile"
fi
echo "[qa] models:"; ollama list | grep -E "^(bge-m3|qwen3:8b|$SERVED)" || true

PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 .venv-mac/bin/python preparation/s4/real-reports/run_qa.py \
  --reports "$REPORTS"

git add preparation/s4/real-reports/runs
git -c user.name="Lang Ju" -c user.email="langju123456@gmail.com" commit -q \
  -m "S4 real annual reports: cited answers, hybrid vs hybrid + answerability gate, from the Mac"
for attempt in 1 2 3; do git pull --rebase -q && git push -q && break; sleep 5; done
git log --oneline -1
