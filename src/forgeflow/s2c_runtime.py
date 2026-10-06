"""Fixed local S2C composition; no provider or authority supplied by a prompt."""

from .analysis_provider import OllamaProvider
from .analysis_workflow import ROLE_TOOLS
from .contracts import DomainError, RunRequest
from .grounded_runtime import PUBLIC_SCENARIOS, ROOT
from .grounded_selection_runtime import SelectionRuntimeFactory
from .local import SCOPE, load_store, load_workflow
from .s2c_checks import S2CCheckWorkflow, S2CInputProvider, S2CRequest
from .s2c_context import S2CContextBuilder
from .s2c_contracts import REVISION
from .s2c_store import S2CAnalysisStore, S2CStore
from .s2c_workflow import S2CWorkflow
from .skills import SkillRegistry

FIXTURES = {"M01": "aurora-pass", "M01-blocked": "aurora-blocked", "M02": "aurora-unknown", "M04": "aurora-pass"}


class S2CRuntimeFactory:
    def __init__(self, store=None, provider=None, *, freeze_call=None, owner=None):
        self.source = store if store is not None else load_store()
        self.provider = provider if provider is not None else OllamaProvider()
        self.persistence = S2CAnalysisStore(self.source)
        self.persistence.create_schema()
        self.grounding = S2CStore(self.persistence)
        self.grounding.create_schema()
        self.input_provider = S2CInputProvider(self.source)
        self.input_provider.create_schema()
        self.legacy = SelectionRuntimeFactory(self.source, self.provider)
        self.freeze_call, self.owner = freeze_call, owner
        self.workflows = {}

    def __call__(self, scenario_id):
        if scenario_id not in PUBLIC_SCENARIOS:
            raise DomainError("FROZEN_SCENARIO_DENIED", status=403)
        if scenario_id not in self.workflows:
            options = {"owner": self.owner} if self.owner is not None else {}
            self.workflows[scenario_id] = S2CWorkflow(
                self.persistence, self.provider,
                S2CContextBuilder(self.source, scenario_id, input_provider=self.input_provider),
                SkillRegistry(ROOT / "skills", role_tools=ROLE_TOOLS),
                grounding_store=self.grounding, freeze_call=self.freeze_call, **options,
            )
        return self.workflows[scenario_id]

    def reauthorize(self, session, principal, scope, material):
        if material.get("analysis_revision") != REVISION:
            return self.legacy.reauthorize(session, principal, scope, material)
        snapshot = material.get("source_snapshot")
        if type(snapshot) is not dict:
            raise DomainError("GROUNDING_CONTEXT_CHANGED")
        snapshots = snapshot.get("bundles", [snapshot])
        if type(snapshots) is not list or not snapshots:
            raise DomainError("GROUNDING_CONTEXT_CHANGED")
        scenarios = set()
        for item in snapshots:
            if type(item) is not dict or type(item.get("grounding")) is not dict:
                raise DomainError("GROUNDING_CONTEXT_CHANGED")
            sid = item["grounding"].get("scenario_id")
            if type(sid) is not str or sid not in PUBLIC_SCENARIOS:
                raise DomainError("FROZEN_SCENARIO_DENIED", status=403)
            scenarios.add(sid)
        if len(scenarios) != 1:
            raise DomainError("GROUNDING_CONTEXT_CHANGED")
        self(next(iter(scenarios)))._reauthorize(session, principal, scope, material)

    def submit_parent(self, principal, scenario_id, key, *, scope=SCOPE):
        if scenario_id not in PUBLIC_SCENARIOS:
            raise DomainError("FROZEN_SCENARIO_DENIED", status=403)
        if scenario_id == "M03":
            return self.input_provider.submit(principal, S2CRequest(scope=scope, scenario_id=scenario_id), key)
        return self.source.submit(principal, RunRequest(scope=scope, fixture_id=FIXTURES[scenario_id]), key)

    def check_workflow(self):
        base = load_workflow(self.source)
        return S2CCheckWorkflow(
            self.source, base.sandbox, base.scanner, base.bridge, base.knowledge, base.memory,
            base.skills, input_provider=self.input_provider,
        )

    async def check_parent(self, scope, parent):
        if parent.get("assessment_json") is None:
            workflow = self.check_workflow() if parent["request_json"]["fixture_id"] == "aurora-frozen-derived" else load_workflow(self.source)
            return await workflow.check(scope, parent["id"])
        return parent
