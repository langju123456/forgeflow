"""Compose the new grounded revision over the unchanged durable S1/S2 engine."""

from __future__ import annotations

from pathlib import Path

from .analysis_provider import OllamaProvider
from .analysis_store import AnalysisStore
from .analysis_workflow import ROLE_TOOLS
from .contracts import DomainError
from .grounded_context import GroundedContextBuilder
from .grounded_store import GroundedStore
from .grounded_workflow import GroundedWorkflow
from .local import load_store
from .skills import SkillRegistry

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_SCENARIOS = frozenset({"M01", "M01-blocked", "M02", "M03", "M04"})
FROZEN_CASES = frozenset(f"M{number:02d}" for number in range(1, 13))


def resolve_scenario(case_id: str = "M01", scenario: str | None = None) -> str:
    selected = scenario or case_id
    if case_id not in FROZEN_CASES or selected not in FROZEN_CASES | {"M01-blocked"}:
        raise DomainError("FROZEN_SCENARIO_DENIED", status=403)
    if selected != case_id and (case_id, selected) != ("M01", "M01-blocked"):
        raise DomainError("FROZEN_SCENARIO_DENIED", status=403)
    return selected


class GroundedRuntimeFactory:
    """Share one physical provider and its persisted concurrency profile.

    Selection is trusted application configuration. This factory accepts only
    model-behavior scenarios. Fault cases use the separate TEST_DRIVER evaluator
    and are never appended to a model prompt.
    """

    def __init__(self, store=None, provider=None, *, freeze_call=None, owner=None):
        self.source = store if store is not None else load_store()
        self.persistence = AnalysisStore(self.source)
        self.persistence.create_schema()
        self.grounding = GroundedStore(self.persistence)
        self.grounding.create_schema()
        self.provider = provider if provider is not None else OllamaProvider()
        self.freeze_call = freeze_call
        self.owner = owner
        self.workflows: dict[str, GroundedWorkflow] = {}

    def __call__(self, scenario_id: str) -> GroundedWorkflow:
        case_id = "M01" if scenario_id == "M01-blocked" else scenario_id
        selected = resolve_scenario(case_id, scenario_id)
        if selected not in PUBLIC_SCENARIOS:
            raise DomainError("FROZEN_SCENARIO_DENIED", status=403)
        if selected not in self.workflows:
            options = {"owner": self.owner} if self.owner is not None else {}
            self.workflows[selected] = GroundedWorkflow(
                self.persistence,
                self.provider,
                GroundedContextBuilder(self.source, scenario_id=selected),
                SkillRegistry(ROOT / "skills", role_tools=ROLE_TOOLS),
                grounding_store=self.grounding,
                freeze_call=self.freeze_call,
                **options,
            )
        return self.workflows[selected]

    def reauthorize(self, session, principal, scope, material):
        """Resolve the persisted case, then recheck all sources in the transaction."""
        if type(material) is not dict or type(material.get("source_snapshot")) is not dict:
            raise DomainError("GROUNDING_CONTEXT_CHANGED")
        snapshot = material["source_snapshot"]
        snapshots = snapshot.get("bundles", [snapshot])
        if type(snapshots) is not list or not snapshots:
            raise DomainError("GROUNDING_CONTEXT_CHANGED")
        scenarios = set()
        for item in snapshots:
            if type(item) is not dict or type(item.get("grounding")) is not dict:
                raise DomainError("GROUNDING_CONTEXT_CHANGED")
            scenario_id = item["grounding"].get("scenario_id")
            if type(scenario_id) is not str or scenario_id not in PUBLIC_SCENARIOS:
                raise DomainError("FROZEN_SCENARIO_DENIED", status=403)
            scenarios.add(scenario_id)
        if len(scenarios) != 1:
            raise DomainError("GROUNDING_CONTEXT_CHANGED")
        self(next(iter(scenarios)))._reauthorize(session, principal, scope, material)


def build_runtime(store=None, provider=None, *, case_id="M01", scenario=None, freeze_call=None, owner=None):
    factory = GroundedRuntimeFactory(store, provider, freeze_call=freeze_call, owner=owner)
    return factory.source, factory(resolve_scenario(case_id, scenario))
