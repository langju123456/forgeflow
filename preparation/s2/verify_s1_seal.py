"""Read-only check of the sealed S1 inventory; no provider or runtime calls."""

import hashlib
import json
from pathlib import Path


def verify() -> dict:
    root = Path(__file__).resolve().parents[2]
    seal = root / "deliveries/2026-10-05/manifest.json"
    entries = json.loads(seal.read_text())["files"]
    mismatches = []
    for entry in entries:
        path = root / entry["path"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            mismatches.append(entry["path"])
    return {
        "seal": str(seal.relative_to(root)),
        "checked_entries": len(entries),
        "mismatches": mismatches,
        "status": "UNCHANGED" if not mismatches else "DIFFERENT_FROM_S1_SNAPSHOT",
        "note": "After authorized S2 source changes, differences describe history; do not rewrite the S1 seal.",
    }


if __name__ == "__main__":
    result = verify()
    print(json.dumps(result, indent=2))
    raise SystemExit(bool(result["mismatches"]))
