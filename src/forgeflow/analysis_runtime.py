"""One local S2 composition over the existing S1 Store and API runtime."""

from pathlib import Path

from .analysis_context import AnalysisContextBuilder, ExportGrant, SourceGrant
from .analysis_provider import OllamaProvider
from .analysis_store import AnalysisStore
from .analysis_workflow import ROLE_TOOLS, AnalysisWorkflow
from .fabric_contract import FABRIC_SCOPE
from .fabric_runtime import FABRIC_SOP_HASH, load_fabric_store
from .local import load_store
from .skills import SkillRegistry

ROOT = Path(__file__).resolve().parents[2]


def fabric_grant():
    """Export grant for the Fabric project: its own scope and its own SOP text, nothing else."""
    return ExportGrant(scope=FABRIC_SCOPE, documents=(SourceGrant("aurora-evt-sop", "v1", FABRIC_SOP_HASH),))


def build_runtime(store=None, provider=None, grant=None):
    source = store if store is not None else load_store()
    persistence = AnalysisStore(source)
    persistence.create_schema()
    workflow = AnalysisWorkflow(
        persistence,
        provider if provider is not None else OllamaProvider(),
        AnalysisContextBuilder(source, grant),
        SkillRegistry(ROOT / "skills", role_tools=ROLE_TOOLS),
    )
    return source, workflow


def build_fabric_runtime(store=None, provider=None):
    """Model analysis on a Fabric parent run: the same Supervisor and PM chain, the Fabric grant."""
    return build_runtime(store if store is not None else load_fabric_store(), provider, grant=fabric_grant())
