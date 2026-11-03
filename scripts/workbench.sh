#!/usr/bin/env bash
# One command: build the React workbench if needed, seed a local SQLite database, open http://127.0.0.1:8780/workbench/
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d .venv ]; then python3.12 -m venv .venv; fi
.venv/bin/python -m pip install -q -e . >/dev/null
if [ ! -f web/dist/index.html ] || [ -n "$(find web/src web/index.html -newer web/dist/index.html 2>/dev/null | head -1)" ]; then
  (cd web && npm ci --no-audit --no-fund >/dev/null && npm run build >/dev/null)
fi
( sleep 3; command -v open >/dev/null && open "http://127.0.0.1:8780/workbench/" ) &
exec .venv/bin/python -m forgeflow.workbench "$@"
