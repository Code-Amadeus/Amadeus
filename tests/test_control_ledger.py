"""Offline Control Ledger invariants, including real process-death boundaries."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from unittest.mock import patch

import pytest

from server.control_ledger import (
    ControlEffect,
    ControlLedgerConflict,
    ControlLedgerStore,
    ReconciliationPolicy,
)


@contextmanager
def database(path):
    with closing(sqlite3.connect(path)) as db, db:
        yield db


def admit(
    store,
    root="root-1",
    epoch=1,
    *,
    scope="chat:one",
    fence="foreground-chat",
    mode="turn_decision",
    text_hash="same words",
):
    return store.admit(
        root_id=root,
        source_scope=scope,
        fence_scope=fence,
        utterance_id="utterance-" + root,
        chat_epoch=epoch,
        authority_mode=mode,
        transcript_hash=text_hash,
    )


def effect(identity="effect-1", target="focus:one"):
    return ControlEffect(
        identity, "focus", target, {"session_id": target, "project_id": "project-one"}
    )


def accept(store, root="root-1", epoch=1, effects=None):
    return store.accept(
        root,
        chat_epoch=epoch,
        plan_id="plan-" + root,
        effects=(effect(),) if effects is None else effects,
        evidence={"grounded_by": "offline fixture"},
    )


def claim(store, identity="effect-1", policy=None):
    return store.claim(
        identity,
        owner="executor-one",
        lease_seconds=10,
        reconciliation=policy
        or ReconciliationPolicy("probe-one", max_probes=2, interval_seconds=5, ttl_seconds=20),
    )


def domain_setup(path):
    with database(path) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS domain_fixture (session_id TEXT PRIMARY KEY, project_id TEXT, applications INTEGER)"
        )


def domain_apply(db, payload):
    db.execute(
        """INSERT INTO domain_fixture VALUES (?,?,1) ON CONFLICT(session_id)
               DO UPDATE SET project_id=excluded.project_id,applications=applications+1""",
        (payload["session_id"], payload["project_id"]),
    )
    return dict(payload)


def applications(path):
    with database(path) as db:
        return db.execute("SELECT COALESCE(SUM(applications),0) FROM domain_fixture").fetchone()[0]


def test_acceptance_and_all_outbox_rows_commit_or_rollback_together(tmp_path):
    store = ControlLedgerStore(tmp_path / "control.sqlite3")
    admit(store)
    with database(store.path) as db:
        db.execute("""CREATE TRIGGER fail_second BEFORE INSERT ON control_effect_outbox
                   WHEN NEW.ordinal=1 BEGIN SELECT RAISE(ABORT,'injected failure'); END""")
    with pytest.raises(ControlLedgerConflict, match="injected failure"):
        accept(store, effects=(effect(), effect("effect-2", "focus:two")))
    assert store.get_admission("root-1")["plan_id"] is None
    with database(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM control_effect_outbox").fetchone()[0] == 0
    store.close()


def test_concurrent_acceptance_is_one_immutable_plan_not_many_effects(tmp_path):
    path = tmp_path / "control.sqlite3"
    first = ControlLedgerStore(path)
    second = ControlLedgerStore(path)
    admit(first)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda index: accept(first if index % 2 else second), range(16)))
    assert sum(not result["replayed"] for result in results) == 1
    with pytest.raises(ControlLedgerConflict, match="immutable"):
        accept(first, effects=(effect("different-effect"),))
    with database(path) as db:
        assert db.execute("SELECT COUNT(*) FROM control_effect_outbox").fetchone()[0] == 1
    first.close()
    second.close()


def test_source_identity_not_text_hash_owns_dedupe_and_replay_cannot_revive_fence(tmp_path):
    store = ControlLedgerStore(tmp_path / "control.sqlite3")
    original = admit(store)
    newer = admit(store, "root-2", 2)
    assert original["root_id"] != newer["root_id"]  # Identical wording is a new utterance.
    replay = admit(store, epoch=99)
    assert replay["chat_epoch"] == 1 and replay["lifecycle"] == "superseded"
    with pytest.raises(ControlLedgerConflict):
        accept(store)
    with pytest.raises(ControlLedgerConflict):
        admit(store, text_hash="changed transcript")
    with pytest.raises(ControlLedgerConflict):
        admit(store, mode="legacy")
    assert accept(store, "root-2", 2)["effect_count"] == 1
    store.close()


def test_no_effect_acceptance_is_durable_but_does_not_claim_no_user_intent(tmp_path):
    path = tmp_path / "control.sqlite3"
    store = ControlLedgerStore(path)
    admit(store)
    assert accept(store, effects=())["disposition"] == "no_effect_accepted"
    store.close()
    store = ControlLedgerStore(path)
    assert accept(store, effects=())["replayed"]
    assert "no_action_intent" not in store.get_admission("root-1")["plan_json"]
    store.close()


def test_legacy_cannot_bypass_frozen_mode_even_after_rollback(tmp_path):
    store = ControlLedgerStore(tmp_path / "control.sqlite3")
    admit(store)
    accept(store)
    store.quiesce()
    with pytest.raises(ControlLedgerConflict, match="legacy"):
        store.apply_legacy_local("root-1", apply=lambda _cursor: {})
    with pytest.raises(ControlLedgerConflict):
        claim(store)
    with pytest.raises(ControlLedgerConflict, match="paused"):
        admit(store, "root-2", 2)
    admit(store, "root-2", 2, mode="legacy")
    assert store.apply_legacy_local("root-2", apply=lambda _cursor: {}) == {}
    with pytest.raises(ControlLedgerConflict):
        accept(store, "root-2", 2)
    assert store.get_effect("effect-1")["state"] == "cancelled"
    store.close()


def test_only_one_external_claim_can_win_and_its_token_owns_receipts(tmp_path):
    path = tmp_path / "control.sqlite3"
    stores = [ControlLedgerStore(path), ControlLedgerStore(path)]
    admit(stores[0])
    accept(stores[0])

    def attempt(index):
        try:
            return claim(stores[index % 2])
        except ControlLedgerConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        winners = [row for row in pool.map(attempt, range(16)) if row]
    assert len(winners) == 1
    store = stores[0]
    token = winners[0]["claim_token"]
    with pytest.raises(ControlLedgerConflict):
        store.bind_external("effect-1", claim_token="wrong", external_id="external-one")
    store.bind_external("effect-1", claim_token=token, external_id="external-one")
    receipt = dict(
        claim_token=token, external_id="external-one", outcome="succeeded", details={"revision": 1}
    )
    assert not store.record_receipt("effect-1", **receipt)["replayed"]
    assert store.record_receipt("effect-1", **receipt)["replayed"]
    with pytest.raises(ControlLedgerConflict, match="immutable"):
        store.record_receipt("effect-1", **{**receipt, "outcome": "failed"})
    for store in stores:
        store.close()


@pytest.mark.parametrize(
    "state,expected",
    [
        ("pending", "cancelled"),
        ("dispatching", "unknown_reconciling"),
        ("running", "running"),
        ("terminal", "terminal"),
    ],
)
def test_supersede_respects_effect_state_and_never_rewrites_receipts(tmp_path, state, expected):
    store = ControlLedgerStore(tmp_path / "control.sqlite3")
    admit(store)
    accept(store)
    if state != "pending":
        token = claim(store)["claim_token"]
        if state in {"running", "terminal"}:
            store.bind_external("effect-1", claim_token=token, external_id="external-one")
        if state == "terminal":
            store.record_receipt(
                "effect-1",
                claim_token=token,
                external_id="external-one",
                outcome="succeeded",
                details={"fact": 1},
            )
    before = store.get_receipt("effect-1")
    admit(store, "root-2", 2)
    assert store.get_effect("effect-1")["state"] == expected
    assert store.get_receipt("effect-1") == before
    if state == "dispatching":
        with pytest.raises(ControlLedgerConflict, match="sibling"):
            accept(store, "root-2", 2, effects=(effect("effect-2"),))
        # A valid late receipt is a fact about the old effect, not a new apply.
        store.record_receipt(
            "effect-1",
            claim_token=token,
            external_id="external-one",
            outcome="succeeded",
            details={},
        )
        assert accept(store, "root-2", 2, effects=(effect("effect-2"),))["effect_count"] == 1
    store.close()


def test_unknown_reconciliation_is_bounded_owned_and_never_requeues(tmp_path):
    now = [100.0]
    store = ControlLedgerStore(tmp_path / "control.sqlite3", clock=lambda: now[0])
    admit(store)
    accept(store)
    claim(store)
    now[0] = 111.0
    store.expire_claims()
    assert store.get_effect("effect-1")["state"] == "unknown_reconciling"
    with pytest.raises(ControlLedgerConflict):
        claim(store)
    assert store.due_unknown(owner="wrong-owner") == []
    due = store.due_unknown(owner="probe-one")[0]
    probe = store.claim_probe("effect-1", owner="probe-one", expected_probe_at=due["next_probe_at"])
    store.note_probe_unresolved("effect-1", owner="probe-one", probe_attempt=probe["probe_attempt"])
    with pytest.raises(ControlLedgerConflict):
        store.claim_probe("effect-1", owner="probe-one", expected_probe_at=due["next_probe_at"])
    now[0] = 116.0
    probe = store.claim_probe("effect-1", owner="probe-one", expected_probe_at=116.0)
    store.note_probe_unresolved("effect-1", owner="probe-one", probe_attempt=probe["probe_attempt"])
    assert store.get_effect("effect-1")["state"] == "needs_user_decision"
    assert store.get_effect("effect-1")["probe_count"] == 2
    assert store.get_receipt("effect-1") is None
    store.close()


def test_unknown_expiry_escalates_even_without_a_probe(tmp_path):
    now = [100.0]
    store = ControlLedgerStore(tmp_path / "control.sqlite3", clock=lambda: now[0])
    admit(store)
    accept(store)
    claim(store)
    now[0] = 111.0
    store.expire_claims()
    now[0] = 132.0
    store.expire_claims()
    assert store.get_effect("effect-1")["state"] == "needs_user_decision"
    store.close()


def test_local_domain_writes_and_receipt_are_atomic_and_replayable(tmp_path):
    path = tmp_path / "control.sqlite3"
    store = ControlLedgerStore(path)
    domain_setup(path)
    admit(store)
    accept(store)

    def fail(db, payload):
        domain_apply(db, payload)
        raise RuntimeError("after domain write")

    with pytest.raises(RuntimeError):
        store.apply_local("effect-1", owner="focus", apply=fail)
    assert applications(path) == 0
    assert store.get_effect("effect-1")["state"] == "pending"
    assert store.get_receipt("effect-1") is None
    result = store.apply_local("effect-1", owner="focus", apply=domain_apply)
    assert result["receipt"]["outcome"] == "succeeded"
    assert applications(path) == 1
    store.close()
    store = ControlLedgerStore(path)
    assert store.apply_local("effect-1", owner="focus", apply=fail)["replayed"]
    assert applications(path) == 1
    store.close()


def test_local_apply_and_concurrent_supersede_have_one_sql_order(tmp_path):
    path = tmp_path / "control.sqlite3"
    store = ControlLedgerStore(path)
    domain_setup(path)
    admit(store)
    accept(store)
    admit(store, "root-2", 2)
    with pytest.raises(ControlLedgerConflict):
        store.apply_local("effect-1", owner="focus", apply=domain_apply)
    assert applications(path) == 0
    accept(store, "root-2", 2, effects=(effect("effect-2"),))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: store.apply_local("effect-2", owner="focus", apply=domain_apply), range(8)
            )
        )
    assert sum(not row["replayed"] for row in results) == 1
    assert applications(path) == 1
    store.close()


_CHILD = r"""
import os, sys
from pathlib import Path
from server.control_ledger import ControlLedgerStore, ControlEffect, ReconciliationPolicy
path, point = Path(sys.argv[1]), sys.argv[2]
store = ControlLedgerStore(path, clock=lambda: 100.0)
effects = (ControlEffect("effect-1", "focus", "focus:one", {"session_id":"focus:one","project_id":"one"}),)
if point == "inside_accept":
    store._db.create_function("crash", 0, lambda: os._exit(77))
    store._db.execute("CREATE TRIGGER die BEFORE INSERT ON control_effect_outbox BEGIN SELECT crash(); END")
