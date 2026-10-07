"""Contract for the test clock, shared by conftest.py and the tests that check it.

Every test runs on a clock that starts at T0 and then moves forward in real time (it is shifted, never
frozen, so lease timeouts and event loops behave). T0 is in the past and equals the S2C frozen evaluation
clock, so the shift is negative and a component that still reads the real clock is exposed immediately.
Importing this module fixes the session start; conftest.py imports it first.
"""

import os
import time
from datetime import UTC, datetime, timedelta

# FORGEFLOW_TEST_CLOCK_T0 (ISO 8601 with a zone, e.g. 2026-11-06T12:00:00+00:00) is a developer aid for
# checking how the suite behaves on another date without changing the machine clock.
T0 = datetime.fromisoformat(os.environ.get("FORGEFLOW_TEST_CLOCK_T0", "2026-10-05T12:00:00+00:00"))
REAL_TIME = time.time  # captured before any patching
REAL_AT_START = datetime.now(UTC)
MONOTONIC_AT_START = time.monotonic()
OFFSET = T0 - REAL_AT_START


def real_utcnow():
    return datetime.now(UTC)


def expected_now():
    """T0 plus the time elapsed in this session; what every shifted clock must show."""
    return T0 + timedelta(seconds=time.monotonic() - MONOTONIC_AT_START)
