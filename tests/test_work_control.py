from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier
from unittest.mock import patch

import pytest

from agent_host.provider_contract import ProviderRequirements
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from server import control_ledger as control_ledger_module
from server.control_ledger import (
    ControlEffect,
    ControlLedgerConflict,
    ControlLedgerStore,
    ReconciliationPolicy,
)
from server.turn_admission import capture_turn_admission
from server.work_control import WorkControl, WorkEffectPayloadV1


def _admission(*, root_suffix: str = "one", epoch: int = 1, mode: str = "turn_decision"):
    admission = capture_turn_admission(
        utterance_id="utterance-" + root_suffix,
        turn_id="turn-" + root_suffix,
        session_id="session-one",
        transcript="Build the accepted artifact",
        input_source="voice",
        chat_epoch=epoch,
        pending=False,
        authority_mode=mode,
    )
    assert admission is not None
    return admission


def _payload(project_id: str, *, provider: str = "fake-alpha") -> WorkEffectPayloadV1:
    return WorkEffectPayloadV1(
        provider=provider,
        task="Build the accepted artifact",
        title="Accepted artifact",
        project_id=project_id,
        session_id="session-one",
        turn_id="turn-one",
        requirements=ProviderRequirements(
            task_kind="workspace_mutation",
            workspace_access="write",
            ownership="managed",
        ),
    )


@contextmanager
def _case(tmp_path: Path, *, provider: str = "fake-alpha"):
    database = tmp_path / "shared.sqlite3"
    project_path = tmp_path / "project"
    project_path.mkdir(parents=True)
    work = WorkLedgerStore(database)
    project = work.create_or_get_project(project_path)
    control = ControlLedgerStore(database)
    bridge = WorkControl(control, work)
    admission = _admission()
    bridge.admit(admission, fence_scope="foreground-chat")
    sealed = bridge.seal(admission, _payload(project.project_id, provider=provider))
    try:
        yield database, project, control, work, bridge, sealed["effect_id"]
    finally:
        control.close()
        work.close()


def _policy() -> ReconciliationPolicy:
    return ReconciliationPolicy(
        "provider-submission",
        max_probes=2,
        interval_seconds=3,
        ttl_seconds=20,
    )


