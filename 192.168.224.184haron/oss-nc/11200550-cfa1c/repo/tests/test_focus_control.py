"""Isolated real-domain Focus acceptance; not a production authority canary."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerNotFound, WorkLedgerStore
from core.turn_coordinator import TurnCoordinator
from server.control_decision import ControlDecision, ControlDecisionEntry
from server.control_ledger import ControlLedgerConflict, ControlLedgerStore
from server.focus_control import FocusControl
from server.reference_catalog import TypedReferenceCandidate
from server.turn_admission import capture_turn_admission
from server.work_destination_service import WorkDestinationService


@pytest.fixture
def context(tmp_path):
    roots = [tmp_path / name for name in ("a", "b")]
    for root in roots:
        root.mkdir()
    path = tmp_path / "ledger.sqlite3"
    with WorkLedgerStore(path) as store, closing(ControlLedgerStore(path)) as ledger:
        a, b = [store.create_or_get_project(root) for root in roots]
        allowed = {a.canonical_path, b.canonical_path}
        destination = WorkDestinationService(store, registry_check=lambda value: value in allowed)
        work = store.create_work_item(a.project_id, title="Existing work")
        destination.bind_session_context("voice", a.project_id, work_item_id=work.work_item_id)
        destination.set_session_project_feedback("voice", status="info", message="old feedback")
        control = FocusControl(ledger, destination, fence_scope="foreground", clock=lambda: 300.0)
        yield SimpleNamespace(
            path=path, store=store, ledger=ledger, destination=destination,
            a=a, b=b, allowed=allowed, control=control, turns=TurnCoordinator(),
        )


def admitted(context, *, turn="turn-1", epoch=1, session="voice", mode="turn_decision"):
    grant = context.turns.open_turn(turn_id=turn, local_next_epoch=epoch, session_id=session)
    record = capture_turn_admission(
        utterance_id=turn, turn_id=turn, session_id=session, transcript="Choose destination",
        chat_epoch=grant["chat_epoch"], authority_mode=mode,
    )
    context.control.admit(record)
    return record


def decision(project=None):
    return ControlDecision(status="ok", entries=(ControlDecisionEntry(
        proposal_index=0,
        control={"provider": "codex", "intent": "focus", "subject": "project"} if project else {"provider": "codex", "intent": "focus"},
        reference_candidates=(TypedReferenceCandidate("project", project.project_id, project.name, "persistent"),) if project else None,
        session_context="bind" if project else "clear",
        reference_kind="project" if project else "none",
        workspace_effect="none",
    ),))


@pytest.mark.parametrize("clear", [False, True])
def test_explicit_durable_ingress_grant_joins_the_real_focus_domain_once(context, clear):
    observed = capture_turn_admission(
        utterance_id="input", turn_id="turn", session_id="voice", transcript="Choose destination",
        authority_mode="turn_decision",
    )
    with pytest.raises(ControlLedgerConflict, match="confirmed, mode-bound"):
        context.control.admit(observed)
    fields = dict(
        root_id=observed.root_id, source_scope=observed.dialogue_source_scope,
        fence_scope="foreground", utterance_id=observed.utterance_id,
        authority_mode=observed.authority_mode, transcript_hash=observed.transcript_hash,
    )
    opened = context.ledger.open_admission(**fields, minimum_epoch=12)
    assert not opened["replayed"] and opened["admission"]["chat_epoch"] == 12
    assert observed.chat_epoch is None  # observation is not silently upgraded
    granted = replace(observed, chat_epoch=opened["admission"]["chat_epoch"])
    assert context.control.admit(granted) == opened["admission"]
    chosen = decision(None if clear else context.b)
    before = domain_rows(context)
    sealed = context.control.seal(granted, chosen)
    assert domain_rows(context) == before
    replay = context.ledger.open_admission(**fields, minimum_epoch=999)
    assert replay["replayed"] and replay["admission"]["chat_epoch"] == 12
    assert context.ledger.get_effect(sealed["effect_id"])["state"] == "pending"
    with patch.object(WorkLedgerStore, "write_session_context", wraps=WorkLedgerStore.write_session_context) as write:
        first = context.control.apply(sealed["effect_id"])
        again = context.control.apply(sealed["effect_id"])
        assert not first["replayed"] and again["replayed"]
        assert first["receipt"] == again["receipt"]
        write.assert_called_once()
    assert context.destination.session_project("voice") == ("" if clear else context.b.project_id)
    assert context.store.get_session_work_context("voice") is None


def domain_rows(context):
    return context.store.get_conversation_binding("voice"), context.store.get_session_work_context("voice")


@pytest.mark.parametrize("clear", [False, True])
def test_seal_then_apply_uses_real_context_once_and_returns_a_durable_receipt(context, clear):
    record = admitted(context)
    chosen = decision(None if clear else context.b)
    before = domain_rows(context)
    work = context.store.list_work_items()
    sealed = context.control.seal(record, chosen)
    assert domain_rows(context) == before
    assert context.ledger.get_effect(sealed["effect_id"])["state"] == "pending"
    with patch.object(WorkLedgerStore, "write_session_context", wraps=WorkLedgerStore.write_session_context) as write:
        first = context.control.apply(sealed["effect_id"])
        assert not first["replayed"]
        assert first["receipt"] == context.ledger.get_receipt(sealed["effect_id"])
        assert first["receipt"]["details"]["project_id"] == ("" if clear else context.b.project_id)
        assert context.destination.session_project("voice") == ("" if clear else context.b.project_id)
        assert context.store.get_session_work_context("voice") is None
        # A replay returns the old immutable fact, not a projection claiming it
        # has just changed today's destination back to the old target.
        context.destination.set_session_project("voice", context.a.project_id)
        again = context.control.seal(replace(record, turn_id="replay-alias", chat_epoch=10), chosen)
        replay = context.control.apply(again["effect_id"])
        assert replay["replayed"] and replay["receipt"] == first["receipt"]
        assert write.call_count == 2  # one receipt application, one later ordinary change
        assert context.destination.session_project("voice") == context.a.project_id
    assert context.store.list_work_items() == work


def test_accepted_target_cannot_change_and_terminal_replay_does_not_revalidate_old_path(context):
    record = admitted(context)
    first = context.control.seal(record, decision(context.b))
    context.control.apply(first["effect_id"])
    context.allowed.clear()
    assert context.control.seal(record, decision(context.b))["replayed"]
    assert context.control.apply(first["effect_id"])["replayed"]
    with pytest.raises(ControlLedgerConflict, match="immutable"):
        context.control.seal(record, decision(context.a))


def test_empty_project_identity_in_a_bind_cannot_become_draft_clear(context):
    record = admitted(context)
    chosen = decision(replace(context.b, project_id=""))
    before = domain_rows(context)
    with pytest.raises(ControlLedgerConflict, match="candidate"):
        context.control.seal(record, chosen)
    assert context.ledger.pending_effects() == []
    assert domain_rows(context) == before


def test_concurrent_terminal_receipt_wins_over_a_stale_preflight_failure(context):
    record = admitted(context)
    sealed = context.control.seal(record, decision(context.b))
    available = context.destination.available_project
    other_result = []

    def complete_then_revoke(project_id):
        # The outer apply has already observed pending. Another applicant
        # commits before this availability read; terminal replay is read-only.
        with patch.object(context.destination, "available_project", side_effect=available):
            other_result.append(context.control.apply(sealed["effect_id"]))
        context.allowed.remove(context.b.canonical_path)
        return available(project_id)

    with patch.object(context.destination, "available_project", side_effect=complete_then_revoke):
        replay = context.control.apply(sealed["effect_id"])
    assert replay["replayed"] and replay["receipt"] == other_result[0]["receipt"]


@pytest.mark.parametrize("change", ["unknown_epoch", "pending", "unbound_mode", "wrong_session", "non_chat_scope"])
def test_observational_or_mismatched_sources_cannot_enter_control_acceptance(context, change):
    record = capture_turn_admission(
        utterance_id="u", turn_id="t", session_id="voice", transcript="Focus", chat_epoch=1,
        authority_mode="turn_decision",
    )
    record = replace(record, **{
        "unknown_epoch": {"chat_epoch": None},
        "pending": {"pending": True},
        "unbound_mode": {"authority_mode": "source_witness_v1"},
        "wrong_session": {"session_id": "other"},
        "non_chat_scope": {"dialogue_source_scope": "auip:app"},
    }[change])
    with pytest.raises(ControlLedgerConflict):
        context.control.admit(record)
    with pytest.raises(ControlLedgerConflict, match="unknown control_admissions"):
        context.ledger.get_admission(record.root_id)


@pytest.mark.parametrize("shape", ["unavailable", "empty", "compound", "execute", "payload", "ambiguous", "zero_match", "work_item", "workspace_write"])
def test_unsupported_or_ambiguous_decision_never_becomes_a_focus_effect(context, shape):
    record = admitted(context)
    valid = decision(context.b)
    entry = valid.entries[0]
    invalid = {
        "unavailable": replace(valid, status="unavailable"),
        "empty": replace(valid, entries=()),
        "compound": replace(valid, entries=(entry, replace(entry, proposal_index=1))),
        "execute": replace(valid, entries=(replace(entry, control={"intent": "execute"}),)),
        "payload": replace(valid, entries=(replace(entry, control={"intent": "focus", "task": "also execute"}),)),
        "ambiguous": replace(valid, entries=(replace(entry, reference_candidates=entry.reference_candidates * 2),)),
        "zero_match": replace(valid, entries=(replace(entry, reference_candidates=()),)),
        "work_item": replace(valid, entries=(replace(entry, reference_candidates=(TypedReferenceCandidate("work_item", "work", "Work", "session_draft"),)),)),
        "workspace_write": replace(valid, entries=(replace(entry, workspace_effect="write"),)),
    }[shape]
    before = domain_rows(context)
    with pytest.raises(ControlLedgerConflict):
        context.control.seal(record, invalid)
    assert context.ledger.pending_effects() == []
    assert domain_rows(context) == before
    # Unsupported new-mode work cannot use the legacy adapter to obtain the effect.
    with pytest.raises(ControlLedgerConflict, match="legacy commit"):
        context.control.apply_legacy(record, decision(context.a))


def test_project_availability_is_checked_before_seal_and_again_before_apply(context):
    record = admitted(context)
    context.allowed.remove(context.b.canonical_path)
    with pytest.raises(WorkLedgerConflict, match="trusted"):
        context.control.seal(record, decision(context.b))
    context.allowed.add(context.b.canonical_path)
    sealed = context.control.seal(record, decision(context.b))
    before = domain_rows(context)
    context.allowed.remove(context.b.canonical_path)
    with pytest.raises(WorkLedgerConflict, match="trusted"):
        context.control.apply(sealed["effect_id"])
    assert domain_rows(context) == before
    assert context.ledger.get_effect(sealed["effect_id"])["state"] == "pending"
    assert context.ledger.get_receipt(sealed["effect_id"]) is None


def test_sql_commit_rechecks_the_exact_project_path_validated_before_lock(context):
    record = admitted(context)
    sealed = context.control.seal(record, decision(context.b))
    available = context.destination.available_project
    before = domain_rows(context)

    def race(project_id):
        checked = available(project_id)
        context.store._connection.execute(
            "UPDATE projects SET canonical_path=? WHERE project_id=?",
            (str(Path(checked.canonical_path) / "replacement"), project_id),
        )
        return checked

    with patch.object(context.destination, "available_project", side_effect=race):
        with pytest.raises(ControlLedgerConflict, match="changed after grounding"):
            context.control.apply(sealed["effect_id"])
    assert domain_rows(context) == before
    assert context.ledger.get_receipt(sealed["effect_id"]) is None


def test_next_foreground_session_invalidates_old_focus_without_retargeting_it(context):
    old = admitted(context)
    sealed = context.control.seal(old, decision(context.b))
    admitted(context, turn="new", epoch=2, session="other")
    with pytest.raises(ControlLedgerConflict, match="stale"):
        context.control.apply(sealed["effect_id"])
    assert context.destination.session_project("voice") == context.a.project_id
    assert context.store.get_conversation_binding("other") is None


@pytest.mark.parametrize("operation", ["load", "delete", "failed_delete"])
def test_session_activation_boundary_can_durably_retire_pending_focus(context, tmp_path, monkeypatch, operation):
    # Explicit isolated configuration, not production lifecycle wiring. A file
    # failure after retirement must not resurrect the previous effect authority.
    from core import session_manager as sm

    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    sm.create_session("voice")
    sm.conversation_history.add_user("original conversation")
    sm.create_session("other", activate=False)
    record = admitted(context)
    sealed = context.control.seal(record, decision(context.b))
    observed = []

    def retire(old, new):
        observed.append((old, new, sm.conversation_history.dialog.copy()))
        context.ledger.discard(record.root_id)

    sm.configure_activation_guard(retire)
    if operation == "load":
        assert sm.load_session("other")[0]
        assert sm.get_current_session_id() == "other"
    else:
        if operation == "failed_delete":
            def refuse_remove(path):
                raise OSError("file busy")

            monkeypatch.setattr(sm.os, "remove", refuse_remove)
        assert sm.delete_session("voice") is (operation == "delete")
        assert sm.get_current_session_id() == (None if operation == "delete" else "voice")
    assert observed == [("voice", "other" if operation == "load" else None, [
        {"role": "user", "content": "original conversation"},
    ])]
    # Reopen the actual SQLite owner: invalidation is not an in-memory marker.
    with closing(ControlLedgerStore(context.path)) as reopened:
        assert reopened.get_admission(record.root_id)["lifecycle"] == "discarded"
        assert reopened.get_effect(sealed["effect_id"])["state"] == "cancelled"
        control = FocusControl(reopened, context.destination, fence_scope="foreground")
        with pytest.raises(ControlLedgerConflict):
            control.apply(sealed["effect_id"])
        assert reopened.get_receipt(sealed["effect_id"]) is None
    assert context.destination.session_project("voice") == context.a.project_id
    assert context.store.get_conversation_binding("other") is None


def test_quiesce_preserves_new_mode_tombstone_and_only_new_legacy_turn_can_write(context):
    old = admitted(context)
    sealed = context.control.seal(old, decision(context.b))
    context.ledger.quiesce()
    with pytest.raises(ControlLedgerConflict):
        context.control.apply(sealed["effect_id"])
    with pytest.raises(ControlLedgerConflict, match="legacy commit"):
        context.control.apply_legacy(old, decision(context.b))
    with pytest.raises(ControlLedgerConflict, match="changed"):
        context.control.admit(replace(old, authority_mode="legacy"))
    new = admitted(context, turn="new", epoch=2, mode="legacy")
    with pytest.raises(ControlLedgerConflict, match="legacy admission"):
        context.control.seal(new, decision(context.b))
    context.control.apply_legacy(new, decision(context.b))
    assert context.destination.session_project("voice") == context.b.project_id
    assert context.ledger.get_effect(sealed["effect_id"])["state"] == "cancelled"


def test_legacy_fence_is_rechecked_in_the_same_transaction_as_domain_write(context):
    old = admitted(context, mode="legacy")
    before = domain_rows(context)
    available = context.destination.available_project

    def race(project_id):
        checked = available(project_id)
        admitted(context, turn="new", epoch=2)
        return checked

    with patch.object(context.destination, "available_project", side_effect=race):
        with pytest.raises(ControlLedgerConflict, match="legacy commit"):
            context.control.apply_legacy(old, decision(context.b))
    assert domain_rows(context) == before


def test_parallel_applicants_and_reopened_adapter_share_one_receipt(context):
    record = admitted(context)
    sealed = context.control.seal(record, decision(context.b))
    with closing(ControlLedgerStore(context.path)) as second:
        other = FocusControl(second, context.destination, fence_scope="foreground")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(
                lambda index: (context.control if index % 2 else other).apply(sealed["effect_id"]),
                range(8),
            ))
    assert sum(not result["replayed"] for result in results) == 1
    assert all(result["receipt"] == results[0]["receipt"] for result in results)


@pytest.mark.parametrize("boundary", ["before_receipt", "after_commit"])
def test_process_death_through_the_adapter_recovers_one_real_focus(context, boundary):
    record = admitted(context)
    sealed = context.control.seal(record, decision(context.b))
    before = domain_rows(context)
    script = r"""
