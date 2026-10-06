"""Offline TEST_DRIVER adapter checks; no model/runtime/container is started."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def live(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("s2c_live_test_driver", ROOT / "scripts/s2c_live_evaluate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeniedProvider:
    async def preflight(self):
        return {"status": "DENIED", "provenance": "TEST_DRIVER"}


async def test_existing_directory_rejected_before_provider_or_file_mutation(live, tmp_path):
    marker = tmp_path / "keep.json"
    marker.write_text("unchanged")
    with pytest.raises(RuntimeError, match="USE_NEW_EXPERIMENT_DIRECTORY"):
        await live.execute(tmp_path, "unused", provider=DeniedProvider())
    assert list(tmp_path.iterdir()) == [marker]
    assert marker.read_text() == "unchanged"


async def test_denied_preflight_never_constructs_runtime(live, tmp_path):
    def forbidden(**kwargs):
        raise AssertionError("Runtime must not be constructed")

    output = tmp_path / "new-denied"
    with pytest.raises(RuntimeError, match="PINNED_LOCAL_MODEL_NOT_READY"):
        await live.execute(output, "unused", provider=DeniedProvider(), factory_type=forbidden)
    assert [path.name for path in output.iterdir()] == ["preflight.json"]
    assert json.loads((output / "preflight.json").read_text())["status"] == "DENIED"


def test_cli_collision_does_not_append_failure_to_old_evidence(live, tmp_path, monkeypatch):
    marker = tmp_path / "sealed.json"
    marker.write_text("old evidence")
    monkeypatch.setattr(sys, "argv", ["s2c_live_evaluate.py", "--output", str(tmp_path), "--key", "unused"])
    with pytest.raises(SystemExit) as caught:
        live.main()
    assert caught.value.code == 1
    assert list(tmp_path.iterdir()) == [marker]


async def test_full_adapter_writes_fresh_five_plus_eight_denominator(live, tmp_path, monkeypatch):
    """The mock proves adapter routing; its replies are not live evidence."""
    spec = importlib.util.spec_from_file_location("s2c_live_fixture_driver", ROOT / "tests/test_s2c_evaluate.py")
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    samples = {sid: fixtures.evidence(sid) for sid in ("M01", "M01-blocked", "M02", "M03", "M04")}
    seen = []

    class Provider:
        async def preflight(self):
            return {"status": "VERIFIED", "provenance": "TEST_DRIVER"}

    class Factory:
        def __init__(self, *, provider, freeze_call):
            self.freeze_call = freeze_call
            self.current = None
            self.source = self
            self.input_provider = self

        def submit_parent(self, caller, sid, key):
            self.current = sid
            seen.append(sid)
            return samples[sid]["parent_before"]

        async def check_parent(self, scope, parent):
            return parent

        def get_run(self, caller, scope, parent_id):
            return samples[self.current]["parent_before"]

        def events(self, caller, scope, parent_id):
            return samples[self.current]["parent_events"]

        def load_run(self, caller, scope, parent_id):
            witness = samples[self.current]["actual_check_witness"]
            return {"manifest": witness["input_manifest"], "witness": witness["trusted_input"]}

        def __call__(self, sid):
            sample = samples[sid]
            catalog = sample["grounding_records"][0]["claim_catalog"]
            bundle = SimpleNamespace(to_material=lambda: {"test_driver_fixture_only": True})
            context = SimpleNamespace(build=lambda *args, **kwargs: bundle, catalog=lambda bundle: catalog,
                                      frozen_source_refs=lambda bundle: {})

            async def analyze(*args):
                for frozen in sample["call_freeze_refs"].values():
                    self.freeze_call(frozen["call"])
                return sample["analysis"]

            return SimpleNamespace(
                context=context, analyze=analyze, _reauthorize=lambda *args: None,
                grounding_store=SimpleNamespace(list=lambda *args, **kwargs: sample["grounding_records"],
                                               raw_list=lambda *args, **kwargs: sample["raw_provider_journal"]),
                store=SimpleNamespace(events=lambda *args, **kwargs: []),
            )

    monkeypatch.setattr(live, "load_identities", lambda: {"TEST_DRIVER": SimpleNamespace(roles={"admin"})})
    output = tmp_path / "new-experiment"
    result = await live.execute(output, "TEST_DRIVER", provider=Provider(), factory_type=Factory)
    assert seen == ["M01", "M01-blocked", "M02", "M03", "M04"]
    assert result["acceptance"]["scenario_count"] == 13
    assert result["acceptance"]["counts"] == {"PASS": 13, "FAIL": 0, "NOT_RUN": 0}
    assert len(list((output / "raw-chains").glob("*.json"))) == 5
    freeze = json.loads((output / "experiment-freeze.json").read_text())
    assert freeze["runtime_phase_extension"]["original_dataset_and_gold_changed"] is False
    assert freeze["model_predicts_completion_status"] is False
    assert freeze["profile"]["automatic_retries"] == 0
