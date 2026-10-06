"""Structured policy controls authority independently from natural-language skills."""

import re

from forgeflow.contracts import DomainError, ExecutionSpec, Principal, authorize, digest

POLICY = {
    "version": "policy-v1",
    "network": "none",
    "profile": "checker-v1",
    "role_tools": {
        "hardware": ["knowledge.search", "memory.search", "sandbox.execute"],
        "pm": ["knowledge.search", "memory.search"],
        "supervisor": ["knowledge.search", "memory.search"],
    },
}
POLICY_HASH = digest(POLICY)


def execution_policy(principal: Principal, spec: ExecutionSpec, scanned_hash: str, scan_status: str) -> None:
    authorize(principal, spec.scope, {"hardware"})
    if spec.code_hash != scanned_hash or scan_status != "PASS":
        raise DomainError("SCAN_DENIED", "A successful scan of this exact code hash is required", 403)
    if not re.fullmatch(r"(?:[a-zA-Z0-9./:_-]+@)?sha256:[0-9a-f]{64}", spec.image):
        raise DomainError("IMAGE_DENIED", "An offline-resolved immutable image digest is required", 403)
    if spec.input_hash != digest(spec.input_manifest):
        raise DomainError("INPUT_HASH_MISMATCH", status=403)
