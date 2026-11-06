#!/usr/bin/env bash
# One command: build the React workbench if needed, seed a local SQLite database, open http://127.0.0.1:8780/workbench/
# The 年报问答 tab reads the public annual reports from ~/code/finreports (fetched and hash-checked here) and uses the
# local Ollama models bge-m3 and qwen3:8b when they are installed; without them it falls back to lexical search.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d .venv ]; then python3.12 -m venv .venv; fi
.venv/bin/python -m pip install -q -e '.[pdf]' >/dev/null
bash scripts/fetch_real_reports.sh "${FORGEFLOW_REPORTS:-$HOME/code/finreports}" || echo "annual reports not fetched; 年报问答 will list them as missing"
if [ ! -f web/dist/index.html ] || [ -n "$(find web/src web/index.html -newer web/dist/index.html 2>/dev/null | head -1)" ]; then
  (cd web && npm ci --no-audit --no-fund >/dev/null && npm run build >/dev/null)
fi
( sleep 3; command -v open >/dev/null && open "http://127.0.0.1:8780/workbench/#reports" ) &
exec .venv/bin/python -m forgeflow.workbench --reports "${FORGEFLOW_REPORTS:-$HOME/code/finreports}" "$@"
