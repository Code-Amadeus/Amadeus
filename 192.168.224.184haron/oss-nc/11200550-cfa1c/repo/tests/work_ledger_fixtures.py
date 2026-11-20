"""Construct real historical schemas, not a latest schema with a rewound version."""

from pathlib import Path
import sqlite3

from agent_host import work_ledger_store as ledger
from agent_host.work_ledger_types import canonicalize_path


HISTORICAL_MIGRATIONS = (
    ledger._MIGRATION_1,
    ledger._MIGRATION_2,
    ledger._MIGRATION_3,
    ledger._MIGRATION_4,
    ledger._MIGRATION_5_ADD_COLUMN + ledger._MIGRATION_5,
    ledger._MIGRATION_6,
    ledger._MIGRATION_7_OPERATIONS + ledger._MIGRATION_7_ADD_ATTEMPT_OPERATION + ledger._MIGRATION_7,
)


def create_historical_schema(path: Path, version: int) -> sqlite3.Connection:
    assert 1 <= version <= len(HISTORICAL_MIGRATIONS)
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript("BEGIN IMMEDIATE;\n" + "\n".join(HISTORICAL_MIGRATIONS[:version]) + "\nCOMMIT;")
    return connection


def seed_historical_work(connection, workspace: Path, *, metadata_json="{}"):
    """Seed column-named v1-v6 records before Operation existed."""
    workspace.mkdir(parents=True, exist_ok=True)
    path = canonicalize_path(workspace)
    connection.execute(
        "INSERT INTO projects(project_id,name,display_path,canonical_path,path_identity,created_at,updated_at) "
        "VALUES ('project_historical','Historical',?,?,?,1,1)",
        (path.canonical_path, path.canonical_path, path.identity_key),
    )
    connection.execute(
        "INSERT INTO work_items(work_item_id,project_id,title,goal,state,workspace_path,workspace_identity,"
        "created_at,updated_at,last_activity_at,metadata_json) "
        "VALUES ('work_historical','project_historical','Historical Work','Original goal','open',?,?,1,1,1,?)",
        (path.canonical_path, path.identity_key, metadata_json),
    )
    connection.execute(
        "INSERT INTO run_attempts(attempt_id,work_item_id,attempt_number,provider,task,execution_status,"
        "created_at,updated_at) "
        "VALUES ('attempt_historical','work_historical',1,'locus','Historical instruction','queued',1,1)",
    )
    return dict(project_id="project_historical", work_item_id="work_historical",
                attempt_id="attempt_historical", goal="Original goal",
                workspace_path=path.canonical_path, workspace_identity=path.identity_key)
