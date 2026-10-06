"""V2 constrained selection; V1 experiment source and predicates stay sealed.

The catalog enum constrains which existing claim the model can select. It does
not supply a completed report: exact coverage, uniqueness and evidence status
are still checked after the actual provider reply, by the unchanged semantic
validator and durable engine.
"""

from __future__ import annotations

import copy
from contextvars import ContextVar

from .contracts import DomainError, canonical, digest
from .grounded_contracts import schema
from .grounded_workflow import SKILLS, GroundedWorkflow

SELECTION_REVISION = "grounded-selection-v2"
_PHASES = ("plan", "context", "report", "summary")
_ACTIVE_CATALOG: ContextVar[tuple[int, dict] | None] = ContextVar(
    "forgeflow_grounded_selection_catalog", default=None
)


def schema_for_catalog(phase: str, catalog: dict) -> dict:
    """Constrain provider grammar to pre-authorized per-call claim identities."""
    result = schema(phase)
    if phase not in {"report", "summary"}:
        return result
    for field in ("observations", "concerns"):
        values = [item["id"] for item in catalog[field]]
        if len(values) != len(set(values)):
            raise DomainError("GROUNDING_CATALOG_CHANGED")
        definition = result["properties"][field]
        if values:
            definition["items"]["enum"] = values
        else:
            # An empty enum is invalid under JSON Schema's meta-schema. The
            # existing string item type plus maxItems=0 describes an empty list.
            definition["maxItems"] = 0
    return result


class GroundedSelectionWorkflow(GroundedWorkflow):
    """Same ledger, providers, source grants, journal and no-retry policy as V1."""

    def _schema(self, phase):
        if phase not in {"report", "summary"}:
            return schema(phase)
        active = _ACTIVE_CATALOG.get()
        if active is None or active[0] != id(self):
            raise DomainError("GROUNDING_SCHEMA_CONTEXT_REQUIRED")
        return schema_for_catalog(phase, active[1])

    def _material(self, active, *bundles):
        if not bundles:
            raise DomainError("GROUNDING_CONTEXT_REQUIRED")
        catalog = self.context.catalog(bundles[-1])
        token = _ACTIVE_CATALOG.set((id(self), copy.deepcopy(catalog)))
        try:
            material = super()._material(active, *bundles)
        finally:
            _ACTIVE_CATALOG.reset(token)
        material.update(
            analysis_revision=SELECTION_REVISION,
            prompt_revision=SELECTION_REVISION,
            schema_catalog=copy.deepcopy(catalog),
            claim_catalog_hash=digest(catalog),
            output_schema_hash=digest({phase: schema_for_catalog(phase, catalog) for phase in _PHASES}),
        )
        return material

    def _reauthorize(self, session, principal, scope, material):
        bindings = material.get("skill_bindings", [])
        if (
            not isinstance(bindings, list)
            or len(bindings) != 2
            or {binding.get("role") for binding in bindings} != set(SKILLS)
        ):
            raise DomainError("ANALYSIS_SKILL_CHANGED")
        active = self._activate()
        for binding in bindings:
            role = binding["role"]
            if binding.get("name") != SKILLS[role] or binding.get("sha256") != active[role].sha256:
                raise DomainError("ANALYSIS_SKILL_CHANGED")
        if (
            material.get("analysis_revision") != SELECTION_REVISION
            or material.get("prompt_revision") != SELECTION_REVISION
        ):
            raise DomainError("GROUNDING_REVISION_CHANGED")
        catalog = material.get("schema_catalog")
        if not isinstance(catalog, dict) or set(catalog) != {"observations", "concerns"}:
            raise DomainError("GROUNDING_CATALOG_CHANGED")
        if digest(catalog) != material.get("claim_catalog_hash"):
            raise DomainError("GROUNDING_CATALOG_CHANGED")
        try:
            expected = digest({phase: schema_for_catalog(phase, catalog) for phase in _PHASES})
        except (KeyError, TypeError) as error:
            raise DomainError("GROUNDING_CATALOG_CHANGED") from error
        if material.get("output_schema_hash") != expected:
            raise DomainError("GROUNDING_REVISION_CHANGED")
        # This callback independently derives the source catalog hash from
        # permission-checked current DB sources and the pinned frozen overlay.
        self.context.reauthorize_in_session(session, principal, scope, material)

    def _messages(self, phase, bundle, skill, task=None):
        messages = super()._messages(phase, bundle, skill, task)
        if phase in {"report", "summary"}:
            from .analysis_contracts import strict_json_object

            projection = strict_json_object(messages[1]["content"])
            # The reply selects claim IDs, not free citation strings. Each ID's
            # complete source binding remains in schema_catalog and the source
            # snapshot, reauthorized in the persistence transaction. Repeating
            # opaque refs on every row adds bytes without helping selection.
            projection["observations_columns"] = projection["observations_columns"][:5]
            projection["observations"] = [row[:5] for row in projection["observations"]]
            projection["concerns_columns"] = projection["concerns_columns"][:2]
            projection["concerns"] = [row[:2] for row in projection["concerns"]]
            messages[1]["content"] = canonical(projection)
            messages[0]["content"] += (
                " Status measures check evidence only. All SATISFIED+MEASURED means COMPLETE."
                " Pending human approval does not make analysis INCOMPLETE."
                " Pick enum ids exactly; no duplicates or omitted concerns."
            )
        return messages

    async def _call(self, p, scope, analysis_id, lease, active, phase, bundle, **kwargs):
        catalog = self.context.catalog(bundle)
        token = _ACTIVE_CATALOG.set((id(self), copy.deepcopy(catalog)))
        try:
            return await super()._call(p, scope, analysis_id, lease, active, phase, bundle, **kwargs)
        finally:
            _ACTIVE_CATALOG.reset(token)

    def input_bound(self, phase, bundle, skill, task=None):
        """Offline reproducibility helper using the engine's unchanged budget."""
        catalog = self.context.catalog(bundle)
        material = {
            "messages": self._messages(phase, bundle, skill, task),
            "schema": schema_for_catalog(phase, catalog),
        }
        return len(canonical(material).encode()) + 256 + 64 * len(material["messages"])
