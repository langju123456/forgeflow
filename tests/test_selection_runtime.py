"""TEST_DRIVER V2 composition must preserve legacy reads and request binding."""

import pytest
from test_analysis_workflow import checked
from test_analysis_workflow import env as base_environment
from test_grounded_workflow import EvidenceSelectionDriver

from forgeflow.contracts import DomainError
from forgeflow.grounded_runtime import GroundedRuntimeFactory
from forgeflow.grounded_selection import SELECTION_REVISION
from forgeflow.grounded_selection_runtime import SelectionRuntimeFactory


@pytest.fixture
def env(tmp_path):
    return base_environment.__wrapped__(tmp_path)


async def test_v2_factory_reads_immutable_v1_and_generates_version_bound_v2(env):
    source, _, scope, caller, _, _ = env
    parent = checked(env)
    old = GroundedRuntimeFactory(source, EvidenceSelectionDriver())
    original = await old("M01").analyze(caller, scope, parent["id"], "legacy")
    driver = EvidenceSelectionDriver()
    current = SelectionRuntimeFactory(source, driver)
    assert current.provider is current.legacy.provider
    assert current.persistence.get(caller, scope, original["id"], reauthorize=current.reauthorize) == original
    records = current.grounding.list(caller, scope, original["id"], reauthorize=current.reauthorize)
    assert len(records) == 4
    with pytest.raises(DomainError):
        await current("M01").analyze(caller, scope, parent["id"], "legacy")
    assert driver.calls == []
    result = await current("M01").analyze(caller, scope, parent["id"], "v2")
    assert result["state"] == "COMPLETE" and len(driver.calls) == 4
    assert result["material_json"]["prompt_revision"] == SELECTION_REVISION
    assert current.persistence.get(caller, scope, result["id"], reauthorize=current.reauthorize) == result
    assert source.get_run(caller, scope, parent["id"]) == parent


def test_v2_factory_fault_scope_and_unknown_revision_do_not_gain_a_provider(env):
    source, _, scope, caller, _, _ = env
    driver = EvidenceSelectionDriver()
    factory = SelectionRuntimeFactory(source, driver)
    for scenario in ("M05", "M12", "tenant:foreign", "M01-blocked-code"):
        with pytest.raises(DomainError):
            factory(scenario)
    assert factory.workflows == {} and driver.calls == []
    with pytest.raises(DomainError):
        factory.reauthorize(None, caller, scope, {"prompt_revision": "unapproved-model-version"})
