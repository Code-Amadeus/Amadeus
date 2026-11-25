"""SDK-owned stdlib-only schema 18/19 policy metadata reader.

Host distribution is a byte-identical build copy. Contract: policy-metadata-19-v1.
No Memory, candidate, source or vector body is selected. Schema metadata and
initial-state signatures are generated from canonical Eidema schema.sql.
"""
from __future__ import annotations

CONTRACT_VERSION = "policy-metadata-19-v1"


def _exclusion_rows(db, user_id):
    return tuple(tuple(row) for row in db.execute("SELECT DISTINCT namespace,record_key,revision,start_cp,end_cp FROM exclusions WHERE user_id=? ORDER BY namespace,record_key,revision,start_cp", (user_id,)))


def _retirement_rows(db, user_id):
    return tuple(tuple(row) for row in db.execute("SELECT o.namespace,o.record_key,o.revision,o.start_cp,o.end_cp,m.status_reason FROM memory_retirement_sources o JOIN memories m ON m.memory_id=o.memory_id WHERE m.user_id=? AND m.retention='explicit' AND m.state IN ('superseded','retracted') UNION SELECT u.namespace,u.record_key,u.revision,NULL,NULL,COALESCE(m.invalid_reason,m.status_reason) FROM recall_uses u JOIN memories m ON m.memory_id=u.memory_id WHERE u.user_id=? AND (m.state IN ('superseded','retracted') OR m.invalid_reason IS NOT NULL) UNION SELECT b.namespace,b.record_key,b.revision,NULL,NULL,b.invalid_reason FROM output_bindings b WHERE b.user_id=? AND b.invalid_reason IS NOT NULL AND NOT EXISTS (SELECT 1 FROM exclusions e WHERE e.user_id=b.user_id AND e.namespace=b.namespace AND e.record_key=b.record_key) ORDER BY 1,2,3,4,5,6", (user_id, user_id, user_id)))


def _output_rows(db, user_id):
    return tuple(tuple(row) for row in db.execute("SELECT namespace,record_key,revision FROM output_bindings WHERE user_id=? UNION SELECT namespace,record_key,revision FROM recall_uses WHERE user_id=? ORDER BY 1,2,3", (user_id, user_id)))


def _owner_namespaces(db):
    tables = ("memories", "operations", "exclusions", "forgotten", "recall_uses",
              "source_streams", "output_bindings", "output_source_reads",
              "output_parent_reads", "output_candidate_reads")
    return frozenset(row[0] for row in db.execute(" UNION ".join(
        "SELECT user_id FROM " + table for table in tables)))


def validate_schema(db):
    state = db.execute("SELECT schema_version FROM store_state WHERE singleton=1").fetchone()
    if state is None or state[0] not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError("unsupported_source_policy_schema")
    tables = {row[1]:row[2] for row in db.execute("PRAGMA main.table_list")
              if not row[1].startswith("sqlite_") and row[2] != "shadow"}
    if tables != SCHEMA_TABLE_TYPES:
        raise ValueError("incomplete_source_policy_schema")
    # Column names allow legitimate migrated representations while refusing
    # missing authority/provenance fields. Pristine proof below is stricter.
    version = state[0]
    for table, columns in SCHEMA_COLUMNS.items():
        expected = COLUMN_OVERRIDES.get(version, {}).get(table, columns)
        actual = tuple(row[1] for row in db.execute('PRAGMA table_info("' + table + '")'))
        if actual != tuple(row[1] for row in expected):
            raise ValueError("incomplete_source_policy_schema")
    return version


def read_source_policy_metadata(db, user_id):
    """Read one caller-held SQLite snapshot without importing Eidema.

    Returned coordinates are local Host projection facts; they grant neither
    purpose nor execution authority and are never model/UI payloads.
    """
    if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 2048:
        raise ValueError("invalid_identity")
    validate_schema(db)
    return {"change_seq":db.execute("SELECT change_seq FROM store_state WHERE singleton=1").fetchone()[0],
            "exclusions":_exclusion_rows(db, user_id),
            "retirements":_retirement_rows(db, user_id),
            "outputs":_output_rows(db, user_id),
            "owner_namespaces":_owner_namespaces(db)}


