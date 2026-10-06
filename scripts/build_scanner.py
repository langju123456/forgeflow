"""Build a task-owned image; print immutable ID. Never configures Docker globally."""

import hashlib
import json
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "scanner"
base = json.loads(
    subprocess.check_output(
        [
            "docker",
            "image",
            "inspect",
            "python:3.12-slim",
            "--format",
            "{{json .RepoDigests}}",
        ],
        text=True,
    )
)[0]
rules_hash = hashlib.sha256((root / "rules.yml").read_bytes()).hexdigest()
subprocess.run(
    [
        "docker",
        "build",
        "-f",
        str(root / "Dockerfile.scanner"),
        "--build-arg",
        "BASE_IMAGE=" + base,
        "--build-arg",
        "RULES_HASH=" + rules_hash,
        "-t",
        "forgeflow-scanner:semgrep-1.179.0",
        str(root),
    ],
    check=True,
)
subprocess.run(
    [
        "docker",
        "image",
        "inspect",
        "forgeflow-scanner:semgrep-1.179.0",
        "--format",
        "{{.Id}}",
    ],
    check=True,
)
