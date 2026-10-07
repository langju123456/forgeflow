"""Authorized synthetic S1 evidence exported to a bounded S2 local model.

The caller and recipient role must independently read every source. A run view
is deliberately projected only after Store's whole-run ACL succeeds. Server-side
bindings are not prompt content; they are rechecked inside publication's DB tx.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from . import contracts
from .analysis_contracts import ContextProposal, RoleReport, parse_output, strict_json_object
from .context import KnowledgeService, MemoryService
from .contracts import DomainError, ExecutionResult, Principal, Scope, authorize, canonical, digest
from .gate import RULE_HASH, RULES, evaluate
from .policy import POLICY, POLICY_HASH
from .store import Document, DocumentVersion, Event, Memory, MemorySource, Run, aware, filters, view
from .templates import template_hash

PROFILE_ID = "S2_SYNTHETIC_LOCAL"
CONTEXT_REVISION = "analysis-context-v2-mcp-witness"
ANALYSIS_POLICY = {
    "id": "analysis-policy-v1",
    "profile": PROFILE_ID,
    "delegate": {"supervisor": ["pm"], "pm": []},
    "role_tools": {
        role: [
            "model.generate",
            *sorted(set(POLICY["role_tools"][role]) & {"knowledge.search", "memory.search"}),
        ]
        for role in ("supervisor", "pm")
    },
    "synthetic_only": True,
    "external_writes": False,
    "sandbox": False,
    "approval": False,
}
ANALYSIS_POLICY_HASH = digest(ANALYSIS_POLICY)
ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_SCOPE = Scope(tenant_id="synthetic-lab", workspace_id="engineering", project_id="aurora")
SOP_CONTENT = (
    "Aurora EVT readiness synthetic SOP v1: boot_cycles >=100; boot_failures=0; "
    "BOM revision A3; CI smoke true; thermal_peak_c <=70. Unknown evidence never passes. "
    "This example is not hardware certification. Technical assessment and human stage "
    "approval are separate."
)
SOP_HASH = hashlib.sha256(SOP_CONTENT.encode()).hexdigest()


@dataclass(frozen=True)
class SourceGrant:
    id: str
    version: str
    source_hash: str


@dataclass(frozen=True)
class ExportGrant:
    """Trusted configuration. Models cannot create or broaden this grant."""

    scope: Scope = SYNTHETIC_SCOPE
    documents: tuple[SourceGrant, ...] = (SourceGrant("aurora-evt-sop", "v1", SOP_HASH),)
    max_classification: int = 0
    profile_id: str = PROFILE_ID
    destination: str = "ollama-local-qwen3:8b"
    purpose: str = "synthetic-aurora-readiness-analysis"

    @property
    def revision(self) -> str:
        return digest({**asdict(self), "scope": self.scope.model_dump(mode="json")})


@dataclass(frozen=True)
class SourceBinding:
    ref: str
    kind: str
    id: str
    version: str
    source_hash: str
    status: str
    classification: int
    witness_hash: str


@dataclass(frozen=True)
class ContextBundle:
    caller: Principal
    principal: Principal
    scope: Scope
    run_id: str
    role: str
    material_hash: str
    model_context: dict
    citations: dict[str, SourceBinding]
    context_hash: str
    grant_revision: str
    tool_receipts: tuple[dict, ...] = ()

    def to_material(self) -> dict:
        """Source snapshot only; no principal token or model transcript."""
        return {
            "caller": _principal_binding(self.caller),
            "scope": self.scope.model_dump(mode="json"),
            "run_id": self.run_id,
            "role": self.role,
            "material_hash": self.material_hash,
            "grant_revision": self.grant_revision,
            "context_revision": CONTEXT_REVISION,
            "sources": [asdict(binding) for binding in self.citations.values()],
        }


def _principal_binding(principal: Principal) -> dict:
    """Set iteration must not change a durable source binding on restart."""
    return {
        **principal.model_dump(mode="json"),
        "roles": sorted(principal.roles),
        "scopes": [
            scope.model_dump(mode="json") for scope in sorted(principal.scopes, key=lambda item: item.key())
        ],
    }


def _fresh(value, now: datetime) -> None:
    if value is not None:
        try:
            if aware(value) <= now:
                raise DomainError("EVIDENCE_EXPIRED")
        except (TypeError, ValueError) as error:
            raise DomainError("EVIDENCE_EXPIRED") from error


def _witness(source: dict) -> str:
    return digest(
        {
            key: source.get(key)
            for key in (
                "id",
                "citation_id",
                "version",
                "source_hash",
                "roles",
                "classification",
                "status",
                "valid_until",
                "source_event_ids",
                "run_id",
                "content",
                "human_confirmed",
            )
        }
    )


def _mcp_projection(scope: Scope, run_id: str, run: dict) -> str:
    """Recorded protocol activity is evidence, never a sandbox security proof."""
    try:
        result, snapshot = run["result_json"], run["snapshot_json"]
        receipt = result.get("mcp", {})
        if receipt.get("status") == "TEST_DRIVER":
            return "TEST_DRIVER"
        if (
            receipt.get("transport") != "stdio"
            or receipt.get("independent_server_process") is not True
            or receipt.get("sdk") != "mcp"
            or receipt.get("sdk_version") != "1.30.0"
            or receipt.get("protocol_version") != "2025-11-25"
            or receipt.get("tools") != ["sandbox.execute"]
            or receipt.get("binding", {}).get("scope") != scope.model_dump(mode="json")
            or receipt.get("binding", {}).get("run_id") != run_id
            or receipt.get("binding", {}).get("roles") != ["hardware"]
        ):
            return "UNKNOWN"
        messages = receipt.get("messages")
        if not isinstance(messages, list) or not 7 <= len(messages) <= 100:
            return "UNKNOWN"
        requests, responses, initialized = {}, {}, False
        for entry in messages:
            message = entry.get("message")
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                return "UNKNOWN"
            encoded = canonical(message).encode()
            if entry.get("sha256") != hashlib.sha256(encoded).hexdigest() or entry.get("bytes") != len(
                encoded
            ):
                return "UNKNOWN"
            direction, method = entry.get("direction"), message.get("method")
            if direction == "client_to_server" and method == "notifications/initialized":
                if (
                    any(field in message for field in ("id", "result", "error"))
                    or initialized
                    or "initialize" not in requests
                    or requests["initialize"]["id"] not in responses
                ):
                    return "UNKNOWN"
                initialized = True
            elif direction == "client_to_server" and method in {"initialize", "tools/list", "tools/call"}:
                if (
                    any(field in message for field in ("result", "error"))
                    or method in requests
                    or (method != "initialize" and not initialized)
                ):
                    return "UNKNOWN"
                if type(message.get("id")) not in {int, str}:
                    return "UNKNOWN"
                requests[method] = message
            elif direction == "server_to_client" and "result" in message:
                identifier = message.get("id")
                sent_ids = {request["id"] for request in requests.values()}
                if (
                    type(identifier) not in {int, str}
                    or identifier not in sent_ids
                    or identifier in responses
                    or any(field in message for field in ("method", "params", "error"))
                ):
                    return "UNKNOWN"
                responses[identifier] = message["result"]
            else:
                return "UNKNOWN"
        if set(requests) != {"initialize", "tools/list", "tools/call"} or not initialized:
            return "UNKNOWN"
        if len({message["id"] for message in requests.values()}) != 3:
            return "UNKNOWN"
        if set(responses) != {message["id"] for message in requests.values()}:
            return "UNKNOWN"
        handshake = responses[requests["initialize"]["id"]]
        tools = responses[requests["tools/list"]["id"]]["tools"]
        tool_result = responses[requests["tools/call"]["id"]]
        if (
            handshake.get("protocolVersion") != receipt["protocol_version"]
            or handshake.get("serverInfo")
            != {key: value for key, value in receipt.get("server_info", {}).items() if value is not None}
            or requests["initialize"].get("params", {}).get("protocolVersion") != receipt["protocol_version"]
            or not isinstance(tools, list)
            or [tool.get("name") for tool in tools] != ["sandbox.execute"]
        ):
            return "UNKNOWN"
        template = "timeout" if run["request_json"]["fixture_id"] == "aurora-timeout" else "evt-check"
        if requests["tools/call"].get("params") != {
            "name": "sandbox.execute",
            "arguments": {
                "template_id": template,
                "input_manifest_id": snapshot["input_hash"],
                "resource_profile": "checker-v1",
            },
        }:
            return "UNKNOWN"
        recorded = ExecutionResult.model_validate(tool_result["structuredContent"])
        persisted = ExecutionResult.model_validate(
            {key: result[key] for key in ExecutionResult.model_fields if key in result}
        )
        if recorded != persisted or tool_result.get("isError") is not (recorded.status != "SUCCEEDED"):
            return "UNKNOWN"
        return "RECORDED"
    except (KeyError, TypeError, ValueError, AttributeError):
        return "UNKNOWN"


class _IntersectionStore:
    """Repository view filtered by caller AND recipient before text or ranking."""

    def __init__(self, builder, bundle):
        self.builder, self.bundle = builder, bundle

    def document_rows(self, principal, scope):
        return self.builder._intersection_rows(self.bundle, "document")

    def memory_rows(self, principal, scope):
        return self.builder._intersection_rows(self.bundle, "memory")

    def cite(self, principal, scope, kind, identifier):
        return self.builder._intersection_citation(self.bundle, kind, identifier)


class AnalysisContextBuilder:
    def __init__(self, store, grant: ExportGrant | None = None, fixture_root: Path | None = None):
        self.store = store
        self.grant = grant or ExportGrant()
        self.fixture_root = fixture_root or ROOT / "fixtures"
        self.knowledge, self.memory = KnowledgeService(store), MemoryService(store)

    def _reference(self, scope, run_id, kind, identifier, source_hash):
        # Stable across restart for idempotent analysis. These opaque identifiers
        # grant no access: every use resolves the full server-side scoped binding.
        return "src_" + digest([scope.model_dump(mode="json"), run_id, kind, identifier, source_hash])[:32]

    def _intersection_citation(self, bundle, kind, identifier):
        with self.store._tx() as session:
            if kind == "document":
                document_id = session.scalar(
                    select(DocumentVersion.document_id).where(
                        *filters(DocumentVersion, bundle.scope),
                        DocumentVersion.id == identifier,
                    )
                )
                metadata = self.store._get(session, Document, bundle.scope, document_id, lock=True)
                roles, classification = metadata.roles, metadata.classification
            else:
                metadata = session.execute(
                    select(Memory.roles, Memory.classification)
                    .where(
                        *filters(Memory, bundle.scope),
                        Memory.id == identifier,
                    )
                    .with_for_update()
                ).first()
                if metadata is None:
                    raise DomainError("NOT_FOUND", status=404)
                roles, classification = metadata
            # Neither model role's extra access nor caller's admin access can
            # load source text until both independent metadata ACLs pass.
            for observer in (bundle.caller, bundle.principal):
                authorize(observer, bundle.scope, set(roles), classification)
            source = self._citation(bundle.principal, bundle.scope, kind, identifier, session)
            self._citation(bundle.caller, bundle.scope, kind, identifier, session)
            return source

    def _intersection_rows(self, bundle, kind):
        scope, principal, caller = bundle.scope, bundle.principal, bundle.caller
        permitted = {(item.id, item.version, item.source_hash) for item in self.grant.documents}
        rows = []
        with self.store._tx() as session:
            # Document and Memory rows here contain metadata only. Contents
            # live in DocumentVersion and Memory, so select Memory columns only.
            if kind == "document":
                candidates = session.scalars(
                    select(Document)
                    .where(
                        *filters(Document, scope),
                        Document.deleted.is_(False),
                        Document.classification <= min(principal.clearance, caller.clearance),
                    )
                    .with_for_update()
                ).all()
                identifiers = []
                for document in candidates:
                    if not (principal.roles & set(document.roles) and caller.roles & set(document.roles)):
                        continue
                    version = session.execute(
                        select(DocumentVersion.id, DocumentVersion.source_hash).where(
                            *filters(DocumentVersion, scope),
                            DocumentVersion.document_id == document.id,
                            DocumentVersion.version == document.current_version,
                        )
                    ).first()
                    if version and (document.id, document.current_version, version[1]) in permitted:
                        identifiers.append(version[0])
            else:
                candidates = session.execute(
                    select(Memory.id, Memory.roles, Memory.classification)
                    .where(
                        *filters(Memory, scope),
                        Memory.run_id == bundle.run_id,
                        Memory.classification <= min(principal.clearance, caller.clearance),
                        Memory.status.in_(["CANDIDATE", "CONFIRMED"]),
                    )
                    .with_for_update()
                ).all()
                completed_id = session.scalar(
                    select(Event.id).where(
                        *filters(Event, scope),
                        Event.run_id == bundle.run_id,
                        Event.kind == "CHECK_COMPLETED",
                    )
                )
                identifiers = []
                for identifier, roles, classification in candidates:
                    if not (
                        principal.permits(scope, set(roles), classification)
                        and caller.permits(scope, set(roles), classification)
                    ):
                        continue
                    source_ids = list(
                        session.scalars(
                            select(MemorySource.event_id).where(
                                *filters(MemorySource, scope),
                                MemorySource.memory_id == identifier,
                            )
                        )
                    )
                    if source_ids == [completed_id]:
                        identifiers.append(identifier)
            for identifier in identifiers:
                source = self._citation(principal, scope, kind, identifier, session)
                self._citation(caller, scope, kind, identifier, session)
                if kind == "memory":
                    source["subject_key"] = source["subject"]
                    source["supersedes"] = None
                rows.append(source)
        return rows

    def role_principal(self, caller: Principal, scope: Scope, role: str) -> Principal:
        if caller.kind != "user" or role not in ANALYSIS_POLICY["role_tools"]:
            raise DomainError("ANALYSIS_ROLE_DENIED", status=403)
        authorize(caller, scope)
        if scope != self.grant.scope or self.grant.profile_id != PROFILE_ID:
            raise DomainError("EXPORT_DENIED", status=403)
        # A trusted profile mints only one role, never caller's admin/hardware grants.
        return Principal(
            subject_id=f"analysis:{caller.subject_id}:{role}",
            kind="agent",
            roles=frozenset({role}),
            scopes=(scope,),
            clearance=min(caller.clearance, self.grant.max_classification),
        )

    def _run(self, principal, scope, run_id, session=None):
        if session is None:
            return self.store.get_run(principal, scope, run_id)
        row = self.store._get(session, Run, scope, run_id)
        return self.store._run_view(session, scope, row, principal)

    def _events(self, principal, scope, run_id, session=None):
        if session is None:
            return self.store.events(principal, scope, run_id)
        self.store._run_acl(session, principal, scope, self.store._get(session, Run, scope, run_id))
        return [
            view(row)
            for row in session.scalars(
                select(Event)
                .where(
                    *filters(Event, scope),
                    Event.run_id == run_id,
                    Event.classification <= principal.clearance,
                )
                .order_by(Event.sequence)
            )
        ]

    def _citation(self, principal, scope, kind, identifier, session=None):
        if session is None:
            return self.store.cite(principal, scope, kind, identifier)
        if kind == "document":
            document_id = session.scalar(
                select(DocumentVersion.document_id).where(
                    *filters(DocumentVersion, scope),
                    DocumentVersion.id == identifier,
                )
            )
            document = self.store._get(session, Document, scope, document_id)
            authorize(principal, scope, set(document.roles), document.classification)
            row = self.store._get(session, DocumentVersion, scope, identifier)
            status = (
                "DELETED"
                if document.deleted
                else ("SUPERSEDED" if document.current_version != row.version else "CURRENT")
            )
            return {
                **view(row),
                "id": document.id,
                "citation_id": row.id,
                "roles": document.roles,
                "classification": document.classification,
                "status": status,
            }
        if kind != "memory":
            raise DomainError("NOT_FOUND", status=404)
        metadata = session.execute(
            select(Memory.run_id, Memory.classification, Memory.roles).where(
                *filters(Memory, scope),
                Memory.id == identifier,
            )
        ).first()
        if metadata is None:
            raise DomainError("NOT_FOUND", status=404)
        authorize(principal, scope, set(metadata[2]), metadata[1])
        self.store._run_acl(session, principal, scope, self.store._get(session, Run, scope, metadata[0]))
        source_ids = [
            item.event_id
            for item in session.scalars(
                select(MemorySource).where(
                    *filters(MemorySource, scope),
                    MemorySource.memory_id == identifier,
                )
            )
        ]
        for event_id in source_ids:
            event = session.execute(
                select(Event.run_id, Event.classification).where(
                    *filters(Event, scope),
                    Event.id == event_id,
                )
            ).first()
            if event is None:
                raise DomainError("NOT_FOUND", status=404)
            authorize(principal, scope, classification=event[1])
            self.store._run_acl(session, principal, scope, self.store._get(session, Run, scope, event[0]))
        row = self.store._get(session, Memory, scope, identifier)
        return {**view(row), "version": "1", "citation_id": row.id, "source_event_ids": source_ids}

    def _document_exact(self, principal, scope, reference, session=None):
        if session is None:
            with self.store._tx() as own_session:
                return self._document_exact(principal, scope, reference, own_session)
        identifier = session.scalar(
            select(DocumentVersion.id).where(
                *filters(DocumentVersion, scope),
                DocumentVersion.document_id == reference["id"],
                DocumentVersion.version == reference["version"],
            )
        )
        if identifier is None:
            raise DomainError("SOURCE_CHANGED")
        source = self._citation(principal, scope, "document", identifier, session)
        if source["version"] != reference["version"] or source["source_hash"] != reference["source_hash"]:
            raise DomainError("SOURCE_CHANGED")
        return source

    def _export_source(self, source, kind, run_id, completed_id, now):
        classification = source.get("classification")
        if type(classification) is not int or not 0 <= classification <= self.grant.max_classification:
            raise DomainError("EXPORT_DENIED", status=403)
        _fresh(source.get("valid_until"), now)
        status = source.get("status", "CURRENT")
        if status not in ({"CURRENT"} if kind == "document" else {"CANDIDATE", "CONFIRMED"}):
            raise DomainError("SOURCE_CHANGED")
        if hashlib.sha256(source["content"].encode()).hexdigest() != source["source_hash"]:
            raise DomainError("SOURCE_CHANGED")
        if kind == "document":
            permitted = {(item.id, item.version, item.source_hash) for item in self.grant.documents}
            if (source["id"], source["version"], source["source_hash"]) not in permitted:
                raise DomainError("EXPORT_DENIED", status=403)
        elif source.get("run_id") != run_id or source.get("source_event_ids") != [completed_id]:
            # No arbitrary memory can acquire synthetic origin from its prose.
            raise DomainError("EXPORT_DENIED", status=403)

    def _checked(self, principal, scope, run_id, session=None):
        run = self._run(principal, scope, run_id, session)
        events = self._events(principal, scope, run_id, session)
        previous, sequence = "0" * 64, 1
        for item in events:
            data = {
                "scope": scope.model_dump(mode="json"),
                "run_id": run_id,
                **{
                    key: item[key]
                    for key in (
                        "step_id",
                        "sequence",
                        "kind",
                        "actor",
                        "payload_hash",
                        "prev_hash",
                        "classification",
                        "created_at",
                    )
                },
            }
            if (
                item["sequence"] != sequence
                or item["prev_hash"] != previous
                or digest(item["payload"]) != item["payload_hash"]
                or digest(data) != item["event_hash"]
            ):
                raise DomainError("INVALID_SOURCE_AUDIT")
            previous, sequence = item["event_hash"], sequence + 1
        completed = [item for item in events if item["kind"] == "CHECK_COMPLETED"]
        if len(completed) != 1 or not run.get("result_json") or not run.get("snapshot_json"):
            raise DomainError("CHECK_NOT_COMPLETED")
        result, assessment, snapshot = run["result_json"], run["assessment_json"], run["snapshot_json"]
        if not assessment or result.get("synthetic") is not True or run.get("cancel_requested_at"):
            raise DomainError("SYNTHETIC_SOURCE_REQUIRED")
        request = run["request_json"]
        fixture_id = request.get("fixture_id")
        if fixture_id not in {"aurora-pass", "aurora-blocked", "aurora-unknown", "aurora-timeout"}:
            raise DomainError("SYNTHETIC_SOURCE_REQUIRED")
        manifest = strict_json_object((self.fixture_root / f"{fixture_id}.json").read_text())
        template = "timeout" if fixture_id == "aurora-timeout" else "evt-check"
        if (
            manifest.get("synthetic") is not True
            or manifest.get("version") != "v1"
            or manifest.get("source_ref") != "fixture:aurora-evt-synthetic-v1"
            or request.get("fixture_version") != "v1"
            or snapshot.get("fixture_version") != "v1"
            or snapshot.get("input_hash") != digest(manifest)
            or snapshot.get("input_manifest_hash") != digest(manifest)
            or snapshot.get("rule_hash") != RULE_HASH
            or snapshot.get("policy_hash") != POLICY_HASH
            or snapshot.get("code_hash") != template_hash(template)
            or run["snapshot_hash"] != digest(snapshot)
            or completed[0]["payload"].get("result_hash") != digest(result)
            or completed[0]["payload"].get("assessment_hash") != digest(assessment)
            or digest(evaluate(manifest, result, now=contracts.utcnow())) != digest(assessment)
        ):
            raise DomainError("SOURCE_CHANGED")
        now = self.store._now(session) if session is not None else contracts.utcnow()
        _fresh(snapshot.get("valid_until"), now)
        for metric in manifest.get("metrics", {}).values():
            _fresh(metric.get("valid_until"), now)
        if not snapshot.get("documents"):
            raise DomainError("SYNTHETIC_SOURCE_REQUIRED")
        for reference in snapshot["documents"]:
            source = self._document_exact(principal, scope, reference, session)
            self._export_source(source, "document", run_id, completed[0]["id"], now)
        material = digest(
            {
                "result": result,
                "assessment": assessment,
                "snapshot": snapshot,
                "revision": run["revision"],
                "project": run["project"],
                "completed_event": completed[0]["event_hash"],
                "fixture": digest(manifest),
                "rules": RULE_HASH,
                "policy": ANALYSIS_POLICY_HASH,
                "context_revision": CONTEXT_REVISION,
            }
        )
        return run, manifest, completed[0], material, now

    def build(self, caller: Principal, scope: Scope, run_id: str, role: str = "supervisor") -> ContextBundle:
        principal = self.role_principal(caller, scope, role)
        _, _, _, caller_hash, _ = self._checked(caller, scope, run_id)
        run, manifest, completed, material_hash, now = self._checked(principal, scope, run_id)
        if caller_hash != material_hash:
            raise DomainError("SOURCE_CHANGED")
        snapshot = run["snapshot_json"]
        ref = self._reference(scope, run_id, "run", run_id, material_hash)
        binding = SourceBinding(
            ref,
            "run",
            run_id,
            completed["id"],
            material_hash,
            "CHECK_COMPLETED",
            snapshot.get("classification", 0),
            completed["event_hash"],
        )
        assessment, result = run["assessment_json"], run["result_json"]
        content = {
            "ref": ref,
            "kind": "hardware_check",
            "synthetic": True,
            "gate": assessment["status"],
            "checker": result["status"],
            "cleanup": result["cleanup"],
            "stage": run["project"]["current_stage"],
            "transition": assessment["stage_transition"],
            "checks": {item["rule"]: item["status"] for item in assessment["checks"]},
            "metrics": {
                key: value.get("value")
                for key, value in manifest.get("metrics", {}).items()
                if key in RULES["required"]
            },
            "evidence_status": {item["id"]: item["status"] for item in assessment.get("evidence", [])},
            "scan": result.get("scan", {}).get("status", "UNKNOWN"),
            "mcp": _mcp_projection(scope, run_id, run),
        }
        context = {
            "notice": "SYNTHETIC. Gate facts are immutable. No action or approval authority.",
            "sources": [content],
        }
        bundle = ContextBundle(
            caller,
            principal,
            scope,
            run_id,
            role,
            material_hash,
            context,
            {ref: binding},
            digest(context),
            self.grant.revision,
        )
        # An initial SOP citation is grounded in exactly the run's source version.
        for reference in snapshot["documents"]:
            source = self._document_exact(principal, scope, reference)
            self._document_exact(caller, scope, reference)
            self._export_source(source, "document", run_id, completed["id"], now)
            bundle = self._add(bundle, "document", source)
        self.revalidate(bundle)
        return bundle

    def _add(self, bundle, kind, source):
        if any(item.kind == kind and item.id == source["citation_id"] for item in bundle.citations.values()):
            return bundle
        ref = self._reference(bundle.scope, bundle.run_id, kind, source["citation_id"], source["source_hash"])
        binding = SourceBinding(
            ref,
            kind,
            source["citation_id"],
            source.get("version", "1"),
            source["source_hash"],
            source.get("status", "CURRENT"),
            source["classification"],
            _witness(source),
        )
        context = copy.deepcopy(bundle.model_context)
        context["sources"].append(
            {
                "ref": ref,
                "kind": kind,
                "version": binding.version,
                "status": binding.status,
                "text": source["content"][: 700 if kind == "document" else 400],
            }
        )
        return replace(
            bundle,
            model_context=context,
            citations={**bundle.citations, ref: binding},
            context_hash=digest(context),
        )

    def search(self, bundle: ContextBundle, proposal: ContextProposal) -> ContextBundle:
        self.revalidate(bundle)
        proposal = ContextProposal.model_validate(proposal.model_dump())
        _, _, completed, _, now = self._checked(bundle.principal, bundle.scope, bundle.run_id)
        repository = _IntersectionStore(self, bundle)
        for request in proposal.requests:
            if request.tool not in ANALYSIS_POLICY["role_tools"][bundle.role]:
                raise DomainError("ANALYSIS_TOOL_DENIED", status=403)
            kind = "document" if request.tool == "knowledge.search" else "memory"
            service = KnowledgeService(repository) if kind == "document" else MemoryService(repository)
            hits = service.search(bundle.principal, bundle.scope, request.query, request.top_k)
            # Only metadata is consulted to find exact immutable citation IDs.
            rows = (
                repository.document_rows(bundle.principal, bundle.scope)
                if kind == "document"
                else repository.memory_rows(bundle.principal, bundle.scope)
            )
            result_refs = []
            for hit in hits:
                if hit.status not in {"CURRENT", "CANDIDATE", "CONFIRMED"}:
                    raise DomainError("EVIDENCE_UNUSABLE")
                candidates = [
                    row
                    for row in rows
                    if row["id"] == hit.id
                    and row["version"] == hit.version
                    and row["source_hash"] == hit.source_hash
                ]
                if len(candidates) != 1:
                    raise DomainError("SOURCE_CHANGED")
                identifier = candidates[0]["citation_id"]
                source = self._citation(bundle.principal, bundle.scope, kind, identifier)
                self._citation(bundle.caller, bundle.scope, kind, identifier)
                self._export_source(source, kind, bundle.run_id, completed["id"], now)
                if source["source_hash"] != hit.source_hash or source.get("version", "1") != hit.version:
                    raise DomainError("SOURCE_CHANGED")
                bundle = self._add(bundle, kind, source)
                result_refs.append(
                    self._reference(
                        bundle.scope, bundle.run_id, kind, source["citation_id"], source["source_hash"]
                    )
                )
            receipt = {
                "tool": request.tool,
                "transport": "IN_PROCESS",
                "role": bundle.role,
                "query_hash": digest(request.query),
                "top_k": request.top_k,
                "result_count": len(result_refs),
                "source_refs": result_refs,
            }
            bundle = replace(bundle, tool_receipts=(*bundle.tool_receipts, receipt))
        self.revalidate(bundle)
        return bundle

    def snapshot(self, bundle: ContextBundle) -> dict:
        self.revalidate(bundle)
        return bundle.to_material()

    def combined_snapshot(self, *bundles: ContextBundle) -> dict:
        return {"bundles": [self.snapshot(bundle) for bundle in bundles]}

    def _revalidate_snapshot(self, snapshot, caller, scope, session=None, recipient_role=None):
        if snapshot.get("context_revision") != CONTEXT_REVISION:
            raise DomainError("ANALYSIS_CONTEXT_CHANGED")
        if (
            snapshot["scope"] != scope.model_dump(mode="json")
            or snapshot["caller"] != _principal_binding(caller)
            or snapshot["grant_revision"] != self.grant.revision
        ):
            raise DomainError("SOURCE_CHANGED")
        principal = self.role_principal(caller, scope, recipient_role or snapshot["role"])
        for observer in (caller, principal):
            checked_run, _, completed, material_hash, now = self._checked(
                observer, scope, snapshot["run_id"], session
            )
            if material_hash != snapshot["material_hash"]:
                raise DomainError("SOURCE_CHANGED")
            for source in snapshot["sources"]:
                if source["kind"] == "run":
                    if (
                        source["id"] != snapshot["run_id"]
                        or source["source_hash"] != material_hash
                        or source["version"] != completed["id"]
                        or source["witness_hash"] != completed["event_hash"]
                        or source["status"] != "CHECK_COMPLETED"
                        or source["classification"] != checked_run["snapshot_json"].get("classification", 0)
                    ):
                        raise DomainError("SOURCE_CHANGED")
                else:
                    citation = self._citation(observer, scope, source["kind"], source["id"], session)
                    self._export_source(citation, source["kind"], snapshot["run_id"], completed["id"], now)
                    if (
                        _witness(citation) != source["witness_hash"]
                        or citation.get("version", "1") != source["version"]
                        or citation["source_hash"] != source["source_hash"]
                        or citation.get("status", "CURRENT") != source["status"]
                        or citation["classification"] != source["classification"]
                    ):
                        raise DomainError("SOURCE_CHANGED")
                if source["ref"] != self._reference(
                    scope, snapshot["run_id"], source["kind"], source["id"], source["source_hash"]
                ):
                    raise DomainError("SOURCE_CHANGED")

    def revalidate(self, bundle: ContextBundle, recipient_role: str | None = None) -> None:
        if bundle.context_hash != digest(bundle.model_context):
            raise DomainError("SOURCE_CHANGED")
        self._revalidate_snapshot(
            bundle.to_material(), bundle.caller, bundle.scope, recipient_role=recipient_role
        )

    def reauthorize_in_session(self, session, principal: Principal, scope: Scope, material: dict) -> None:
        """Synchronous DB transaction callback; never starts another Store tx."""
        if (
            material.get("policy_revision") != ANALYSIS_POLICY_HASH
            or material.get("profile_id") != PROFILE_ID
        ):
            raise DomainError("ANALYSIS_POLICY_CHANGED")
        source_snapshot = material["source_snapshot"]
        snapshots = source_snapshot.get("bundles", [source_snapshot])
        if not snapshots or len(snapshots) > 4:
            raise DomainError("SOURCE_CHANGED")
        for snapshot in snapshots:
            self._revalidate_snapshot(snapshot, principal, scope, session)

    def validate_output(self, bundle, phase, raw, report_refs=()):
        self.revalidate(bundle)
        parsed = parse_output(phase, raw)

        def check(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "source_refs" and any(ref not in bundle.citations for ref in item):
                        raise DomainError("INVALID_CITATION", status=422)
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)

        data = parsed.model_dump(mode="json")
        check(data)
        if phase == "summary" and data["report_refs"] != list(report_refs):
            raise DomainError("INVALID_REPORT_REFERENCE", status=422)
        if phase == "report" and not any(
            data[key] for key in ("facts", "risks", "unknowns", "recommendations")
        ):
            raise DomainError("EMPTY_ROLE_REPORT", status=422)
        return parsed

    def share_report(self, pm_bundle, supervisor_bundle, report: RoleReport, report_ref="report_pm"):
        """A PM citation does not automatically grant Supervisor access."""
        if pm_bundle.role != "pm" or supervisor_bundle.role != "supervisor":
            raise DomainError("ANALYSIS_ROLE_DENIED", status=403)
        if (
            pm_bundle.scope != supervisor_bundle.scope
            or pm_bundle.run_id != supervisor_bundle.run_id
            or pm_bundle.caller != supervisor_bundle.caller
        ):
            raise DomainError("NOT_FOUND", status=404)
        report = self.validate_output(pm_bundle, "report", report.model_dump(mode="json"))
        self.revalidate(pm_bundle, recipient_role="supervisor")
        self.revalidate(supervisor_bundle)
        context = copy.deepcopy(supervisor_bundle.model_context)
        context["reports"] = [{"ref": report_ref, "role": "pm", **report.model_dump(mode="json")}]
        # Preserve PM's opaque identities; all attached sources were reauthorized.
        citations = {**supervisor_bundle.citations, **pm_bundle.citations}
        known = {item["ref"] for item in context["sources"]}
        context["sources"].extend(
            copy.deepcopy(item) for item in pm_bundle.model_context["sources"] if item["ref"] not in known
        )
        bundle = replace(
            supervisor_bundle, model_context=context, citations=citations, context_hash=digest(context)
        )
        self.revalidate(bundle)
        return bundle
