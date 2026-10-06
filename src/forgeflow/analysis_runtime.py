"""One local S2 composition over the existing S1 Store and API runtime."""

from pathlib import Path

from .analysis_context import AnalysisContextBuilder
from .analysis_provider import OllamaProvider
from .analysis_store import AnalysisStore
from .analysis_workflow import ROLE_TOOLS, AnalysisWorkflow
from .local import load_store
from .skills import SkillRegistry

ROOT = Path(__file__).resolve().parents[2]


def build_runtime(store=None, provider=None):
    source = store if store is not None else load_store()
    persistence = AnalysisStore(source)
    persistence.create_schema()
    workflow = AnalysisWorkflow(
        persistence,
        provider if provider is not None else OllamaProvider(),
        AnalysisContextBuilder(source),
        SkillRegistry(ROOT / "skills", role_tools=ROLE_TOOLS),
    )
    return source, workflow
