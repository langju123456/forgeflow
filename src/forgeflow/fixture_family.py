"""The one place that knows the Aurora fixture ids.

Each original fixture (aurora-pass, ...) is pinned by hash in the frozen evaluation data and never changes. The
-v2 copies differ from the originals only in valid_until, so new runs and the demo keep working after the
originals expire. Their manifests still say version v1: the facts are the same ones.
"""

from .contracts import DomainError

# Expiry of the renewed (-v2) fixtures and of the SOP document seeded into new databases.
RENEWED_VALID_UNTIL = "2027-04-05T00:00:00Z"
NAMES = ("pass", "blocked", "unknown", "timeout")
FAMILY = {f"aurora-{name}{suffix}": f"aurora-{name}" for name in NAMES for suffix in ("", "-v2")}


def family(fixture_id):
    if not isinstance(fixture_id, str) or fixture_id not in FAMILY:
        raise DomainError("SYNTHETIC_SOURCE_REQUIRED", status=422)
    return FAMILY[fixture_id]


# The frozen S2C derived-input fixture is not an Aurora manifest; it always uses the plain check template.
DERIVED_FIXTURES = frozenset({"aurora-frozen-derived"})


def template_for(fixture_id):
    if fixture_id in DERIVED_FIXTURES:
        return "evt-check"
    return "timeout" if family(fixture_id) == "aurora-timeout" else "evt-check"


def cli_fixture(name, legacy=False):
    """The fixture id a CLI choice such as "pass" means: the renewed copy unless the original is asked for."""
    return f"aurora-{name}" if legacy else f"aurora-{name}-v2"


def evidence_valid_until(manifest):
    """The earliest expiry among the fixture's measurements, as written in the fixture."""
    return min((metric["valid_until"] for metric in manifest["metrics"].values()),
               key=lambda value: value.replace("Z", "+00:00"))
