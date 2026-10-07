# ruff: noqa: F811  (the export fixture is imported from test_frozen_replay_at_anchor)
"""The frozen replay must run exactly the anchor commit's regrade cases, whatever the caller's pytest options.

PYTEST_ADDOPTS (and PYTEST_PLUGINS and friends) are inherited by a child pytest, so a caller could deselect the
full archive replay and still get "25 passed" and a zero exit code (F4). The replay now scrubs PYTEST_* from the
child environment, reads a junit report instead of trusting the exit code, and requires the whole fixed case set.
"""

import pytest
import test_frozen_replay_at_anchor as replay
from test_frozen_replay_at_anchor import export  # noqa: F401  (module-scoped fixture)

CRITICAL = "test_entire_real_archive_replay_keeps_original_eight_faults_and_thirteen_denominator"


def complete(count=26, **overrides):
    results = {f"tests.test_s2c_regrade::case_{index}": "passed" for index in range(count - 1)}
    results[f"tests.test_s2c_regrade::{CRITICAL}"] = "passed"
    results.update(overrides)
    return results


@pytest.mark.parametrize("options", [
    f"--deselect tests/test_s2c_regrade.py::{CRITICAL}",
    "-k no_such_test_name",
    "-m no_such_marker",
])
def test_inherited_pytest_options_cannot_change_which_cases_run(export, monkeypatch, options):
    monkeypatch.setenv("PYTEST_ADDOPTS", options)
    results = replay.run_anchor_regrade_tests(export)
    replay.assert_complete(results)
    assert any(CRITICAL in name for name in results)


def test_pytest_variables_are_removed_from_the_child_environment(export):
    environment = replay.anchor_environment(export, {"PYTEST_ADDOPTS": "-k x", "PYTEST_PLUGINS": "y",
                                                     "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "KEEP": "1"})
    assert [name for name in environment if name.startswith("PYTEST_")] == []
    assert environment["KEEP"] == "1" and environment["PYTHONPATH"] == str(export / "src")


def test_a_complete_result_set_is_accepted():
    replay.assert_complete(complete())


@pytest.mark.parametrize("results", [
    complete(count=25),
    {key: value for key, value in complete().items() if CRITICAL not in key},
    complete(**{"tests.test_s2c_regrade::case_3": "skipped"}),
    complete(**{"tests.test_s2c_regrade::case_3": "failed"}),
    complete(**{f"tests.test_s2c_regrade::{CRITICAL}": "error"}),
    {},
])
def test_a_missing_skipped_or_failed_case_is_rejected(results):
    with pytest.raises(AssertionError):
        replay.assert_complete(results)
