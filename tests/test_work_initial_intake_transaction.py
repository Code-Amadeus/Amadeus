"""The runtime's initial Work/Operation/Attempt is one local write set.

No model, native Provider, Git allocation or application execution is used.
Standalone WorkItem creation remains a separate supported storage operation.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from unittest.mock import patch

import pytest

from agent_host.provider_types import ProviderRunRequest
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from server.work_ledger_coordinator import WorkLedgerCoordinator


TABLES = ("work_items", "work_operations", "run_attempts")
GOAL = "Build the requested game"
SOURCE = "Could you make that game for us?"


def _rows(store):
    return {table: [dict(row) for row in store._connection.execute(f"SELECT * FROM {table}")]
            for table in TABLES}


def _prepare(store, workspace, project_id, *, intent="execute", work_item_id=""):
    metadata = {"project_id": project_id, "source": "initial_intake_test", "session_id": "intake-session",
                "turn_id": "intake-turn", "source_user_text": SOURCE, "intent": intent}
    if work_item_id:
        metadata.update(continuation="amend", work={"work_item_id": work_item_id})
    with patch("config.settings.WORK_WORKTREE_ISOLATION", False):
        return WorkLedgerCoordinator(store).prepare_request(ProviderRunRequest(
            provider="fake", task=GOAL, cwd=str(workspace), metadata=metadata,
        ))


@pytest.mark.parametrize("fail_table", ["work_operations", "run_attempts"])
def test_initial_insert_failure_rolls_back_the_new_triple_not_existing_work(tmp_path, fail_table):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = tmp_path / "work.sqlite3"
    with WorkLedgerStore(database) as store:
        project = store.create_or_get_project(workspace)
        manual = store.create_work_item(project.project_id, title="Manual unstarted Work")
        before = _rows(store)
        store._connection.execute(f"CREATE TRIGGER fail_initial BEFORE INSERT ON {fail_table} "
                                  "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END")
        with pytest.raises(WorkLedgerConflict):
            _prepare(store, workspace, project.project_id)
        assert _rows(store) == before
        assert store.get_work_item(manual.work_item_id) is not None
    with WorkLedgerStore(database) as reopened:
        assert _rows(reopened) == before


@pytest.mark.parametrize("intent", ["execute", "amend"])
def test_success_preserves_planned_intent_goal_and_source_metadata(tmp_path, intent):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = tmp_path / "work.sqlite3"
    with WorkLedgerStore(database) as store:
        project = store.create_or_get_project(workspace)
        prepared = _prepare(store, workspace, project.project_id, intent=intent)
        binding = prepared.metadata["work"]
        work = store.get_work_item(binding["work_item_id"])
        operation = store.get_operation(binding["operation_id"])
        attempt = store.get_attempt(binding["attempt_id"])
        assert work.goal == operation.instruction == attempt.task == GOAL
        assert operation.intent == intent
        assert operation.work_item_id == attempt.work_item_id == work.work_item_id
        assert attempt.operation_id == operation.operation_id
        assert attempt.execution_status == "queued"
        assert work.metadata["source_user_text"] == SOURCE
        assert operation.metadata["turn_id"] == attempt.metadata["turn_id"] == "intake-turn"
        before = _rows(store)
    with WorkLedgerStore(database) as reopened:
        assert _rows(reopened) == before


@pytest.mark.parametrize("fail_table", ["work_operations", "run_attempts"])
def test_existing_work_amendment_failure_preserves_its_prior_triple(tmp_path, fail_table):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with WorkLedgerStore(tmp_path / "work.sqlite3") as store:
        project = store.create_or_get_project(workspace)
        first = _prepare(store, workspace, project.project_id).metadata["work"]
        store.update_attempt(first["attempt_id"], execution_status="succeeded")
        store.release_writer_lease(first["attempt_id"], status="released")
        before = _rows(store)
        store._connection.execute(f"CREATE TRIGGER fail_amend BEFORE INSERT ON {fail_table} "
                                  "BEGIN SELECT RAISE(ABORT, 'injected amend failure'); END")
        with pytest.raises(WorkLedgerConflict):
            _prepare(store, workspace, project.project_id, intent="amend", work_item_id=first["work_item_id"])
        assert _rows(store) == before


@pytest.mark.parametrize("invalid", [{"provider": ""}, {"instruction": ""}, {"intent": "unsupported"}])
def test_initial_attempt_validation_cannot_leave_a_new_work(tmp_path, invalid):
    with WorkLedgerStore(tmp_path / "work.sqlite3") as store:
        project = store.create_or_get_project(tmp_path)
        before = _rows(store)
        kwargs = dict(title="Game", goal=GOAL, intent="execute", instruction=GOAL, provider="fake", task=GOAL)
        with pytest.raises(ValueError):
            store.create_work_item_with_attempt(project.project_id, **{**kwargs, **invalid})
        assert _rows(store) == before


@pytest.mark.parametrize("workspace_mode", ["local", "none"])
def test_atomic_store_preserves_exact_ids_and_distinct_instruction_fields(tmp_path, workspace_mode):
    with WorkLedgerStore(tmp_path / "work.sqlite3") as store:
        project = store.create_or_get_project(tmp_path)
        work, operation, attempt = store.create_work_item_with_attempt(
            project.project_id, title="Game", goal=GOAL, intent="amend",
            instruction="User-level instruction", provider="fake", task="Grounded provider task",
            workspace_mode=workspace_mode, work_item_id="work_exact", operation_id="operation_exact",
            attempt_id="attempt_exact", provider_run_id="run_exact",
            metadata={"work_fact": 1}, operation_metadata={"operation_fact": 2}, attempt_metadata={"attempt_fact": 3},
        )
        assert work.work_item_id == operation.work_item_id == attempt.work_item_id == "work_exact"
        assert operation.operation_id == attempt.operation_id == "operation_exact"
        assert attempt.attempt_id == "attempt_exact" and attempt.provider_run_id == "run_exact"
        assert work.goal == GOAL and operation.instruction == "User-level instruction"
        assert attempt.task == "Grounded provider task" and operation.intent == "amend"
        assert work.metadata == {"work_fact": 1}
        assert operation.metadata == {"operation_fact": 2}
        assert attempt.metadata == {"attempt_fact": 3}
        assert store.get_work_item(work.work_item_id).to_dict() == work.to_dict()
        if workspace_mode == "none":
            assert work.workspace_path == ""
            assert work.workspace_identity == "none:work_exact"
        else:
            assert Path(work.workspace_path).resolve() == tmp_path.resolve()
        before = _rows(store)
        with pytest.raises(WorkLedgerConflict):
            store.create_work_item_with_attempt(
                project.project_id, title="Other", intent="execute", instruction="Other",
                provider="fake", task="Other", provider_run_id="run_exact",
            )
        assert _rows(store) == before  # A global run-id conflict rolls the new Work back too.


_CRASH_CHILD = r'''
import os, sys
from pathlib import Path
from unittest.mock import patch
from agent_host.work_ledger_store import WorkLedgerStore
from agent_host.provider_types import ProviderRunRequest
from server.work_ledger_coordinator import WorkLedgerCoordinator
database, workspace, stage = sys.argv[1:]
with WorkLedgerStore(database) as store:
    if stage != "after_prepare":
        store._connection.create_function("exit_intake", 0, lambda: os._exit(77))
        store._connection.execute(f"CREATE TEMP TRIGGER exit_write BEFORE INSERT ON {stage} BEGIN SELECT exit_intake(); END")
    with patch("config.settings.WORK_WORKTREE_ISOLATION", False):
        WorkLedgerCoordinator(store).prepare_request(ProviderRunRequest(provider="fake", task="Build a game", cwd=workspace))
    if stage == "after_prepare":
        os._exit(77)
raise RuntimeError("crash point not reached")
'''


@pytest.mark.parametrize("stage", ["work_operations", "run_attempts", "after_prepare"])
def test_real_process_exit_recovers_all_or_none_of_initial_intake(tmp_path, stage):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = tmp_path / "work.sqlite3"
    environment = dict(os.environ, AMADEUS_SESSION_DIR=str(tmp_path / "child-session"), PYTHONUTF8="1")
    result = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-c", _CRASH_CHILD, str(database), str(workspace), stage],
        cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True,
        text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 77, result.stderr
    with WorkLedgerStore(database) as reopened:
        rows = _rows(reopened)
        expected = 1 if stage == "after_prepare" else 0
        assert {table: len(items) for table, items in rows.items()} == {table: expected for table in TABLES}
        if expected:
            work, operation, attempt = (rows[table][0] for table in TABLES)
            assert work["work_item_id"] == operation["work_item_id"] == attempt["work_item_id"]
            assert operation["operation_id"] == attempt["operation_id"]
            assert attempt["execution_status"] == "queued" and attempt["provider_run_id"] == ""


def test_distinct_initial_intakes_are_not_deduplicated_by_equal_text(tmp_path):
    database = tmp_path / "work.sqlite3"
    with WorkLedgerStore(database) as store:
        project_id = store.create_or_get_project(tmp_path).project_id
    gate = Barrier(2)

    def create():
        with WorkLedgerStore(database) as store:
            gate.wait(timeout=5)
            return store.create_work_item_with_attempt(
                project_id, title="Same wording", intent="execute", instruction=GOAL,
                provider="fake", task=GOAL, goal=GOAL,
            )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(create) for _ in range(2)]
        triples = [future.result(timeout=10) for future in futures]
    assert triples[0][0].work_item_id != triples[1][0].work_item_id
    with WorkLedgerStore(database) as store:
        assert all(len(rows) == 2 for rows in _rows(store).values())