def _counts(work: WorkLedgerStore) -> tuple[int, int, int]:
    return tuple(
        int(work._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("work_items", "work_operations", "run_attempts")
    )


@pytest.mark.parametrize("provider", ["fake-alpha", "remote-generic-beta"])
def test_exact_accepted_effect_atomically_claims_one_work_triple(tmp_path, provider):
    with _case(tmp_path, provider=provider) as (_, project, control, work, bridge, effect_id):
        result = bridge.bind_dispatch_intent(
            effect_id,
            provider_run_id=provider + "_run-1",
            lease_seconds=5,
            reconciliation=_policy(),
        )

        assert not result["replayed"]
        effect = control.get_effect(effect_id)
        assert effect["state"] == "dispatching"
        assert effect["claim_token"]
        assert effect["claim_owner"] == "work_control_c1"
        assert effect["probe_owner"] == "provider-submission"
        assert (effect["max_probes"], effect["probe_count"], effect["probe_interval"]) == (
            2,
            0,
            3,
        )
        assert effect["unknown_expires_at"] > effect["claim_expires_at"]
        assert _counts(work) == (1, 1, 1)
        binding = work.get_origin_effect_binding(effect_id)
        assert result["binding"] == binding
        assert binding is not None and binding["provider_run_id"] == provider + "_run-1"
        item = work.get_work_item(binding["work_item_id"])
        operation = work.get_operation(binding["operation_id"])
        attempt = work.get_attempt(binding["attempt_id"])
        assert item is not None and item.project_id == project.project_id
        assert operation is not None and operation.intent == "execute"
        assert attempt is not None and attempt.provider == provider
        assert attempt.execution_status == "queued"
        assert {item.origin_effect_id, operation.origin_effect_id, attempt.origin_effect_id} == {
            effect_id
        }
        assert control.get_receipt(effect_id) is None
        assert work.list_completions(item.work_item_id) == []
        assert work.list_writer_leases() == []


def test_exact_replay_returns_original_and_different_run_conflicts(tmp_path):
    with _case(tmp_path) as (_, _, control, work, bridge, effect_id):
        first = bridge.bind_dispatch_intent(
            effect_id,
            provider_run_id="fake-alpha_run-1",
            lease_seconds=5,
            reconciliation=_policy(),
        )
        before = (
            _counts(work),
            dict(control.get_effect(effect_id)),
            dict(first["binding"]),
        )
        replay = bridge.bind_dispatch_intent(
            effect_id,
            provider_run_id="fake-alpha_run-1",
            lease_seconds=99,
            reconciliation=ReconciliationPolicy("different-policy"),
        )
        assert replay["replayed"] and replay["binding"] == first["binding"]
        assert (_counts(work), control.get_effect(effect_id), replay["binding"]) == before
        with pytest.raises(WorkLedgerConflict, match="different Work binding"):
            bridge.bind_dispatch_intent(
                effect_id,
                provider_run_id="fake-alpha_run-2",
                lease_seconds=5,
                reconciliation=_policy(),
            )
        assert (_counts(work), control.get_effect(effect_id), first["binding"]) == before


def test_unknown_new_work_does_not_block_an_independent_work_in_same_project(tmp_path):
    with _case(tmp_path) as (_, project, control, work, bridge, effect_id):
        bridge.bind_dispatch_intent(
            effect_id,
            provider_run_id="fake-alpha_run-1",
            lease_seconds=5,
            reconciliation=_policy(),
        )
        replacement = _admission(root_suffix="two", epoch=2)
        bridge.admit(replacement, fence_scope="foreground-chat")
        assert control.get_effect(effect_id)["state"] == "unknown_reconciling"
        accepted = bridge.seal(
            replacement,
            replace(_payload(project.project_id), turn_id="turn-two"),
        )
        assert accepted["effect_count"] == 1
        assert control.get_effect(accepted["effect_id"])["target_key"].startswith(
            "work-source:")
        assert _counts(work) == (1, 1, 1)


def test_legacy_unknown_project_key_does_not_block_new_source_key(tmp_path):
    database = tmp_path/"shared.sqlite3"
    project_path = tmp_path/"project"
    project_path.mkdir()
    work = WorkLedgerStore(database)
    project = work.create_or_get_project(project_path)
    control = ControlLedgerStore(database)
    bridge = WorkControl(control, work)
    first_admission = _admission()
    bridge.admit(first_admission, fence_scope="foreground-chat")
    first_payload = _payload(project.project_id)
    first_effect = bridge._identity(first_admission.root_id, "work:0")
    control.accept(first_admission.root_id, chat_epoch=1,
        plan_id=bridge._identity(first_admission.root_id, "work-plan:v1"),
        effects=(ControlEffect(first_effect, "work",
            "work-project:" + project.project_id, first_payload.to_payload()),),
        evidence={"adapter":"proposal_gated_work_c1:v1",
            "transcript_hash":first_admission.transcript_hash})
    bridge.bind_dispatch_intent(first_effect,
        provider_run_id="fake-alpha_run-legacy", lease_seconds=5,
        reconciliation=_policy())

    replacement = _admission(root_suffix="two", epoch=2)
    bridge.admit(replacement, fence_scope="foreground-chat")
    assert control.get_effect(first_effect)["state"] == "unknown_reconciling"
    accepted = bridge.seal(replacement,
        replace(_payload(project.project_id), turn_id="turn-two"))
    assert control.get_effect(accepted["effect_id"])["target_key"] == (
        "work-source:session-one:turn-two")
    control.close()
    work.close()


def test_competing_connections_converge_on_one_committed_binding(tmp_path):
    with _case(tmp_path) as (database, _, _control, _work, _bridge, effect_id):
        controls = [ControlLedgerStore(database) for _ in range(2)]
        works = [WorkLedgerStore(database) for _ in range(2)]
        bridges = [WorkControl(controls[index], works[index]) for index in range(2)]
        gate = Barrier(2)

        def apply(index):
            gate.wait(timeout=5)
            return bridges[index].bind_dispatch_intent(
                effect_id,
                provider_run_id="fake-alpha_run-race",
                lease_seconds=5,
                reconciliation=_policy(),
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = [future.result(timeout=10) for future in [
                    pool.submit(apply, 0),
                    pool.submit(apply, 1),
                ]]
            assert sorted(result["replayed"] for result in results) == [False, True]
            assert results[0]["binding"] == results[1]["binding"]
            assert _counts(works[0]) == (1, 1, 1)
            assert controls[0].get_effect(effect_id)["state"] == "dispatching"
        finally:
            for control in controls:
                control.close()
            for work in works:
                work.close()


def test_claim_and_domain_intent_roll_back_together_on_callback_failure(tmp_path):
    with _case(tmp_path) as (_, _, control, work, _bridge, effect_id):
        def fail(cursor, _payload):
            cursor.execute("UPDATE projects SET name='changed'")
            raise RuntimeError("injected")

        with pytest.raises(RuntimeError, match="injected"):
            control.claim_with_local_intent(
                effect_id,
                owner="test",
                lease_seconds=5,
                reconciliation=_policy(),
                apply=fail,
            )
        assert control.get_effect(effect_id)["state"] == "pending"
        assert work.get_project(work.list_projects()[0].project_id).name != "changed"
        assert _counts(work) == (0, 0, 0)


@pytest.mark.parametrize("escape", ["commit", "rollback", "savepoint", "executescript"])
def test_external_intent_callback_cannot_end_control_transaction(tmp_path, escape):
    with _case(tmp_path) as (_, _, control, work, _bridge, effect_id):
        def escape_transaction(cursor, _payload):
            if escape == "commit":
                cursor.connection.commit()
            elif escape == "rollback":
                cursor.connection.rollback()
            elif escape == "savepoint":
                cursor.execute("SAVEPOINT escaped")
            else:
                cursor.executescript("SELECT 1;")
            return {}

        with pytest.raises(sqlite3.DatabaseError, match="authorized"):
            control.claim_with_local_intent(
                effect_id,
                owner="test",
                lease_seconds=5,
                reconciliation=_policy(),
                apply=escape_transaction,
            )
        assert control.get_effect(effect_id)["state"] == "pending"
        assert _counts(work) == (0, 0, 0)


def test_legacy_or_superseded_admission_never_binds_work(tmp_path):
    database = tmp_path / "shared.sqlite3"
    project_path = tmp_path / "project"
    project_path.mkdir()
    work = WorkLedgerStore(database)
    project = work.create_or_get_project(project_path)
    control = ControlLedgerStore(database)
    bridge = WorkControl(control, work)
    legacy = _admission(mode="legacy")
    bridge.admit(legacy, fence_scope="foreground-chat")
    with pytest.raises(ControlLedgerConflict, match="legacy"):
        bridge.seal(legacy, _payload(project.project_id))
    assert _counts(work) == (0, 0, 0)

    control.close()
    work.close()

    # A separate database avoids turning an immutable legacy admission into a
    # new-mode one merely for the supersede half of the assertion.
    with _case(tmp_path / "supersede") as (_, project, control, work, bridge, effect_id):
        replacement = _admission(root_suffix="two", epoch=2)
        bridge.admit(replacement, fence_scope="foreground-chat")
        assert control.get_effect(effect_id)["state"] == "cancelled"
        with pytest.raises(ControlLedgerConflict, match="no domain binding"):
            bridge.bind_dispatch_intent(
                effect_id,
                provider_run_id="fake-alpha_run-1",
                lease_seconds=5,
                reconciliation=_policy(),
            )
        assert _counts(work) == (0, 0, 0)


def test_raw_malformed_or_multi_effect_plan_is_inert(tmp_path):
    database = tmp_path / "shared.sqlite3"
    project_path = tmp_path / "project"
    project_path.mkdir()
    work = WorkLedgerStore(database)
    project = work.create_or_get_project(project_path)
    control = ControlLedgerStore(database)
    bridge = WorkControl(control, work)
    admission = _admission()
    bridge.admit(admission, fence_scope="foreground-chat")
    effect_id = bridge._identity(admission.root_id, "work:0")
    malformed = {
        **_payload(project.project_id).to_payload(),
        "origin_effect_id": "caller-forged",
    }
    control.accept(
        admission.root_id,
        chat_epoch=1,
        plan_id="raw-plan",
        effects=(
            ControlEffect(
                effect_id,
                "work",
                "work-project:" + project.project_id,
                malformed,
            ),
        ),
        evidence={"adapter": "proposal_gated_work_c1:v1"},
    )
    with pytest.raises(ControlLedgerConflict, match="payload shape"):
        bridge.bind_dispatch_intent(
            effect_id,
            provider_run_id="fake-alpha_run-1",
            lease_seconds=5,
            reconciliation=_policy(),
        )
    assert control.get_effect(effect_id)["state"] == "pending"
    assert _counts(work) == (0, 0, 0)
    control.close()
    work.close()

    # The dedicated sealer emits exactly one effect. A raw Control caller may
    # persist a wider inert plan, but WorkControl must not consume it.
    database = tmp_path / "multi.sqlite3"
    work = WorkLedgerStore(database)
    project = work.create_or_get_project(project_path)
    control = ControlLedgerStore(database)
    bridge = WorkControl(control, work)
    bridge.admit(admission, fence_scope="foreground-chat")
    effect_id = bridge._identity(admission.root_id, "work:0")
    payload = _payload(project.project_id).to_payload()
    control.accept(
        admission.root_id,
        chat_epoch=1,
        plan_id="multi-plan",
        effects=(
            ControlEffect(effect_id, "work", "work-project:" + project.project_id, payload),
            ControlEffect("second", "work", "work-project:second", payload),
        ),
        evidence={"adapter": "proposal_gated_work_c1:v1"},
    )
    with pytest.raises(ControlLedgerConflict, match="admitted source"):
        bridge.bind_dispatch_intent(
            effect_id,
            provider_run_id="fake-alpha_run-1",
            lease_seconds=5,
            reconciliation=_policy(),
        )
    assert _counts(work) == (0, 0, 0)
    control.close()
    work.close()


def test_incomplete_origin_and_other_database_fail_closed(tmp_path):
    with _case(tmp_path / "damaged") as (_, project, control, work, bridge, effect_id):
        orphan_item = work.create_work_item(project.project_id, title="damaged")
        work._connection.execute(
            "UPDATE work_items SET origin_effect_id=? WHERE work_item_id=?",
            (effect_id, orphan_item.work_item_id),
        )
        with pytest.raises(WorkLedgerConflict, match="incomplete"):
            bridge.bind_dispatch_intent(
                effect_id,
                provider_run_id="fake-alpha_run-1",
                lease_seconds=5,
                reconciliation=_policy(),
            )
        assert control.get_effect(effect_id)["state"] == "pending"
        assert _counts(work) == (1, 0, 0)

    left = WorkLedgerStore(tmp_path / "left.sqlite3")
    right = ControlLedgerStore(tmp_path / "right.sqlite3")
    try:
        with pytest.raises(ControlLedgerConflict, match="share one database"):
            WorkControl(right, left)
        with sqlite3.connect(right.path) as other:
            cursor = other.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                with pytest.raises(WorkLedgerConflict, match="another database"):
                    left.write_effect_work_item_with_attempt(
                        cursor,
                        "project",
                        title="title",
                        goal="task",
                        intent="execute",
                        instruction="task",
                        provider="fake-alpha",
                        task="task",
                        origin_effect_id="effect",
                        provider_run_id="run",
                    )
            finally:
                other.rollback()
                cursor.close()
    finally:
        right.close()
        left.close()


_CRASH_CHILD = r'''
import os, sys
from agent_host.work_ledger_store import WorkLedgerStore
from server.control_ledger import ControlLedgerStore, ReconciliationPolicy
from server.work_control import WorkControl

database, effect_id, run_id, stage = sys.argv[1:]
work = WorkLedgerStore(database)
control = ControlLedgerStore(database)
bridge = WorkControl(control, work)

def trace(sql):
    text = " ".join(sql.split()).upper()
    if ((stage == "before_claim" and text.startswith("UPDATE CONTROL_EFFECT_OUTBOX SET STATE='DISPATCHING'"))
        or (stage == "before_work" and text.startswith("INSERT INTO WORK_ITEMS"))
        or (stage == "before_operation" and text.startswith("INSERT INTO WORK_OPERATIONS"))
        or (stage == "before_attempt" and text.startswith("INSERT INTO RUN_ATTEMPTS"))
        or (stage == "before_commit" and text == "COMMIT")):
        os._exit(77)

control._db.set_trace_callback(trace)
bridge.bind_dispatch_intent(effect_id, provider_run_id=run_id, lease_seconds=5,
    reconciliation=ReconciliationPolicy("provider-submission", max_probes=2,
        interval_seconds=3, ttl_seconds=20))
if stage == "after_commit":
    os._exit(77)
raise RuntimeError("crash point not reached")
'''


@pytest.mark.parametrize(
    "stage",
    ["before_claim", "before_work", "before_operation", "before_attempt", "before_commit", "after_commit"],
)
def test_real_process_exit_leaves_control_and_work_all_or_none(tmp_path, stage):
    with _case(tmp_path) as (database, _, _control, _work, _bridge, effect_id):
        pass
    run_id = "fake-alpha_run-crash"
    result = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-c", _CRASH_CHILD, str(database), effect_id, run_id, stage],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        env={
            **os.environ,
            "PYTHONUTF8": "1",
            "AMADEUS_SESSION_DIR": str(tmp_path / "child-session"),
        },
    )
    assert result.returncode == 77, result.stdout + result.stderr
    work = WorkLedgerStore(database)
    control = ControlLedgerStore(database)
    bridge = WorkControl(control, work)
    try:
        committed = stage == "after_commit"
        assert control.get_effect(effect_id)["state"] == (
            "dispatching" if committed else "pending"
        )
        assert _counts(work) == ((1, 1, 1) if committed else (0, 0, 0))
        assert (work.get_origin_effect_binding(effect_id) is not None) == committed
        replay = bridge.bind_dispatch_intent(
            effect_id,
            provider_run_id=run_id,
            lease_seconds=5,
            reconciliation=_policy(),
        )
        assert replay["replayed"] is committed
        assert _counts(work) == (1, 1, 1)
    finally:
        control.close()
        work.close()