store.accept("root-1", chat_epoch=1, plan_id="plan-root-1", effects=effects, evidence={})
if point == "after_accept":
    os._exit(77)
if point == "after_claim":
    store.claim("effect-1", owner="dead-executor", lease_seconds=10, reconciliation=ReconciliationPolicy("probe"))
    os._exit(77)
def apply(db, payload):
    db.execute("INSERT INTO domain_fixture VALUES ('focus:one','one',1)")
    if point == "inside_local_apply":
        os._exit(77)
    return {"applied":True}
store.apply_local("effect-1", owner="local", apply=apply)
os._exit(77)
"""


@pytest.mark.parametrize(
    "point",
    ["inside_accept", "after_accept", "after_claim", "inside_local_apply", "after_local_apply"],
)
def test_hard_process_death_keeps_acceptance_and_domain_commit_boundaries(tmp_path, point):
    path = tmp_path / "control.sqlite3"
    store = ControlLedgerStore(path, clock=lambda: 100.0)
    domain_setup(path)
    admit(store)
    store.close()
    completed = subprocess.run(
        [sys.executable, "-B", "-c", _CHILD, str(path), point],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 77, completed.stderr
    store = ControlLedgerStore(path, clock=lambda: 200.0)
    admission = store.get_admission("root-1")
    if point == "inside_accept":
        assert admission["plan_id"] is None
        assert applications(path) == 0
        with database(path) as db:
            assert db.execute("SELECT COUNT(*) FROM control_effect_outbox").fetchone()[0] == 0
    elif point == "after_claim":
        store.expire_claims()
        assert store.get_effect("effect-1")["state"] == "needs_user_decision"
        with pytest.raises(ControlLedgerConflict):
            claim(store)
        assert applications(path) == 0
    else:
        assert admission["plan_id"] == "plan-root-1"
        assert applications(path) == (1 if point == "after_local_apply" else 0)
        restored = store.apply_local("effect-1", owner="restored", apply=domain_apply)
        assert restored["replayed"] == (point == "after_local_apply")
        assert applications(path) == 1
        assert store.get_receipt("effect-1")["outcome"] == "succeeded"
    store.close()


def test_plan_payload_is_detached_and_unknown_effect_kind_is_rejected(tmp_path):
    store = ControlLedgerStore(tmp_path / "control.sqlite3")
    admit(store)
    payload = {"project_id": "original"}
    accept(store, effects=(ControlEffect("effect-1", "focus", "focus:one", payload),))
    payload["project_id"] = "changed"
    assert json.loads(store.get_effect("effect-1")["payload_json"])["project_id"] == "original"
    with pytest.raises(ValueError, match="unsupported Control effect kind"):
        accept(
            store,
            effects=(ControlEffect("unknown", "provider_native", "native:one", {}),),
        )
    store.close()


def test_provider_effect_uses_existing_claim_external_identity_and_terminal_receipt(tmp_path):
    path = tmp_path / "provider-control.sqlite3"
    store = ControlLedgerStore(path)
    admit(store)
    provider = ControlEffect("provider-effect", "provider", "cooperative:session-a", {
        "session_id":"session-a", "context_id":"context-a",
        "source_utterance_id":"source-a", "turn_id":"turn-a"})
    accepted = accept(store, effects=(provider,))
    assert accepted["disposition"] == "effects_accepted"
    claimed = claim(store, "provider-effect")
    assert claimed["kind"] == "provider" and claimed["state"] == "dispatching"
    store.bind_external("provider-effect", claim_token=claimed["claim_token"],
        external_id="provider-run-a")
    recorded = store.record_receipt("provider-effect", claim_token=claimed["claim_token"],
        external_id="provider-run-a", outcome="succeeded",
        details={"provider":"codex", "status":"done"})
    assert recorded["receipt"]["outcome"] == "succeeded"
    store.close()

    reopened = ControlLedgerStore(path)
    assert reopened.get_effect("provider-effect")["state"] == "terminal"
    assert reopened.get_receipt("provider-effect") == {
        "external_id":"provider-run-a", "outcome":"succeeded",
        "details":{"provider":"codex", "status":"done"}}
    reopened.close()


def test_external_binding_and_terminal_receipt_share_domain_transactions(tmp_path):
    path = tmp_path / "atomic-provider.sqlite3"
    store = ControlLedgerStore(path)
    domain_setup(path)
    admit(store)
    accept(store)
    claimed = store.claim_with_local_intent("effect-1", owner="provider-owner",
        lease_seconds=10,
        reconciliation=ReconciliationPolicy("provider-probe", ttl_seconds=20),
        apply=domain_apply)["effect"]
    assert applications(path) == 1

    def fail_after_write(cursor, payload):
        domain_apply(cursor, payload)
        raise RuntimeError("simulated local checkpoint failure")

    with pytest.raises(RuntimeError, match="checkpoint failure"):
        store.bind_external_with_local("effect-1",
            claim_token=claimed["claim_token"], external_id="provider-run",
            apply=fail_after_write)
    assert applications(path) == 1
    assert store.get_effect("effect-1")["state"] == "dispatching"
    assert store.get_effect("effect-1")["external_id"] == ""

    store.bind_external_with_local("effect-1", claim_token=claimed["claim_token"],
        external_id="provider-run", apply=domain_apply)
    assert applications(path) == 2
    with pytest.raises(RuntimeError, match="checkpoint failure"):
        store.record_receipt_with_local("effect-1",
            claim_token=claimed["claim_token"], external_id="provider-run",
            outcome="succeeded", details={"status":"done"},
            apply=fail_after_write)
    assert applications(path) == 2 and store.get_receipt("effect-1") is None
    assert store.get_effect("effect-1")["state"] == "running"

    receipt = store.record_receipt_with_local("effect-1",
        claim_token=claimed["claim_token"], external_id="provider-run",
        outcome="succeeded", details={"status":"done"}, apply=domain_apply)
    assert receipt["receipt"]["outcome"] == "succeeded"
    assert applications(path) == 3 and store.get_effect("effect-1")["state"] == "terminal"
    store.close()


def test_explicit_unknown_submission_keeps_external_identity_and_never_requeues(tmp_path):
    store = ControlLedgerStore(tmp_path / "unknown-provider.sqlite3")
    admit(store)
    accept(store)
    claimed = claim(store)
    row = store.mark_unknown("effect-1", claim_token=claimed["claim_token"],
        external_id="provider-run:append:source", reason="delivery_ambiguous")
    assert row["state"] == "unknown_reconciling"
    assert row["external_id"] == "provider-run:append:source"
    assert row["reason"] == "delivery_ambiguous"
    with pytest.raises(ControlLedgerConflict):
        claim(store)
    store.close()


def test_v2_control_schema_migrates_provider_algebra_without_changing_receipt(tmp_path):
    path = tmp_path / "control-v2.sqlite3"
    with database(path) as db:
        db.executescript("""
            CREATE TABLE control_ledger_meta (
                singleton INTEGER PRIMARY KEY, version INTEGER NOT NULL, accepting INTEGER NOT NULL);
            INSERT INTO control_ledger_meta VALUES (1,2,1);
            CREATE TABLE control_epoch_fences (
                fence_scope TEXT PRIMARY KEY, chat_epoch INTEGER NOT NULL, root_id TEXT NOT NULL);
            CREATE TABLE control_admissions (
                root_id TEXT PRIMARY KEY, source_scope TEXT NOT NULL, fence_scope TEXT NOT NULL,
                utterance_id TEXT NOT NULL, chat_epoch INTEGER NOT NULL, authority_mode TEXT NOT NULL,
                transcript_hash TEXT NOT NULL, lifecycle TEXT NOT NULL, plan_id TEXT UNIQUE,
                plan_json TEXT, accepted_at REAL, created_at REAL NOT NULL,
                UNIQUE(source_scope,utterance_id), UNIQUE(fence_scope,chat_epoch));
            CREATE TABLE control_effect_outbox (
                effect_id TEXT PRIMARY KEY,
                root_id TEXT NOT NULL REFERENCES control_admissions(root_id), ordinal INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('focus','attention','work')), target_key TEXT NOT NULL,
                payload_json TEXT NOT NULL, state TEXT NOT NULL, claim_token TEXT NOT NULL DEFAULT '',
                claim_owner TEXT NOT NULL DEFAULT '', claim_expires_at REAL, external_id TEXT NOT NULL DEFAULT '',
                probe_owner TEXT NOT NULL DEFAULT '', max_probes INTEGER NOT NULL DEFAULT 0,
                probe_count INTEGER NOT NULL DEFAULT 0, probe_interval REAL NOT NULL DEFAULT 0,
                unknown_ttl REAL NOT NULL DEFAULT 0, next_probe_at REAL, unknown_expires_at REAL,
                reason TEXT NOT NULL DEFAULT '', UNIQUE(root_id,ordinal));
            CREATE TABLE control_effect_receipts (
                effect_id TEXT PRIMARY KEY REFERENCES control_effect_outbox(effect_id),
                receipt_json TEXT NOT NULL, recorded_at REAL NOT NULL);
            INSERT INTO control_epoch_fences VALUES ('foreground-chat',1,'root-1');
            INSERT INTO control_admissions VALUES (
                'root-1','chat:one','foreground-chat','utterance-root-1',1,'turn_decision',
                'same words','current','plan-root-1','{}',1,1);
            INSERT INTO control_effect_outbox VALUES (
                'legacy-effect','root-1',0,'work','work:one','{}','terminal','claim','owner',NULL,
                'legacy-run','',0,0,0,0,NULL,NULL,'');
            INSERT INTO control_effect_receipts VALUES (
                'legacy-effect','{"details":{"kept":true},"external_id":"legacy-run","outcome":"succeeded"}',2);
        """)
    store = ControlLedgerStore(path)
    with database(path) as db:
        assert db.execute("SELECT version FROM control_ledger_meta").fetchone()[0] == 3
    assert store.get_effect("legacy-effect")["kind"] == "work"
    assert store.get_receipt("legacy-effect")["details"] == {"kept":True}
    store.close()


def test_restart_does_not_restart_the_unknown_expiry_budget(tmp_path):
    now = [100.0]
    path = tmp_path / "control.sqlite3"
    store = ControlLedgerStore(path, clock=lambda: now[0])
    admit(store)
    accept(store)
    claim(store)
    assert store.get_effect("effect-1")["unknown_expires_at"] == 130.0
    store.close()
    now[0] = 1000.0
    store = ControlLedgerStore(path, clock=lambda: now[0])
    store.expire_claims()
    assert store.get_effect("effect-1")["state"] == "needs_user_decision"
    assert store.get_effect("effect-1")["unknown_expires_at"] == 130.0
    assert store.due_unknown(owner="probe-one") == []
    store.close()


def test_control_tables_can_share_work_database_without_taking_its_schema_ownership(tmp_path):
    from agent_host.work_ledger_store import SCHEMA_VERSION, WorkLedgerStore

    path = tmp_path / "shared.sqlite3"
    work = WorkLedgerStore(path)
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = work.create_or_get_project(project_path)
    control = ControlLedgerStore(path)
    admit(control)
    accept(control, effects=())
    assert work.schema_version == SCHEMA_VERSION
    assert work.get_project(project.project_id).project_id == project.project_id
    control.close()
    work.close()
    control = ControlLedgerStore(path)
    work = WorkLedgerStore(path)
    assert accept(control, effects=())["replayed"]
    assert work.get_project(project.project_id).project_id == project.project_id
    control.close()
    work.close()


def test_plan_and_outbox_use_one_snapshot_even_when_caller_mutates_while_waiting(tmp_path):
    from server import control_ledger

    store = ControlLedgerStore(tmp_path / "control.sqlite3")
    admit(store)
    payload = {"project_id": "before", "nested": ["before"]}
    frozen = threading.Event()
    original_json = control_ledger._json

    def observe(value):
        result = original_json(value)
        if isinstance(value, dict) and "effects" in value:
            frozen.set()
        return result

    with patch.object(control_ledger, "_json", observe), ThreadPoolExecutor(max_workers=1) as pool:
        with store._lock:
            job = pool.submit(
                accept, store, effects=(ControlEffect("effect-1", "focus", "focus:one", payload),)
            )
            assert frozen.wait(timeout=3)
            payload["project_id"] = "after"
            payload["nested"].append("after")
        job.result(timeout=3)
    accepted = json.loads(store.get_admission("root-1")["plan_json"])["effects"][0]["payload"]
    outbox = json.loads(store.get_effect("effect-1")["payload_json"])
    assert accepted == outbox == {"project_id": "before", "nested": ["before"]}
    store.close()


@pytest.mark.parametrize("escape", ["commit", "rollback", "executescript", "savepoint"])
@pytest.mark.parametrize("mode", ["turn_decision", "legacy"])
def test_local_domain_callback_cannot_end_the_guarded_transaction(tmp_path, escape, mode):
    path = tmp_path / "control.sqlite3"
    store = ControlLedgerStore(path)
    domain_setup(path)
    admit(store, mode=mode)
    if mode == "turn_decision":
        accept(store)

    def escaped(cursor, payload):
        domain_apply(cursor, payload)
        if escape == "commit":
            cursor.connection.commit()
        elif escape == "rollback":
            cursor.connection.rollback()
        elif escape == "executescript":
            cursor.executescript("SELECT 1;")
        else:
            cursor.execute("SAVEPOINT caller_owned")
        raise AssertionError("transaction control unexpectedly succeeded")

    with pytest.raises(sqlite3.DatabaseError, match="authorized"):
        if mode == "turn_decision":
            store.apply_local("effect-1", owner="focus", apply=escaped)
        else:
            store.apply_legacy_local("root-1", apply=lambda cursor: escaped(cursor, effect().payload))
    assert applications(path) == 0
    if mode == "turn_decision":
        assert store.get_effect("effect-1")["state"] == "pending"
        assert store.get_receipt("effect-1") is None
        assert not store.apply_local("effect-1", owner="focus", apply=domain_apply)["replayed"]
    else:
        store.apply_legacy_local("root-1", apply=lambda cursor: domain_apply(cursor, effect().payload))
        assert applications(path) == 1
    store.close()


def test_probe_budget_is_claimed_before_io_even_with_competing_workers(tmp_path):
    now = [100.0]
    path = tmp_path / "control.sqlite3"
    stores = [ControlLedgerStore(path, clock=lambda: now[0]) for _ in range(2)]
    admit(stores[0])
    accept(stores[0])
    claim(stores[0])
    now[0] = 111.0
    stores[0].expire_claims()

    def attempt(index):
        try:
            return stores[index % 2].claim_probe(
                "effect-1", owner="probe-one", expected_probe_at=now[0]
            )
        except ControlLedgerConflict:
            return None

    for expected_attempt in (1, 2):
        with ThreadPoolExecutor(max_workers=8) as pool:
            tickets = [ticket for ticket in pool.map(attempt, range(16)) if ticket]
        assert len(tickets) == 1 and tickets[0]["probe_attempt"] == expected_attempt
        now[0] += 5
    # Both workers died without reporting their result. Attempts are consumed,
    # not just counted after an arbitrary number of physical probes.
    assert stores[0].due_unknown(owner="probe-one") == []
    assert attempt(0) is None
    now[0] = 131.0
    stores[0].expire_claims()
    assert stores[0].get_effect("effect-1")["state"] == "needs_user_decision"
    for store in stores:
        store.close()


def test_epoch_owner_is_not_confused_with_provenance_scope(tmp_path):
    store = ControlLedgerStore(tmp_path / "control.sqlite3")
    admit(store, scope="chat:session-one")
    accept(store)
    admit(store, "root-2", 2, scope="chat:session-two")
    assert store.get_effect("effect-1")["state"] == "cancelled"
    assert store.get_admission("root-1")["lifecycle"] == "superseded"
    accept(store, "root-2", 2, effects=(effect("effect-2", "focus:two"),))
    admit(store, "app-root", 1, scope="app:app-one", fence="app-controller-one")
    assert store.get_admission("root-2")["lifecycle"] == "current"
    assert store.get_epoch_fence("foreground-chat")["root_id"] == "root-2"
    assert store.get_epoch_fence("app-controller-one")["root_id"] == "app-root"
    with pytest.raises(ControlLedgerConflict):
        admit(store, scope="chat:session-one", fence="different-owner")
    store.close()


def test_unknown_schema_is_rejected_before_schema_or_journal_changes(tmp_path):
    path = tmp_path / "future.sqlite3"
    with database(path) as db:
        db.execute("CREATE TABLE control_ledger_meta (version INTEGER)")
        db.execute("INSERT INTO control_ledger_meta VALUES (99)")
    before = path.read_bytes()
    with pytest.raises(ControlLedgerConflict, match="unsupported"):
        ControlLedgerStore(path)
    assert path.read_bytes() == before
    with database(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("control_ledger_meta",)
        ]