def is_pristine_core(db):
    """Prove canonical initial schema 18/19 state, including unowned domain rows.

    Every required non-shadow table must exist with its native column schema.
    Body-bearing tables are checked with SELECT 1 only. Seeded metadata rows
    must equal canonical schema initialization; no user-derived state survives.
    """
    version = validate_schema(db)
    for table, columns in SCHEMA_COLUMNS.items():
        expected = COLUMN_OVERRIDES.get(version, {}).get(table, columns)
        actual = [tuple(row) for row in db.execute('PRAGMA table_info("' + table + '")')]
        if actual != expected:
            raise ValueError("incomplete_source_policy_schema")
        if table in INITIAL_METADATA_ROWS:
            rows = [tuple(row) for row in db.execute('SELECT * FROM "' + table + '"')]
            expected_rows = INITIAL_ROW_OVERRIDES.get(version, {}).get(table, INITIAL_METADATA_ROWS[table])
            if rows != expected_rows:
                return False
        elif db.execute('SELECT 1 FROM "' + table + '" LIMIT 1').fetchone() is not None:
            return False
    return True


# BEGIN GENERATED SCHEMA MANIFEST -- generated from the canonical schema files.
SUPPORTED_SCHEMA_VERSIONS = (18, 19)
SCHEMA_SHA256 = 'c0c43ae7430d0239cbcc14b46d70891883a7c207a6af646b171928bd6adbed58'
SCHEMA_TABLE_TYPES = {'candidate_comparisons': 'table',
 'candidate_evidence': 'table',
 'candidate_submissions': 'table',
 'candidates': 'table',
 'derived_authorizations': 'table',
 'embeddings': 'table',
 'exclusions': 'table',
 'forgotten': 'table',
 'formation_attempts': 'table',
 'formation_authority_inputs': 'view',
 'formation_cursors': 'table',
 'formation_gap_handoffs': 'table',
 'formation_inputs': 'table',
 'formation_processed_sources': 'table',
 'formation_quote_failures': 'table',
 'formation_tasks': 'table',
 'maintenance_budgets': 'table',
 'maintenance_clock': 'table',
 'memories': 'table',
 'memory_fts': 'virtual',
 'memory_retirement_sources': 'view',
 'memory_sources': 'view',
 'operations': 'table',
 'origins': 'table',
 'output_bindings': 'table',
 'output_candidate_reads': 'table',
 'output_parent_reads': 'table',
 'output_source_reads': 'table',
 'recall_uses': 'table',
 'receipts': 'table',
 'reformation_blocks': 'table',
 'reformation_config': 'table',
 'reformation_events': 'table',
 'retrieval_documents': 'table',
 'retrieval_embeddings': 'table',
 'retrieval_fts': 'virtual',
 'search_documents': 'table',
 'source_changes': 'table',
 'source_states': 'table',
 'source_streams': 'table',
 'store_state': 'table'}