def _create_v1_database(path: Path) -> None:
    schema = control_ledger_module._SCHEMA
    v1 = schema.replace(
        "INSERT OR IGNORE INTO control_ledger_meta VALUES (1,3,1);",
        "INSERT OR IGNORE INTO control_ledger_meta VALUES (1,1,1);",
    ).replace(
        "kind TEXT NOT NULL CHECK(kind IN ('focus','attention','work','provider'))",
        "kind TEXT NOT NULL CHECK(kind IN ('focus','attention'))",
    )
    assert v1 != schema and "VALUES (1,1,1)" in v1
    with sqlite3.connect(path) as db:
        db.executescript(v1)
        db.execute("UPDATE control_ledger_meta SET accepting=0")
        db.execute(
            "INSERT INTO control_admissions (root_id,source_scope,fence_scope,utterance_id,chat_epoch,authority_mode,transcript_hash,lifecycle,plan_id,plan_json,accepted_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("root-u", "chat:s", "fence-u", "u", 1, "turn_decision", "hash-u", "current", "plan-u", "{}", 100, 90),
        )
        db.execute("INSERT INTO control_epoch_fences VALUES (?,?,?)", ("fence-u", 1, "root-u"))
        db.execute(
            "INSERT INTO control_effect_outbox (effect_id,root_id,ordinal,kind,target_key,payload_json,state,claim_token,claim_owner,claim_expires_at,external_id,probe_owner,max_probes,probe_count,probe_interval,unknown_ttl,next_probe_at,unknown_expires_at,reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("effect-u", "root-u", 0, "focus", "focus:u", '{"project_id":"p"}', "unknown_reconciling", "token", "owner", 110, "", "probe", 3, 1, 5, 30, 115, 140, "lost"),
        )
        db.execute(
            "INSERT INTO control_admissions (root_id,source_scope,fence_scope,utterance_id,chat_epoch,authority_mode,transcript_hash,lifecycle,plan_id,plan_json,accepted_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("root-t", "chat:t", "fence-t", "t", 1, "turn_decision", "hash-t", "current", "plan-t", "{}", 100, 90),
        )
        db.execute("INSERT INTO control_epoch_fences VALUES (?,?,?)", ("fence-t", 1, "root-t"))
        db.execute(
            "INSERT INTO control_effect_outbox (effect_id,root_id,ordinal,kind,target_key,payload_json,state,claim_token,claim_owner,external_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("effect-t", "root-t", 0, "attention", "attention:t", "{}", "terminal", "token-t", "owner-t", "local:t"),
        )
        db.execute(
            "INSERT INTO control_effect_receipts VALUES (?,?,?)",
            ("effect-t", '{"details":{},"external_id":"local:t","outcome":"succeeded"}', 101),
        )


