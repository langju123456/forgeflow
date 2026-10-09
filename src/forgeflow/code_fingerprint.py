"""Fingerprint of the files that define the behaviour of a running service.

A service reports the fingerprint it started with; comparing it with the fingerprint of the code on disk shows
whether the process is stale. Tests, documents, deliveries, caches and runtime state are not covered, so editing
them never makes a service look stale.
"""

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIRECTORIES = ("src/forgeflow", "fixtures", "skills")
FILES = ("pyproject.toml", "requirements.lock")


def covered_files(root=ROOT):
    root = Path(root)
    found = []
    for directory in DIRECTORIES:
        base = root / directory
        if base.is_dir():
            found += [path for path in base.rglob("*") if path.is_file() and "__pycache__" not in path.parts
                      and not path.name.startswith(".") and path.suffix != ".pyc"]
    found += [root / name for name in FILES if (root / name).is_file()]
    return sorted(str(path.relative_to(root)) for path in found)


def code_fingerprint(root=ROOT):
    names = covered_files(root)
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode() + b"\0" + hashlib.sha256((Path(root) / name).read_bytes()).digest())
    return {"fingerprint": digest.hexdigest(), "files": len(names)}
