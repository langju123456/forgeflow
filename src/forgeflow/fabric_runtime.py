"""Task-local test identities and an isolated project; no credential discovery."""

import json
import secrets

from .contracts import Principal
from .evt_store import EVTBoundStore
from .fabric_contract import FABRIC_SCOPE
from .fixture_family import RENEWED_VALID_UNTIL
from .local import RUNTIME, database_url


class FabricStore(EVTBoundStore):
    def _prepare(self, session, principal, scope, run, kind, parameters, project):
        # This project uses the composite Fabric gate, never hardware-only actions.
        if scope != FABRIC_SCOPE:
            return super()._prepare(session, principal, scope, run, kind, parameters, project)
        return None


def load_fabric_store():
    source = FabricStore(database_url())
    source.create_schema()
    return source


def load_fabric_identities():
    rows = json.loads((RUNTIME / "fabric-identities.json").read_text())
    return {row["token"]: Principal.model_validate(row["principal"]) for row in rows}


def initialize_fabric():
    RUNTIME.mkdir(exist_ok=True)
    path = RUNTIME / "fabric-identities.json"
    if not path.exists():
        definitions = [
            ("fabric-test-owner", "user", {"admin", "submitter", "supervisor", "hardware", "pm"}),
            ("fabric-test-reviewer", "user", {"reviewer"}),
            ("fabric-test-executor", "service", {"fabric_executor", "memory_writer", "hardware"}),
        ]
        rows = [{"token": secrets.token_urlsafe(32), "principal": Principal(
            subject_id=name, kind=kind, roles=frozenset(roles), scopes=(FABRIC_SCOPE,),
        ).model_dump(mode="json")} for name, kind, roles in definitions]
        path.write_text(json.dumps(rows, indent=2) + "\n")
        path.chmod(0o600)
    source = load_fabric_store()
    owner = next(p for p in load_fabric_identities().values() if "admin" in p.roles)
    source.ensure_project(FABRIC_SCOPE)
    source.put_document(owner, FABRIC_SCOPE, "aurora-evt-sop", "v1",
        "Aurora EVT readiness synthetic SOP v1: boot_cycles >=100; boot_failures=0; "
        "BOM revision A3; CI smoke true; thermal_peak_c <=70. Unknown evidence never passes. "
        "Local TEST_STAGE is a workflow simulation, not EVT certification or release authorization.",
        roles={"admin", "submitter", "supervisor", "hardware", "pm", "reviewer", "worker",
               "fabric_executor", "memory_writer"}, classification=0,
        valid_until=RENEWED_VALID_UNTIL)
    return {"scope": FABRIC_SCOPE.model_dump(), "identities": "TEST_DRIVER",
            "tokens_file": str(path), "external_writes": 0}