def test_v1_migration_preserves_control_state_and_reaches_current_effect_kinds(tmp_path):
    database = tmp_path / "shared.sqlite3"
    work = WorkLedgerStore(database)
    work_version = work.schema_version
    work.close()
    _create_v1_database(database)
    control = ControlLedgerStore(database)
    try:
        assert tuple(
            control._db.execute(
                "SELECT version,accepting FROM control_ledger_meta"
            ).fetchone()
        ) == (3, 0)
        unknown = control.get_effect("effect-u")
        assert {
            key: unknown[key]
            for key in (
                "state",
                "claim_token",
                "probe_owner",
                "max_probes",
                "probe_count",
                "next_probe_at",
                "unknown_expires_at",
                "reason",
            )
        } == {
            "state": "unknown_reconciling",
            "claim_token": "token",
            "probe_owner": "probe",
            "max_probes": 3,
            "probe_count": 1,
            "next_probe_at": 115,
            "unknown_expires_at": 140,
            "reason": "lost",
        }
        assert control.get_receipt("effect-t") == {
            "details": {},
            "external_id": "local:t",
            "outcome": "succeeded",
        }
        sql = control._db.execute(
            "SELECT sql FROM sqlite_master WHERE name='control_effect_outbox'"
        ).fetchone()[0]
        assert "'focus','attention','work','provider'" in sql
        assert control._db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        control.close()
    with WorkLedgerStore(database) as reopened:
        assert reopened.schema_version == work_version


