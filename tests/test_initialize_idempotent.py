"""`init` commands are safe to run again on a database that already holds the SOP (found while reading fabric_e2e).

Document versions are immutable, so seeding the same version with another expiry raises. The renewed expiry in the
seed must therefore apply only to a database that has no SOP yet; an existing one is left exactly as it is.
"""

import subprocess

import pytest
from sqlalchemy import select

from forgeflow import fabric_runtime, local
from forgeflow.store import Document, DocumentVersion, Store, filters

OLD, NEW = "2026-11-05T00:00:00Z", "2027-04-05T00:00:00Z"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("FORGEFLOW_DATABASE_URL", f"sqlite:///{tmp_path / 'init.sqlite'}")
    monkeypatch.setattr(fabric_runtime, "RUNTIME", runtime)
    monkeypatch.setattr(local, "RUNTIME", runtime)
    monkeypatch.setattr(local.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""))
    return Store(f"sqlite:///{tmp_path / 'init.sqlite'}")


def sop(store, scope):
    with store._tx() as session:
        document = session.scalar(select(Document).where(*filters(Document, scope), Document.id == "aurora-evt-sop"))
        rows = session.scalars(select(DocumentVersion).where(
            *filters(DocumentVersion, scope), DocumentVersion.document_id == "aurora-evt-sop")).all()
        return document.current_version, {row.version: str(row.valid_until)[:10] for row in rows}


@pytest.mark.parametrize(("name", "scope", "module"), [
    ("initialize_fabric", fabric_runtime.FABRIC_SCOPE, fabric_runtime),
    ("initialize", local.SCOPE, local),
])
def test_a_fresh_database_gets_the_renewed_expiry(isolated, name, scope, module):
    getattr(module, name)()
    assert sop(isolated, scope) == ("v1", {"v1": NEW[:10]})


@pytest.mark.parametrize(("name", "scope", "module"), [
    ("initialize_fabric", fabric_runtime.FABRIC_SCOPE, fabric_runtime),
    ("initialize", local.SCOPE, local),
])
def test_a_database_seeded_with_the_old_expiry_is_left_untouched_and_init_still_works(
        isolated, monkeypatch, name, scope, module):
    with monkeypatch.context() as old:
        old.setattr(module, "RENEWED_VALID_UNTIL", OLD)  # how the live database was seeded
        getattr(module, name)()
    assert sop(isolated, scope) == ("v1", {"v1": OLD[:10]})
    getattr(module, name)()  # used to raise DOCUMENT_VERSION_IMMUTABLE
    getattr(module, name)()
    assert sop(isolated, scope) == ("v1", {"v1": OLD[:10]})
