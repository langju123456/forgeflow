"""scripts/fabric_service.py restores the verified prior module when the new one cannot start.

Everything that could touch the machine (port probe, health check, owner lookup, process launch, kill) is
replaced by a recorder, so these tests never open a socket, read a pid file or signal a real process.
"""

import importlib.util
import signal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OLD_PID, NEW_PID, RESTORED_PID = 4101, 4202, 4303


class Machine:
    """A pretend 8773: one verified old process that dies when it is signalled."""

    def __init__(self, module, *, revision, launches, old_alive=True, owner=OLD_PID):
        self.module, self.launches, self.calls, self.kills = module, launches, [], []
        self.up, self.revision, self.owner = old_alive, revision, owner

    def listening(self, port):
        assert port == 8773
        return self.up

    def health(self):
        return {"revision": self.revision, "process_id": OLD_PID}

    def own_pid(self, name, port, module):
        return self.owner

    def kill(self, pid, sig):
        self.kills.append((pid, sig))
        assert pid == OLD_PID, "only the verified old process may be signalled"
        self.up = False

    def launch(self, module, name, revision):
        self.calls.append((module, name, revision))
        outcome = self.launches[len(self.calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def service(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("fabric_service_under_test", ROOT / "scripts/fabric_service.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def arrange(*, launches, revision=None, old_alive=True, owner=OLD_PID):
        machine = Machine(module, revision=revision or module.EVT_REVISION, launches=launches,
                          old_alive=old_alive, owner=owner)
        monkeypatch.setattr(module, "listening", machine.listening)
        monkeypatch.setattr(module, "health", machine.health)
        monkeypatch.setattr(module, "own_pid", machine.own_pid)
        monkeypatch.setattr(module, "launch", machine.launch)
        monkeypatch.setattr(module.os, "kill", machine.kill)
        monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
        return machine

    return module, arrange


def exit_message(module, **options):
    with pytest.raises(SystemExit) as raised:
        module.main(**options)
    return str(raised.value)


def test_an_oserror_while_launching_restores_the_verified_prior_module(service):
    module, arrange = service
    machine = arrange(launches=[PermissionError("cannot open the log"), RESTORED_PID])
    message = exit_message(module)
    assert machine.calls == [(module.MODULE, "fabric", module.REVISION),
                             ("forgeflow.evt_api", "evt", module.EVT_REVISION)]
    assert "PermissionError" in message and f"restore PID={RESTORED_PID}" in message
    assert machine.kills == [(OLD_PID, signal.SIGTERM)]


def test_a_new_module_that_never_becomes_healthy_also_restores_the_prior_one(service):
    module, arrange = service
    machine = arrange(launches=[None, RESTORED_PID])
    assert f"restore PID={RESTORED_PID}" in exit_message(module)
    assert [call[0] for call in machine.calls] == [module.MODULE, "forgeflow.evt_api"]


def test_a_restart_of_the_current_module_restores_the_current_module(service):
    module, arrange = service
    machine = arrange(launches=[OSError("disk full"), RESTORED_PID], revision=module.REVISION)
    assert f"restore PID={RESTORED_PID}" in exit_message(module, restart=True)
    assert machine.calls[1] == (module.MODULE, "fabric", module.REVISION)


def test_a_failed_restore_is_reported_not_hidden(service):
    module, arrange = service
    arrange(launches=[OSError("first"), OSError("second")])
    message = exit_message(module)
    assert "prior module restore failed" in message and "OSError" in message


def test_nothing_is_restored_when_there_was_no_old_process(service):
    module, arrange = service
    machine = arrange(launches=[OSError("cannot start")], old_alive=False)
    assert "restore PID=None" in exit_message(module)
    assert machine.calls == [(module.MODULE, "fabric", module.REVISION)] and machine.kills == []


def test_an_unverified_port_occupant_is_never_signalled_or_replaced(service):
    module, arrange = service
    machine = arrange(launches=[], revision="someone-elses-revision")
    assert "Unverified port occupant" in exit_message(module)
    assert machine.kills == [] and machine.calls == []


def test_a_pid_that_does_not_match_the_health_report_is_never_signalled(service):
    module, arrange = service
    machine = arrange(launches=[], owner=9999)
    assert "Unverified port occupant" in exit_message(module)
    assert machine.kills == [] and machine.calls == []


def test_a_successful_start_replaces_only_the_verified_process(service, capsys):
    module, arrange = service
    machine = arrange(launches=[NEW_PID])
    module.main()
    assert f'"pid": {NEW_PID}' in capsys.readouterr().out
    assert machine.kills == [(OLD_PID, signal.SIGTERM)] and len(machine.calls) == 1