def test_failed_v1_migration_rolls_back_schema_and_data(tmp_path):
    database = tmp_path / "v1.sqlite3"
    _create_v1_database(database)
    with patch.object(
        control_ledger_module,
        "_MIGRATE_V1_TO_V2",
        ("CREATE TABLE transient(value TEXT)", "SELECT * FROM missing_table"),
    ):
        with pytest.raises(sqlite3.DatabaseError):
            ControlLedgerStore(database)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT version,accepting FROM control_ledger_meta").fetchone() == (
            1,
            0,
        )
        assert db.execute(
            "SELECT COUNT(*) FROM control_effect_outbox"
        ).fetchone()[0] == 2
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='transient'"
        ).fetchone() is None
        old_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='control_effect_outbox'"
        ).fetchone()[0]
        assert "'focus','attention','work'" not in old_sql


@pytest.mark.parametrize("starting_schema", ["fresh", "v1"])
def test_concurrent_control_constructors_converge_on_current_schema(tmp_path, starting_schema):
    database = tmp_path / "control.sqlite3"
    if starting_schema == "v1":
        _create_v1_database(database)
    gate = Barrier(8)

    def open_store(_index):
        gate.wait(timeout=5)
        store = ControlLedgerStore(database)
        try:
            return tuple(
                store._db.execute(
                    "SELECT version,accepting FROM control_ledger_meta"
                ).fetchone()
            )
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(open_store, range(8)))
    expected_accepting = 1 if starting_schema == "fresh" else 0
    assert results == [(3, expected_accepting)] * 8


