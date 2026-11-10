"""Cache keys and Redis key names.

A cached answer may be served only to a request that would have produced the same answer from the same evidence.
So the key covers everything that changes either:

* who asks (``subject``): retrieval filters by the caller's roles before ranking, so two callers can get
  different hits for the same words; a key without the subject would leak one caller's answer to another;
* what retrieval can return (``index_version``, from ReportDesk.index_version: every row hash plus the mode);
* how the answer is produced (answer model, cited_answer's prompt hash, gate on/off, top_k);
* the question, with surrounding whitespace and repeated spaces removed and nothing else (no lower-casing:
  "Q1" and "q1" are left to retrieval to treat as it does).
"""

from __future__ import annotations

import hashlib
import json
import re

PREFIX = "forgeflow:desk"
INDEX_VERSION = f"{PREFIX}:index_version"  # written by the retrieval server once its index is ready


def normalize_question(question: str) -> str:
    return re.sub(r"\s+", " ", question).strip()


def cache_key(*, subject: str, index_version: str, model: str, prompt_sha256: str, gate: bool, top_k: int,
              question: str) -> str:
    material = {"subject": subject, "index": index_version, "model": model, "prompt": prompt_sha256,
                "gate": bool(gate), "top_k": int(top_k), "question": normalize_question(question)}
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return digest[:32]


def cache(key: str) -> str:
    return f"{PREFIX}:cache:{key}"


def inflight(key: str) -> str:
    return f"{PREFIX}:inflight:{key}"


def job(job_id: str) -> str:
    return f"{PREFIX}:job:{job_id}"


def events(job_id: str) -> str:
    return f"{PREFIX}:events:{job_id}"
