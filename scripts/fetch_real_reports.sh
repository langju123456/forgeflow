#!/bin/bash
# Download the four public annual reports listed in preparation/s4/real-reports/sources.json and verify their
# sha256. The PDFs stay out of git. Usage: bash scripts/fetch_real_reports.sh [target dir, default ~/code/finreports]
set -euo pipefail
cd "$(dirname "$0")/.."
TARGET="${1:-$HOME/code/finreports}"
mkdir -p "$TARGET"
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
python3 - "$TARGET" <<'PY' | while IFS=$'\t' read -r key url sha; do
import json, sys
for r in json.load(open("preparation/s4/real-reports/sources.json"))["reports"]:
    print(f"{r['key']}\t{r['url']}\t{r['sha256']}")
PY
  file="$TARGET/$key.pdf"
  if [ -f "$file" ] && [ "$(shasum -a 256 "$file" | cut -c1-64)" = "$sha" ]; then echo "ok (cached)  $key"; continue; fi
  for attempt in 1 2 3; do curl -fsSL --max-time 300 -A "$UA" -o "$file.part" "$url" && break; sleep 5; done
  got="$(shasum -a 256 "$file.part" | cut -c1-64)"
  if [ "$got" != "$sha" ]; then echo "HASH MISMATCH $key: $got (expected $sha)"; rm -f "$file.part"; exit 1; fi
  mv "$file.part" "$file"; echo "ok           $key"
done
