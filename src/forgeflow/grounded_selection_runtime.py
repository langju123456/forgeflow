"""V2 evidence selection; keep the first experiment's application readable."""

from .analysis_workflow import ROLE_TOOLS
from .contracts import DomainError
from .grounded_context import GroundedContextBuilder
from .grounded_runtime import PUBLIC_SCENARIOS, ROOT, GroundedRuntimeFactory
from .grounded_selection import SELECTION_REVISION, GroundedSelectionWorkflow
from .skills import SkillRegistry


class SelectionRuntimeFactory(GroundedRuntimeFactory):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.legacy = GroundedRuntimeFactory(self.source, self.provider)

    def __call__(self, scenario_id):
        if scenario_id not in PUBLIC_SCENARIOS:
            raise DomainError("FROZEN_SCENARIO_DENIED", status=403)
        if scenario_id not in self.workflows:
            options = {"owner": self.owner} if self.owner is not None else {}
            self.workflows[scenario_id] = GroundedSelectionWorkflow(
                self.persistence, self.provider, GroundedContextBuilder(self.source, scenario_id),
                SkillRegistry(ROOT / "skills", role_tools=ROLE_TOOLS), grounding_store=self.grounding,
                freeze_call=self.freeze_call, **options,
            )
        return self.workflows[scenario_id]

    def reauthorize(self, session, principal, scope, material):
        if "prompt_revision" not in material:
            return self.legacy.reauthorize(session, principal, scope, material)
        if material.get("prompt_revision") != SELECTION_REVISION:
            raise DomainError("GROUNDING_REVISION_CHANGED")
        return super().reauthorize(session, principal, scope, material)


def build_runtime(store=None, provider=None, *, scenario_id="M01", freeze_call=None):
    factory = SelectionRuntimeFactory(store, provider, freeze_call=freeze_call)
    return factory.source, factory(scenario_id)
