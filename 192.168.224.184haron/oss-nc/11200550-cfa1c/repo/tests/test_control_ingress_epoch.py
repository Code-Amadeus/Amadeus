"""Predeclared real-SQL tests for explicit durable ingress epoch issuance."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from server.control_ledger import ControlEffect, ControlLedgerConflict, ControlLedgerStore, ReconciliationPolicy


@pytest.fixture
def ledger(tmp_path):
    with closing(ControlLedgerStore(tmp_path / "ledger.sqlite3")) as store:
        yield store


def open_source(store, root="one", **changes):
    fields = dict(root_id=root, source_scope="chat:one", fence_scope="foreground",
        utterance_id="u-" + root, authority_mode="turn_decision", transcript_hash="same words")
    fields.update(changes)
    return store.open_admission(**fields)


def accept_one(store, row):
    return store.accept(row["root_id"], chat_epoch=row["chat_epoch"], plan_id="plan-" + row["root_id"],
        effects=(ControlEffect("effect-" + row["root_id"], "focus", "session:one", {"session_id": "one"}),), evidence={})


def test_restart_and_memory_floor_use_the_persisted_watermark(ledger):
    first = open_source(ledger, minimum_epoch=9)
    assert not first["replayed"] and first["admission"]["chat_epoch"] == 9
    with closing(ControlLedgerStore(ledger.path)) as restarted:
        second = open_source(restarted, "two")
        assert second["admission"]["chat_epoch"] == 10
    assert open_source(ledger, "three", minimum_epoch=25)["admission"]["chat_epoch"] == 25


def test_same_text_is_not_dedupe_and_other_fence_is_independent(ledger):
    first = open_source(ledger)["admission"]
    second = open_source(ledger, "two")["admission"]
    other = open_source(ledger, "app", source_scope="app:one", fence_scope="app-owner")["admission"]
    assert (first["chat_epoch"], second["chat_epoch"], other["chat_epoch"]) == (1, 2, 1)
    assert ledger.get_admission("one")["lifecycle"] == "superseded"
    assert ledger.get_admission("two")["lifecycle"] == "current"
    ledger.advance_epoch(fence_scope="foreground", expected_epoch=2)
    assert ledger.get_admission("app")["lifecycle"] == "current"
    assert ledger.get_epoch_fence("app-owner")["chat_epoch"] == 1


@pytest.mark.parametrize("same_source", [False, True])
def test_concurrent_sources_do_not_use_split_read_then_write(ledger, same_source):
    with closing(ControlLedgerStore(ledger.path)) as other, ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda index: open_source(
            ledger if index % 2 else other, "one" if same_source else f"root-{index}",
        ), range(16)))
    epochs = [result["admission"]["chat_epoch"] for result in results]
    assert sum(not result["replayed"] for result in results) == (1 if same_source else 16)
    assert sorted(set(epochs)) == ([1] if same_source else list(range(1, 17)))
    assert ledger.get_epoch_fence("foreground")["chat_epoch"] == (1 if same_source else 16)


@pytest.mark.parametrize("change", [
    {"root_id": "changed"}, {"fence_scope": "changed"},
    {"authority_mode": "legacy"}, {"transcript_hash": "changed"},
])
def test_conflicting_replay_never_moves_the_fence(ledger, change):
    original = open_source(ledger)["admission"]
    before = ledger.get_epoch_fence("foreground")
    with pytest.raises(ControlLedgerConflict):
        open_source(ledger, **{"utterance_id": "u-one", **change})
    assert ledger.get_epoch_fence("foreground") == before
    assert ledger.get_admission("one") == original


def test_root_collision_for_another_source_rolls_back_old_retirement(ledger):
    original = open_source(ledger)["admission"]
    accept_one(ledger, original)
    before = ledger.get_admission("one")
    with pytest.raises(ControlLedgerConflict):
        open_source(ledger, utterance_id="a different input")
    assert ledger.get_admission("one") == before
    assert ledger.get_effect("effect-one")["state"] == "pending"
    assert ledger.get_epoch_fence("foreground")["chat_epoch"] == 1


def test_binding_unknown_epoch_does_not_implicitly_use_the_issuance_api(ledger):
    with pytest.raises(ValueError):
        ledger.admit(root_id="one", source_scope="chat:one", fence_scope="foreground",
            utterance_id="u-one", chat_epoch=None, authority_mode="turn_decision", transcript_hash="same")
    with pytest.raises(ValueError):
        open_source(ledger, authority_mode="source_witness_v1")
    assert ledger.get_epoch_fence("foreground") is None


@pytest.mark.parametrize("state", ["pending", "superseded", "discarded", "terminal"])
def test_replay_is_checked_before_any_invalidation_or_new_epoch(ledger, state):
    original = open_source(ledger)["admission"]
    accept_one(ledger, original)
    if state == "superseded":
        open_source(ledger, "two")
    elif state == "discarded":
        ledger.advance_epoch(fence_scope="foreground", expected_epoch=1)
    elif state == "terminal":
        ledger.apply_local("effect-one", owner="test", apply=lambda cursor, payload: {"fact": "accepted"})
    before = ledger.get_epoch_fence("foreground")
    effect = ledger.get_effect("effect-one")
    receipt = ledger.get_receipt("effect-one")
    replay = open_source(ledger, minimum_epoch=999)
    assert replay["replayed"] and replay["admission"]["chat_epoch"] == 1
    assert ledger.get_epoch_fence("foreground") == before
    assert ledger.get_effect("effect-one") == effect
    assert ledger.get_receipt("effect-one") == receipt
    if state == "pending":
        assert effect["state"] == "pending" and replay["admission"]["lifecycle"] == "current"


def test_non_input_advance_is_a_cas_and_does_not_invent_a_root(ledger):
    open_source(ledger)
    second = open_source(ledger, "two")["admission"]
    with pytest.raises(ControlLedgerConflict, match="changed"):
        ledger.advance_epoch(fence_scope="foreground", expected_epoch=1)
    assert ledger.get_admission("two") == second
    advanced = ledger.advance_epoch(fence_scope="foreground", expected_epoch=2, minimum_epoch=8)
    assert advanced == {"fence_scope": "foreground", "chat_epoch": 8, "root_id": "two"}
    assert ledger.get_admission("two")["chat_epoch"] == 2
    assert ledger.get_admission("two")["lifecycle"] == "discarded"
    with closing(sqlite3.connect(ledger.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM control_admissions").fetchone()[0] == 2
    with closing(ControlLedgerStore(ledger.path)) as restarted:
        assert open_source(restarted, "three")["admission"]["chat_epoch"] == 9
    with pytest.raises(ControlLedgerConflict):
        ledger.advance_epoch(fence_scope="missing", expected_epoch=0)


def test_only_one_concurrent_invalidation_can_consume_the_expected_fence(ledger):
    open_source(ledger)
    with closing(ControlLedgerStore(ledger.path)) as other, ThreadPoolExecutor(max_workers=8) as pool:
        def advance(index):
            try:
                return (ledger if index % 2 else other).advance_epoch(fence_scope="foreground", expected_epoch=1)
            except ControlLedgerConflict:
                return None
        results = list(pool.map(advance, range(16)))
    assert sum(result is not None for result in results) == 1
    assert ledger.get_epoch_fence("foreground") == {"fence_scope": "foreground", "chat_epoch": 2, "root_id": "one"}
    assert ledger.get_admission("one")["lifecycle"] == "discarded"


@pytest.mark.parametrize("state,expected", [
    ("pending", "cancelled"), ("dispatching", "unknown_reconciling"),
    ("running", "running"), ("terminal", "terminal"),
])
def test_non_input_advance_preserves_the_existing_effect_state_contract(ledger, state, expected):
    row = open_source(ledger)["admission"]
    accept_one(ledger, row)
    if state != "pending":
        claim = ledger.claim("effect-one", owner="test", lease_seconds=10,
            reconciliation=ReconciliationPolicy("probe", max_probes=2, interval_seconds=5, ttl_seconds=20))
        token = claim["claim_token"]
        if state in {"running", "terminal"}:
            ledger.bind_external("effect-one", claim_token=token, external_id="native")
        if state == "terminal":
            ledger.record_receipt("effect-one", claim_token=token, external_id="native", outcome="succeeded", details={})
    receipt = ledger.get_receipt("effect-one")
    ledger.advance_epoch(fence_scope="foreground", expected_epoch=1)
    assert ledger.get_effect("effect-one")["state"] == expected
    assert ledger.get_receipt("effect-one") == receipt
    if state == "dispatching":
        expiry = ledger.get_effect("effect-one")["unknown_expires_at"]
        ledger.advance_epoch(fence_scope="foreground", expected_epoch=2)
        assert ledger.get_effect("effect-one")["unknown_expires_at"] == expiry


@pytest.mark.parametrize("operation", ["open", "advance"])
def test_fence_write_failure_rolls_back_retirement_and_admission(ledger, operation):
    original = open_source(ledger)["admission"]
    accept_one(ledger, original)
    with closing(sqlite3.connect(ledger.path)) as db, db:
        db.execute("CREATE TRIGGER fail_fence BEFORE UPDATE ON control_epoch_fences BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(ControlLedgerConflict, match="injected failure"):
        if operation == "open":
            open_source(ledger, "two")
        else:
            ledger.advance_epoch(fence_scope="foreground", expected_epoch=1)
    assert ledger.get_admission("one")["lifecycle"] == "current"
    assert ledger.get_effect("effect-one")["state"] == "pending"
    assert ledger.get_epoch_fence("foreground")["chat_epoch"] == 1
    with pytest.raises(ControlLedgerConflict):
        ledger.get_admission("two")


@pytest.mark.parametrize("bad", [-1, True, 1.5, None])
def test_invalid_epoch_inputs_never_mutate_the_fence(ledger, bad):
    open_source(ledger)
    before = ledger.get_epoch_fence("foreground")
    with pytest.raises(ValueError):
        open_source(ledger, "two", minimum_epoch=bad)
    with pytest.raises(ValueError):
        ledger.advance_epoch(fence_scope="foreground", expected_epoch=bad)
    assert ledger.get_epoch_fence("foreground") == before


def test_paused_mode_cannot_fall_through_via_epoch_issuance(ledger):
    original = open_source(ledger)["admission"]
    accept_one(ledger, original)
    ledger.quiesce()
    before = ledger.get_epoch_fence("foreground")
    assert open_source(ledger)["replayed"]
    with pytest.raises(ControlLedgerConflict, match="paused"):
        open_source(ledger, "two")
    with pytest.raises(ControlLedgerConflict, match="changed"):
        open_source(ledger, authority_mode="legacy")
    assert ledger.get_epoch_fence("foreground") == before
    legacy = open_source(ledger, "two", authority_mode="legacy")["admission"]
    assert legacy["chat_epoch"] == 2
    assert ledger.apply_legacy_local("two", apply=lambda cursor: {"fact": "legacy"}) == {"fact": "legacy"}


@pytest.mark.parametrize("operation", ["open", "advance"])
@pytest.mark.parametrize("inside", [False, True])
def test_real_process_loss_preserves_atomic_ingress_and_invalidation(ledger, operation, inside):
    row = open_source(ledger)["admission"]
    accept_one(ledger, row)
    script = r'''
import os, sys
from pathlib import Path
from server.control_ledger import ControlLedgerStore
path, operation, inside = sys.argv[1:]
store = ControlLedgerStore(Path(path))
if inside == 'True':
    store._db.create_function('die_now', 0, lambda: os._exit(77))
    store._db.execute("CREATE TEMP TRIGGER stop_inside AFTER UPDATE ON control_epoch_fences BEGIN SELECT die_now(); END")
if operation == 'open':
    store.open_admission(root_id='two', source_scope='chat:one', fence_scope='foreground', utterance_id='u-two', authority_mode='turn_decision', transcript_hash='same words')
else:
    store.advance_epoch(fence_scope='foreground', expected_epoch=1)
os._exit(77)
'''
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(ledger.path), operation, str(inside)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
    assert result.returncode == 77, result.stdout + result.stderr
    with closing(ControlLedgerStore(ledger.path)) as reopened:
        assert reopened.get_epoch_fence("foreground")["chat_epoch"] == (1 if inside else 2)
        assert reopened.get_effect("effect-one")["state"] == ("pending" if inside else "cancelled")
        if operation == "open":
            retried = open_source(reopened, "two")
            assert retried["replayed"] is (not inside)
            assert retried["admission"]["chat_epoch"] == 2
        else:
            assert open_source(reopened, "two")["admission"]["chat_epoch"] == (2 if inside else 3)
