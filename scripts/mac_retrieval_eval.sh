#!/bin/bash
# Run the lexical / vector / hybrid comparison on a Mac with Ollama, then commit and push the runs.
# Meant to be sent through mac-bridge, from a clone of this repo under ~/code:
#   bash scripts/mac_retrieval_eval.sh
# Embeddings come from the local Ollama server (bge-m3); nothing is sent off the machine.
set -euo pipefail
export NO_PROXY="localhost,127.0.0.1,::1${NO_PROXY:+,$NO_PROXY}" no_proxy="localhost,127.0.0.1,::1"
cd "$(dirname "$0")/.."
MODEL=bge-m3

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

curl -fsS http://127.0.0.1:11434/api/version >/dev/null || { echo "[eval] starting ollama"; (ollama serve >/dev/null 2>&1 &); sleep 5; }
# Registry downloads through a desktop proxy sometimes drop mid-way ("unexpected EOF"); pull resumes.
pulled=false
for attempt in 1 2 3 4 5 6; do
  if ollama pull "$MODEL" >/tmp/ollama-pull.log 2>&1; then pulled=true; break; fi
  echo "[eval] pull attempt $attempt failed: $(tail -c 200 /tmp/ollama-pull.log | tr '\r' '\n' | tail -1)"; sleep 20
done
$pulled || { echo "[eval] could not pull $MODEL"; exit 1; }
echo "[eval] model: $(ollama list | grep "^$MODEL" || true)"

PYTHONDONTWRITEBYTECODE=1 .venv-mac/bin/python preparation/s2/evaluation/hybrid/run_modes.py --modes lexical vector hybrid

git add preparation/s2/evaluation/hybrid/runs
git -c user.name="Lang Ju" -c user.email="langju123456@gmail.com" commit -q -m "S2 hybrid: lexical, vector (bge-m3) and hybrid runs on the frozen set, from the Mac"
for attempt in 1 2 3; do git pull --rebase -q && git push -q && break; sleep 5; done
git log --oneline -1