import os, sys
from pathlib import Path
from agent_host.work_ledger_store import WorkLedgerStore
from server.control_ledger import ControlLedgerStore
from server.focus_control import FocusControl
from server.work_destination_service import WorkDestinationService
path, effect_id, boundary = sys.argv[1:]
store = WorkLedgerStore(path)
ledger = ControlLedgerStore(Path(path))
control = FocusControl(ledger, WorkDestinationService(store, registry_check=lambda _: True), fence_scope="foreground")
if boundary == "before_receipt":
    ledger._receipt = lambda *a, **kw: os._exit(77)
control.apply(effect_id)
os._exit(77)
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script, str(context.path), sealed["effect_id"], boundary],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 77, result.stdout + result.stderr
    if boundary == "before_receipt":
        assert domain_rows(context) == before
        assert context.ledger.get_receipt(sealed["effect_id"]) is None
    applied = context.control.apply(sealed["effect_id"])
    assert applied["replayed"] is (boundary == "after_commit")
    assert context.destination.session_project("voice") == context.b.project_id


def test_different_database_and_unknown_project_do_not_produce_acceptance(context, tmp_path):
    with closing(ControlLedgerStore(tmp_path / "wrong.sqlite3")) as wrong:
        with pytest.raises(ControlLedgerConflict, match="share one database"):
            FocusControl(wrong, context.destination, fence_scope="foreground")
    record = admitted(context)
    nonexistent = replace(context.b, project_id="project_missing")
    with pytest.raises(WorkLedgerNotFound):
        context.control.seal(record, decision(nonexistent))
    assert context.ledger.pending_effects() == []
