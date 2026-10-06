"""Initialize the new task-local database using generated development credentials."""

import json
from pathlib import Path

import psycopg
from psycopg import sql

root = Path(__file__).resolve().parents[1] / ".runtime"
credentials = json.loads((root / "db-bootstrap.json").read_text())
with psycopg.connect(
    host="127.0.0.1",
    port=55436,
    user="postgres",
    dbname="forgeflow",
    password=credentials["bootstrap"],
    autocommit=True,
) as conn:
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='forgeflow_app'").fetchone():
        conn.execute(
            sql.SQL("CREATE ROLE forgeflow_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                sql.Literal(credentials["app"])
            )
        )
    conn.execute("ALTER DATABASE forgeflow OWNER TO forgeflow_app")
    conn.execute("ALTER SCHEMA public OWNER TO forgeflow_app")
    conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
with psycopg.connect(
    host="127.0.0.1", port=55436, user="forgeflow_app", dbname="forgeflow", password=credentials["app"]
) as conn:
    row = conn.execute(
        "SELECT current_user, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=current_user"
    ).fetchone()
    assert row == ("forgeflow_app", False, False, False)
    print(
        {
            "database": "forgeflow",
            "application_role": row[0],
            "superuser": row[1],
            "create_database": row[2],
            "create_role": row[3],
        }
    )
