"""Verify the recorded S1 and S2 preparation snapshots without runtime access."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]


def check(path: Path) -> dict:
    manifest = json.loads(path.read_text())
    mismatches = []
    for entry in manifest["files"]:
        target = PROJECT / entry["path"]
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != entry["sha256"]:
            mismatches.append(entry["path"])
    return {
        "manifest": str(path.relative_to(PROJECT)),
        "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "recorded_files_checked": len(manifest["files"]),
        "mismatches": mismatches,
        "status": "UNCHANGED" if not mismatches else "DIFFERENT_FROM_RECORDED_SNAPSHOT",
    }


def main() -> None:
    results = [
        check(PROJECT / "deliveries/2026-10-05/manifest.json"),
        check(PROJECT / "deliveries/2026-10-05/s2-preparation-manifest.json"),
    ]
    print(
        json.dumps(
            {
                "results": results,
                "note": "Recorded inventories only; new manual runs may add files. Never rewrite old seals to hide drift.",
            },
            indent=2,
        )
    )
    raise SystemExit(any(item["mismatches"] for item in results))


if __name__ == "__main__":
    main()