SCHEMA_COLUMNS = {'candidate_comparisons': [(0, 'candidate_id', 'TEXT', 0, None, 1),
                           (1, 'comparison_kind', 'TEXT', 1, None, 0),
                           (2, 'left_ordinal', 'INTEGER', 1, None, 0),
                           (3, 'right_ordinal', 'INTEGER', 1, None, 0)],
 'candidate_evidence': [(0, 'candidate_id', 'TEXT', 1, None, 1),
                        (1, 'ordinal', 'INTEGER', 1, None, 2),
                        (2, 'role', 'TEXT', 1, None, 0),
                        (3, 'group_no', 'INTEGER', 1, None, 0),
                        (4, 'namespace', 'TEXT', 1, None, 0),
                        (5, 'record_key', 'TEXT', 1, None, 0),
                        (6, 'revision', 'INTEGER', 1, None, 0),
                        (7, 'start_cp', 'INTEGER', 1, None, 0),
                        (8, 'end_cp', 'INTEGER', 1, None, 0)],
 'candidate_submissions': [(0, 'attempt_id', 'TEXT', 0, None, 1), (1, 'fingerprint', 'TEXT', 0, None, 0)],
 'candidates': [(0, 'candidate_id', 'TEXT', 0, None, 1),
                (1, 'task_id', 'TEXT', 1, None, 0),
                (2, 'attempt_id', 'TEXT', 1, None, 0),
                (3, 'item_key', 'TEXT', 1, None, 0),
                (4, 'text', 'TEXT', 0, None, 0),
                (5, 'kind', 'TEXT', 1, None, 0),
                (6, 'condition_text', 'TEXT', 0, None, 0),
                (7, 'created_at_ms', 'INTEGER', 1, None, 0),
                (8, 'row_version', 'INTEGER', 1, '1', 0),
                (9, 'state', 'TEXT', 1, "'pending'", 0),
                (10, 'invalid_reason', 'TEXT', 0, None, 0),
                (11, 'index_state', 'TEXT', 1, "'pending'", 0),
                (12, 'reformation_event_id', 'INTEGER', 0, None, 0)],
 'derived_authorizations': [(0, 'authorization_id', 'TEXT', 0, None, 1),
                            (1, 'stream_id', 'TEXT', 1, None, 0),
                            (2, 'policy_id', 'TEXT', 1, None, 0),
                            (3, 'policy_version', 'TEXT', 1, None, 0),
                            (4, 'pipeline_version', 'TEXT', 1, None, 0),
                            (5, 'generation', 'INTEGER', 1, None, 0),
                            (6, 'enabled', 'INTEGER', 1, '0', 0)],
 'embeddings': [(0, 'memory_id', 'TEXT', 1, None, 1),
                (1, 'row_version', 'INTEGER', 1, None, 0),
                (2, 'embedding_version', 'TEXT', 1, None, 2),
                (3, 'chunk_no', 'INTEGER', 1, None, 3),
                (4, 'dimensions', 'INTEGER', 1, None, 0),
                (5, 'vector', 'BLOB', 1, None, 0)],
 'exclusions': [(0, 'exclusion_id', 'INTEGER', 0, None, 1),
                (1, 'user_id', 'TEXT', 1, None, 0),
                (2, 'namespace', 'TEXT', 1, None, 0),
                (3, 'record_key', 'TEXT', 1, None, 0),
                (4, 'revision', 'INTEGER', 1, None, 0),
                (5, 'start_cp', 'INTEGER', 0, None, 0),
                (6, 'end_cp', 'INTEGER', 0, None, 0)],
 'forgotten': [(0, 'memory_id', 'TEXT', 0, None, 1), (1, 'user_id', 'TEXT', 1, None, 0)],
 'formation_attempts': [(0, 'attempt_id', 'TEXT', 0, None, 1),
                        (1, 'task_id', 'TEXT', 1, None, 0),
                        (2, 'reservation_key', 'TEXT', 1, None, 0),
                        (3, 'window_id', 'TEXT', 1, None, 0),
                        (4, 'reserved_input_tokens', 'INTEGER', 1, None, 0),
                        (5, 'reserved_output_tokens', 'INTEGER', 1, None, 0),
                        (6, 'reserved_cost_units', 'INTEGER', 1, None, 0),
                        (7, 'actual_input_tokens', 'INTEGER', 0, None, 0),
                        (8, 'actual_output_tokens', 'INTEGER', 0, None, 0),
                        (9, 'actual_cost_units', 'INTEGER', 0, None, 0),
                        (10, 'outcome', 'TEXT', 1, None, 0),
                        (11, 'reserved_at_ms', 'INTEGER', 1, None, 0),
                        (12, 'cold_dispatched_at_ms', 'INTEGER', 0, None, 0)],
 'formation_authority_inputs': [(0, 'task_id', 'TEXT', 0, None, 0),
                                (1, 'namespace', 'TEXT', 0, None, 0),
                                (2, 'record_key', 'TEXT', 0, None, 0),
                                (3, 'revision', 'INTEGER', 0, None, 0),
                                (4, 'state_version', 'INTEGER', 0, None, 0),
                                (5, 'policy_revision', 'TEXT', 0, None, 0),
                                (6, 'recorded_at_ms', 'INTEGER', 0, None, 0)],
 'formation_cursors': [(0, 'stream_id', 'TEXT', 1, None, 1),
                       (1, 'pipeline_version', 'TEXT', 1, None, 2),
                       (2, 'position', 'INTEGER', 1, None, 0)],
 'formation_gap_handoffs': [(0, 'task_id', 'TEXT', 1, None, 1),
                            (1, 'parent_task_id', 'TEXT', 1, None, 2),
                            (2, 'item_key', 'TEXT', 1, None, 3),
                            (3, 'direct', 'INTEGER', 1, None, 0)],
 'formation_inputs': [(0, 'task_id', 'TEXT', 1, None, 1),
                      (1, 'ordinal', 'INTEGER', 1, None, 2),
                      (2, 'namespace', 'TEXT', 1, None, 0),
                      (3, 'record_key', 'TEXT', 1, None, 0),
                      (4, 'revision', 'INTEGER', 1, None, 0),
                      (5, 'state_version', 'INTEGER', 1, None, 0),
                      (6, 'policy_revision', 'TEXT', 1, None, 0),
                      (7, 'recorded_at_ms', 'INTEGER', 0, None, 0),
                      (8, 'input_role', 'TEXT', 1, "'incremental'", 0)],
 'formation_processed_sources': [(0, 'stream_id', 'TEXT', 1, None, 1),
                                 (1, 'pipeline_version', 'TEXT', 1, None, 2),
                                 (2, 'namespace', 'TEXT', 1, None, 3),
                                 (3, 'record_key', 'TEXT', 1, None, 4),
                                 (4, 'state_version', 'INTEGER', 1, None, 0)],
 'formation_quote_failures': [(0, 'attempt_id', 'TEXT', 1, None, 1),
                              (1, 'item_key', 'TEXT', 1, None, 2),
                              (2, 'error_code', 'TEXT', 1, None, 0),
                              (3, 'input_ordinal', 'INTEGER', 1, None, 3),
                              (4, 'declared_role_mask', 'INTEGER', 1, None, 0)],
 'formation_tasks': [(0, 'task_id', 'TEXT', 0, None, 1),
                     (1, 'stream_id', 'TEXT', 1, None, 0),
                     (2, 'pipeline_version', 'TEXT', 1, None, 0),
                     (3, 'cursor_before', 'INTEGER', 1, None, 0),
                     (4, 'cursor_after', 'INTEGER', 1, None, 0),
                     (5, 'state', 'TEXT', 1, None, 0),
                     (6, 'max_attempts', 'INTEGER', 1, None, 0),
                     (7, 'current_attempt_id', 'TEXT', 0, None, 0),
                     (8, 'work_class', 'TEXT', 1, "'warm'", 0),
                     (9, 'target_use_mask', 'INTEGER', 1, '0', 0),
                     (10, 'reformation_event_id', 'INTEGER', 0, None, 0),
                     (11, 'reformation_candidate_id', 'TEXT', 0, None, 0),
                     (12, 'reformation_key', 'TEXT', 0, None, 0),
                     (13, 'maintenance_completion_order', 'INTEGER', 0, None, 0)],
 'maintenance_budgets': [(0, 'window_id', 'TEXT', 0, None, 1),
                         (1, 'start_ms', 'INTEGER', 1, None, 0),
                         (2, 'end_ms', 'INTEGER', 1, None, 0),
                         (3, 'max_calls', 'INTEGER', 1, None, 0),
                         (4, 'max_input_tokens', 'INTEGER', 1, None, 0),
                         (5, 'max_output_tokens', 'INTEGER', 1, None, 0),
                         (6, 'max_cost_units', 'INTEGER', 1, None, 0)],
 'maintenance_clock': [(0, 'singleton', 'INTEGER', 0, None, 1),
                       (1, 'last_reservation_ms', 'INTEGER', 1, '0', 0)],
 'memories': [(0, 'memory_id', 'TEXT', 0, None, 1),
              (1, 'root_id', 'TEXT', 1, None, 0),
              (2, 'user_id', 'TEXT', 1, None, 0),
              (3, 'scope_kind', 'TEXT', 1, None, 0),
              (4, 'character_id', 'TEXT', 0, None, 0),
              (5, 'project_id', 'TEXT', 0, None, 0),
              (6, 'text', 'TEXT', 0, None, 0),
              (7, 'kind', 'TEXT', 1, None, 0),
              (8, 'condition_text', 'TEXT', 0, None, 0),
              (9, 'state', 'TEXT', 1, None, 0),
              (10, 'status_reason', 'TEXT', 0, None, 0),
              (11, 'replaced_by', 'TEXT', 0, None, 0),
              (12, 'row_version', 'INTEGER', 1, None, 0),
              (13, 'created_at_ms', 'INTEGER', 1, None, 0),
              (14, 'updated_at_ms', 'INTEGER', 1, None, 0),
              (15, 'index_state', 'TEXT', 1, None, 0),
              (16, 'index_version', 'TEXT', 0, None, 0),
              (17, 'index_error', 'TEXT', 0, None, 0),
              (18, 'retention', 'TEXT', 1, "'explicit'", 0),
              (19, 'invalid_reason', 'TEXT', 0, None, 0),
              (20, 'use_mask', 'INTEGER', 1, '3', 0),
              (21, 'authorization_id', 'TEXT', 0, None, 0),
              (22, 'authorization_generation', 'INTEGER', 0, None, 0)],
 'memory_fts': [(0, 'text', '', 0, None, 0)],
 'memory_retirement_sources': [(0, 'memory_id', 'TEXT', 0, None, 0),
                               (1, 'namespace', 'TEXT', 0, None, 0),
                               (2, 'record_key', 'TEXT', 0, None, 0),
                               (3, 'revision', 'INTEGER', 0, None, 0),
                               (4, 'start_cp', 'INTEGER', 0, None, 0),
                               (5, 'end_cp', 'INTEGER', 0, None, 0)],
 'memory_sources': [(0, 'memory_id', 'TEXT', 0, None, 0),
                    (1, 'namespace', 'TEXT', 0, None, 0),
                    (2, 'record_key', 'TEXT', 0, None, 0),
                    (3, 'revision', 'INTEGER', 0, None, 0),
                    (4, 'start_cp', 'INTEGER', 0, None, 0),
                    (5, 'end_cp', 'INTEGER', 0, None, 0)],
 'operations': [(0, 'user_id', 'TEXT', 1, None, 1),
                (1, 'operation_id', 'TEXT', 1, None, 2),
                (2, 'item_id', 'TEXT', 1, None, 3),
                (3, 'operation_kind', 'TEXT', 1, None, 0),
                (4, 'memory_id', 'TEXT', 0, None, 0),
                (5, 'outcome', 'TEXT', 1, None, 0),
                (6, 'fingerprint', 'TEXT', 0, None, 0),
                (7, 'candidate_id', 'TEXT', 0, None, 0),
                (8, 'reviewed_at_ms', 'INTEGER', 0, None, 0),
                (9, 'formation_task_id', 'TEXT', 0, None, 0)],
 'origins': [(0, 'memory_id', 'TEXT', 0, None, 1),
             (1, 'namespace', 'TEXT', 0, None, 0),
             (2, 'record_key', 'TEXT', 0, None, 0),
             (3, 'revision', 'INTEGER', 0, None, 0),
             (4, 'start_cp', 'INTEGER', 0, None, 0),
             (5, 'end_cp', 'INTEGER', 0, None, 0),
             (6, 'candidate_id', 'TEXT', 0, None, 0)],
 'output_bindings': [(0, 'user_id', 'TEXT', 1, None, 1),
                     (1, 'namespace', 'TEXT', 1, None, 2),
                     (2, 'record_key', 'TEXT', 1, None, 3),
                     (3, 'revision', 'INTEGER', 1, None, 4),
                     (4, 'invalid_reason', 'TEXT', 0, None, 0),
                     (5, 'privacy_invalidated', 'INTEGER', 1, '0', 0)],
 'output_candidate_reads': [(0, 'user_id', 'TEXT', 1, None, 1),
                            (1, 'namespace', 'TEXT', 1, None, 2),
                            (2, 'record_key', 'TEXT', 1, None, 3),
                            (3, 'revision', 'INTEGER', 1, None, 4),
                            (4, 'candidate_id', 'TEXT', 1, None, 5),
                            (5, 'row_version', 'INTEGER', 1, None, 0)],
 'output_parent_reads': [(0, 'user_id', 'TEXT', 1, None, 1),
                         (1, 'namespace', 'TEXT', 1, None, 2),
                         (2, 'record_key', 'TEXT', 1, None, 3),
                         (3, 'revision', 'INTEGER', 1, None, 4),
                         (4, 'parent_namespace', 'TEXT', 1, None, 5),
                         (5, 'parent_record_key', 'TEXT', 1, None, 6),
                         (6, 'parent_revision', 'INTEGER', 1, None, 7)],
 'output_source_reads': [(0, 'user_id', 'TEXT', 1, None, 1),
                         (1, 'namespace', 'TEXT', 1, None, 2),
                         (2, 'record_key', 'TEXT', 1, None, 3),
                         (3, 'revision', 'INTEGER', 1, None, 4),
                         (4, 'source_namespace', 'TEXT', 1, None, 5),
                         (5, 'source_record_key', 'TEXT', 1, None, 6),
                         (6, 'source_revision', 'INTEGER', 1, None, 0),
                         (7, 'state_version', 'INTEGER', 1, None, 0),
                         (8, 'policy_revision', 'TEXT', 1, None, 0),
                         (9, 'recorded_at_ms', 'INTEGER', 0, None, 0)],
 'recall_uses': [(0, 'user_id', 'TEXT', 1, None, 1),
                 (1, 'memory_id', 'TEXT', 1, None, 5),
                 (2, 'row_version', 'INTEGER', 1, None, 0),
                 (3, 'namespace', 'TEXT', 1, None, 2),
                 (4, 'record_key', 'TEXT', 1, None, 3),
                 (5, 'revision', 'INTEGER', 1, None, 4)],
 'receipts': [(0, 'memory_id', 'TEXT', 0, None, 1), (1, 'retained_text', 'TEXT', 1, None, 0)],
 'reformation_blocks': [(0, 'event_id', 'INTEGER', 1, None, 1),
                        (1, 'candidate_id', 'TEXT', 1, None, 2),
                        (2, 'reason', 'TEXT', 1, None, 0),
                        (3, 'required_sources', 'INTEGER', 1, None, 0)],
 'reformation_config': [(0, 'stream_id', 'TEXT', 1, None, 1),
                        (1, 'pipeline_version', 'TEXT', 1, None, 2),
                        (2, 'enabled', 'INTEGER', 1, None, 0),
                        (3, 'prefer_reformation', 'INTEGER', 1, '1', 0)],
 'reformation_events': [(0, 'event_id', 'INTEGER', 0, None, 1),
                        (1, 'stream_id', 'TEXT', 1, None, 0),
                        (2, 'pipeline_version', 'TEXT', 1, None, 0),
                        (3, 'reason', 'TEXT', 1, None, 0),
                        (4, 'candidate_cursor', 'TEXT', 1, "''", 0),
                        (5, 'state', 'TEXT', 1, "'pending'", 0)],
 'retrieval_documents': [(0, 'document_id', 'INTEGER', 0, None, 1),
                         (1, 'document_key', 'TEXT', 1, None, 0),
                         (2, 'target_kind', 'TEXT', 1, None, 0),
                         (3, 'stream_id', 'TEXT', 1, None, 0),
                         (4, 'namespace', 'TEXT', 0, None, 0),
                         (5, 'record_key', 'TEXT', 0, None, 0),
                         (6, 'revision', 'INTEGER', 0, None, 0),
                         (7, 'candidate_id', 'TEXT', 0, None, 0),
                         (8, 'target_version', 'INTEGER', 1, None, 0),
                         (9, 'body', 'TEXT', 1, None, 0),
                         (10, 'condition_text', 'TEXT', 0, None, 0),
                         (11, 'lexical_text', 'TEXT', 1, None, 0),
                         (12, 'index_state', 'TEXT', 1, "'pending'", 0),
                         (13, 'index_version', 'TEXT', 0, None, 0),
                         (14, 'index_dimensions', 'INTEGER', 0, None, 0),
                         (15, 'index_error', 'TEXT', 0, None, 0)],
 'retrieval_embeddings': [(0, 'document_id', 'INTEGER', 1, None, 1),
                          (1, 'embedding_version', 'TEXT', 1, None, 0),
                          (2, 'chunk_no', 'INTEGER', 1, None, 2),
                          (3, 'dimensions', 'INTEGER', 1, None, 0),
                          (4, 'vector', 'BLOB', 1, None, 0)],
 'retrieval_fts': [(0, 'lexical_text', '', 0, None, 0)],
 'search_documents': [(0, 'document_id', 'INTEGER', 0, None, 1),
                      (1, 'memory_id', 'TEXT', 1, None, 0),
                      (2, 'row_version', 'INTEGER', 1, None, 0),
                      (3, 'text', 'TEXT', 1, None, 0)],
 'source_changes': [(0, 'stream_id', 'TEXT', 1, None, 1),
                    (1, 'position', 'INTEGER', 1, None, 2),
                    (2, 'namespace', 'TEXT', 1, None, 0),
                    (3, 'record_key', 'TEXT', 1, None, 0),
                    (4, 'revision', 'INTEGER', 1, None, 0),
                    (5, 'action', 'TEXT', 1, None, 0),
                    (6, 'formation_allowed', 'INTEGER', 1, None, 0),
                    (7, 'policy_revision', 'TEXT', 1, None, 0),
                    (8, 'recorded_at_ms', 'INTEGER', 0, None, 0),
                    (9, 'state_version', 'INTEGER', 1, None, 0),
                    (10, 'lookup_allowed', 'INTEGER', 1, '0', 0),
                    (11, 'derived_use_mask', 'INTEGER', 1, '0', 0),
                    (12, 'evidence_only', 'INTEGER', 1, '0', 0)],
 'source_states': [(0, 'namespace', 'TEXT', 1, None, 1),
                   (1, 'record_key', 'TEXT', 1, None, 2),
                   (2, 'stream_id', 'TEXT', 1, None, 0),
                   (3, 'revision', 'INTEGER', 1, None, 0),
                   (4, 'action', 'TEXT', 1, None, 0),
                   (5, 'formation_allowed', 'INTEGER', 1, None, 0),
                   (6, 'policy_revision', 'TEXT', 1, None, 0),
                   (7, 'recorded_at_ms', 'INTEGER', 0, None, 0),
                   (8, 'state_version', 'INTEGER', 1, None, 0),
                   (9, 'lookup_allowed', 'INTEGER', 1, '0', 0),
                   (10, 'derived_use_mask', 'INTEGER', 1, '0', 0),
                   (11, 'evidence_only', 'INTEGER', 1, '0', 0)],
 'source_streams': [(0, 'stream_id', 'TEXT', 0, None, 1),
                    (1, 'scope_kind', 'TEXT', 1, None, 0),
                    (2, 'user_id', 'TEXT', 1, None, 0),
                    (3, 'character_id', 'TEXT', 0, None, 0),
                    (4, 'project_id', 'TEXT', 0, None, 0),
                    (5, 'received_position', 'INTEGER', 1, '0', 0)],
 'store_state': [(0, 'singleton', 'INTEGER', 0, None, 1),
                 (1, 'schema_version', 'INTEGER', 1, None, 0),
                 (2, 'change_seq', 'INTEGER', 1, '0', 0),
                 (3, 'cleanup_pending', 'INTEGER', 1, '0', 0)]}
