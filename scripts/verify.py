"""Run tests without echoing generated development connection credentials."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from forgeflow.local import database_url

parser = argparse.ArgumentParser()
parser.add_argument("--live", action="store_true")
parser.add_argument("--output", type=Path)
parser.add_argument("--tests", nargs="*")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
output = args.output.resolve() if args.output else root / ".runtime"
output.mkdir(parents=True, exist_ok=True)
environment = os.environ.copy()
if args.live:
    environment["FORGEFLOW_LIVE"] = "1"
    environment["FORGEFLOW_TEST_DATABASE_URL"] = database_url()
    config = json.loads((root / ".runtime" / "runtime.json").read_text())
    if config.get("scanner_image"):
        environment["FORGEFLOW_SCANNER_IMAGE"] = config["scanner_image"]
    if args.output:
        environment["FORGEFLOW_EVIDENCE_DIR"] = str(output)
command = [
    sys.executable,
    "-m",
    "pytest",
    *(args.tests or [str(root / "tests")]),
    "-q",
    "--tb=short",
    "--junitxml=" + str(output / "pytest.xml"),
]
result = subprocess.run(command, env=environment, cwd=root)
raise SystemExit(result.returncode)
