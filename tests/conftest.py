"""Session test clock: every test sees time shifted to start at T0, then moving forward.

Package code reads time only through contracts.utcnow, Store._now (the database clock) and time.time
(lease deadlines handed to the sandbox, scanner and MCP bridge), so these three are shifted together.
time.monotonic is untouched, so timeouts and event loops behave normally.

A test that hands timestamps to another process (it keeps the real clock), or that must see the real
date, carries ``@pytest.mark.real_clock``.
"""

import time

import clock_contract  # first import fixes the session start
import pytest

from forgeflow import contracts
from forgeflow.store import Store


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_clock: keep the real clock (the test passes timestamps to another process or checks the real date)",
    )


@pytest.fixture(autouse=True)
def shifted_clock(request, monkeypatch):
    if request.node.get_closest_marker("real_clock"):
        return
    offset = clock_contract.OFFSET
    seconds = offset.total_seconds()
    real_now = Store._now
    monkeypatch.setattr(time, "time", lambda: clock_contract.REAL_TIME() + seconds)
    monkeypatch.setattr(contracts, "utcnow", lambda: clock_contract.real_utcnow() + offset)
    monkeypatch.setattr(Store, "_now", lambda self, session: real_now(self, session) + offset)