INITIAL_METADATA_ROWS = {'maintenance_clock': [(1, 0)], 'store_state': [(1, 19, 0, 0)]}
COLUMN_OVERRIDES = {18: {'source_changes': [(0, 'stream_id', 'TEXT', 1, None, 1),
                         (1, 'position', 'INTEGER', 1, None, 2),
                         (2, 'namespace', 'TEXT', 1, None, 0),
                         (3, 'record_key', 'TEXT', 1, None, 0),
                         (4, 'revision', 'INTEGER', 1, None, 0),
                         (5, 'action', 'TEXT', 1, None, 0),
                         (6, 'formation_allowed', 'INTEGER', 1, None, 0),
                         (7, 'policy_revision', 'TEXT', 1, None, 0),
                         (8, 'recorded_at_ms', 'INTEGER', 0, None, 0),
                         (9, 'state_version', 'INTEGER', 1, None, 0),
                         (10, 'lookup_allowed', 'INTEGER', 1, '0', 0),
                         (11, 'derived_use_mask', 'INTEGER', 1, '0', 0)],
      'source_states': [(0, 'namespace', 'TEXT', 1, None, 1),
                        (1, 'record_key', 'TEXT', 1, None, 2),
                        (2, 'stream_id', 'TEXT', 1, None, 0),
                        (3, 'revision', 'INTEGER', 1, None, 0),
                        (4, 'action', 'TEXT', 1, None, 0),
                        (5, 'formation_allowed', 'INTEGER', 1, None, 0),
                        (6, 'policy_revision', 'TEXT', 1, None, 0),
                        (7, 'recorded_at_ms', 'INTEGER', 0, None, 0),
                        (8, 'state_version', 'INTEGER', 1, None, 0),
                        (9, 'lookup_allowed', 'INTEGER', 1, '0', 0),
                        (10, 'derived_use_mask', 'INTEGER', 1, '0', 0)]}}
INITIAL_ROW_OVERRIDES = {18: {'store_state': [(1, 18, 0, 0)]}}
