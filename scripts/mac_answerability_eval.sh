#!/bin/bash
# Hybrid retrieval alone vs hybrid + answerability gate, on a Mac with Ollama; commits and pushes the run.
# Meant to be sent through mac-bridge, from a clone of this repo under ~/code:
#   bash scripts/mac_answerability_eval.sh
# Embeddings (bge-m3) and verdicts (qwen3:8b) come from the local Ollama server; nothing leaves the machine.
set -euo pipefail
export NO_PROXY="localhost,127.0.0.1,::1${NO_PROXY:+,$NO_PROXY}" no_proxy="localhost,127.0.0.1,::1"
cd "$(dirname "$0")/.."
git checkout -q master
git pull -q --ff-only
echo "[eval] code at $(git log --oneline -1)"

py=""
for candidate in python3.12 /opt/homebrew/bin/python3.12 /opt/homebrew/opt/python@3.12/bin/python3.12; do
  if command -v "$candidate" >/dev/null 2>&1; then py="$(command -v "$candidate")"; break; fi
done
if [ -z "$py" ]; then
  echo "[eval] installing python@3.12 with Homebrew"; brew install -q python@3.12
  py=/opt/homebrew/opt/python@3.12/bin/python3.12
fi
echo "[eval] python: $("$py" --version)"
[ -x .venv-mac/bin/python ] || "$py" -m venv .venv-mac
.venv-mac/bin/pip install -q --upgrade pip
.venv-mac/bin/pip install -q -e .

curl -fsS --noproxy '*' http://127.0.0.1:11434/api/version >/dev/null \
  || { echo "[eval] starting ollama"; (ollama serve >/dev/null 2>&1 &); sleep 5; }
# A model already present is not pulled again: a registry update would change the weights the
# preregistration describes. Downloads through a desktop proxy sometimes drop ("unexpected EOF"); pull resumes.
for model in bge-m3 qwen3:8b; do
  if ollama show "$model" >/dev/null 2>&1; then continue; fi
  pulled=false
  for attempt in 1 2 3 4 5 6; do
    if ollama pull "$model" >/tmp/ollama-pull.log 2>&1; then pulled=true; break; fi
    echo "[eval] pull $model attempt $attempt failed: $(tail -c 200 /tmp/ollama-pull.log | tr '\r' '\n' | tail -1)"
    sleep 20
  done
  $pulled || { echo "[eval] could not pull $model"; exit 1; }
done
echo "[eval] models:"; ollama list | grep -E '^(bge-m3|qwen3:8b)' || true

PYTHONDONTWRITEBYTECODE=1 .venv-mac/bin/python preparation/s3/answerability/run_eval.py

git add preparation/s3/answerability/runs
git -c user.name="Lang Ju" -c user.email="langju123456@gmail.com" commit -q \
  -m "S3 answerability: hybrid vs hybrid + qwen3:8b gate on the held-out and frozen sets, from the Mac"
for attempt in 1 2 3; do git pull --rebase -q && git push -q && break; sleep 5; done
git log --oneline -1
