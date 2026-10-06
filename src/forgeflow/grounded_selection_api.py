"""Current loopback API with V2 source-enumerated evidence selection."""

from .grounded_api import create_grounded_app
from .grounded_selection import SELECTION_REVISION
from .grounded_selection_runtime import SelectionRuntimeFactory


def main():
    import os

    import uvicorn

    from .local import load_identities

    factory = SelectionRuntimeFactory()
    app = create_grounded_app(factory.source, load_identities(), factory)

    @app.get("/grounded/selection-health")
    def selection_health():
        return {"identity": "DEV_ONLY", "prompt_revision": SELECTION_REVISION,
                "model_task": "CONSTRAINED_EVIDENCE_SELECTION", "automatic_business_actions": False,
                "process_id": os.getpid()}

    uvicorn.run(app, host="127.0.0.1", port=8770)


if __name__ == "__main__":
    main()
