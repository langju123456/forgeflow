"""Add the S2 source-binding column; never change S1 tables or print DB credentials."""

import json

from sqlalchemy import inspect, text

from forgeflow.local import load_store

store = load_store()
inspector = inspect(store.engine)
if not inspector.has_table("analysis_events"):
    raise SystemExit("Initialize the S2 schema before applying this existing-table migration")
columns = {column["name"] for column in inspector.get_columns("analysis_events")}
changed = "material_json" not in columns
if changed:
    with store.engine.begin() as connection:
        connection.execute(text("ALTER TABLE analysis_events ADD COLUMN material_json JSON"))
print(json.dumps({"migration": "S2A_EVENTS_SOURCE_BINDINGS_V1", "table": "analysis_events", "changed": changed}))