_CONSTRUCTOR_CHILD = r'''
import sys, time
from pathlib import Path
from server.control_ledger import ControlLedgerStore

database, gate = sys.argv[1:]
deadline = time.monotonic() + 10
while not Path(gate).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("constructor gate timed out")
    time.sleep(0.005)
store = ControlLedgerStore(Path(database))
try:
    print(store._db.execute("SELECT version FROM control_ledger_meta").fetchone()[0])
finally:
    store.close()
'''


@pytest.mark.parametrize("starting_schema", ["fresh", "v1"])
def test_concurrent_process_control_constructors_converge_on_current_schema(tmp_path, starting_schema):
    database = tmp_path / "control.sqlite3"
    if starting_schema == "v1":
        _create_v1_database(database)
    gate = tmp_path / "go"
    environment = {
        **os.environ,
        "PYTHONUTF8": "1",
        "AMADEUS_SESSION_DIR": str(tmp_path / "child-session"),
    }
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                "-c",
                _CONSTRUCTOR_CHILD,
                str(database),
                str(gate),
            ],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=environment,
        )
        for _ in range(8)
    ]
    try:
        gate.write_text("go", encoding="utf-8")
        outputs = [process.communicate(timeout=30) for process in processes]
        for process, (stdout, stderr) in zip(processes, outputs, strict=True):
            assert process.returncode == 0, stdout + stderr
            assert stdout.strip() == "3"
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


def test_work_control_boundary_has_no_concrete_provider_shortcuts(tmp_path):
    del tmp_path
    root = Path(__file__).resolve().parents[1]
    source = (root / "server" / "work_control.py").read_text(encoding="utf-8").lower()
    assert "codex" not in source
    assert "openclaw" not in source
    assert "provider ==" not in source
