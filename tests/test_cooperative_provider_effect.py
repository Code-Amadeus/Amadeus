from pathlib import Path

import pytest

from agent_host.provider_types import ProviderRunIntakeAuthority, ProviderRunIntakeReceipt
from server.control_ledger import ControlLedgerConflict, ControlLedgerStore
from server.cooperative_provider_effect import (
    CooperativeProviderEffectIntent,
    CooperativeProviderEffectLedger,
)
from server.turn_admission import capture_turn_admission


def admitted(store, *, source, turn, epoch, session="session-a"):
    admission = capture_turn_admission(utterance_id=source, turn_id=turn,
        session_id=session, transcript="accepted text", chat_epoch=epoch,
        authority_mode="turn_decision")
    assert admission is not None
    store.admit(root_id=admission.root_id, source_scope=admission.dialogue_source_scope,
        fence_scope="cooperative", utterance_id=admission.utterance_id,
        chat_epoch=epoch, authority_mode="turn_decision",
        transcript_hash=admission.transcript_hash)
    return admission


def intent(operation, *, source, turn, token="binding-a", run_id=None):
    return CooperativeProviderEffectIntent(operation=operation,
        session_id="session-a", context_id="context-a", binding_token=token,
        source_utterance_id=source, turn_id=turn, provider="codex",
        run_id=("" if operation == "start" else run_id or "run-a"),
        workspace="C:/workspace")


def test_start_append_and_interrupt_have_distinct_external_receipts(tmp_path):
    store = ControlLedgerStore(tmp_path/"control.sqlite3")
    effects = CooperativeProviderEffectLedger(store)

    start_admission = admitted(store, source="source-start", turn="turn-start", epoch=1)
    start_intent = intent("start", source="source-start", turn="turn-start")
    start = effects.accept_and_claim(start_admission, start_intent)
    effects.bind_start(start["effect"], start_intent, run_id="run-a")
    effects.settle(start["effect"], start_intent, run_id="run-a", outcome="succeeded",
        details={"status":"done"})

    append_admission = admitted(store, source="source-append", turn="turn-append", epoch=2)
    append_intent = intent("append", source="source-append", turn="turn-append")
    append = effects.accept_and_claim(append_admission, append_intent)
    effects.settle(append["effect"], append_intent, run_id="run-a", outcome="succeeded",
        details={"state":"delivered"})

    stop_admission = admitted(store, source="source-stop", turn="turn-stop", epoch=3)
    stop_intent = intent("interrupt", source="source-stop", turn="turn-stop")
    stop = effects.accept_and_claim(stop_admission, stop_intent)
    effects.settle(stop["effect"], stop_intent, run_id="run-a", outcome="succeeded",
        details={"state":"stopped"})

    assert store.get_receipt(start["effect"]["effect_id"])["external_id"] == "run-a"
    assert store.get_receipt(append["effect"]["effect_id"])["external_id"] == (
        "run-a:append:source-append")
    assert store.get_receipt(stop["effect"]["effect_id"])["external_id"] == (
        "run-a:interrupt:source-stop")
    store.close()


def test_unknown_append_does_not_block_later_input_or_stop_on_the_same_binding(tmp_path):
    store = ControlLedgerStore(tmp_path/"control.sqlite3")
    effects = CooperativeProviderEffectLedger(store)
    first_admission = admitted(store, source="source-one", turn="turn-one", epoch=1)
    first = effects.accept_and_claim(first_admission,
        intent("append", source="source-one", turn="turn-one"))
    assert first["effect"]["state"] == "dispatching"

    second_admission = admitted(store, source="source-two", turn="turn-two", epoch=2)
    second = effects.accept_and_claim(second_admission,
        intent("append", source="source-two", turn="turn-two"))
    stop_admission = admitted(store, source="source-stop", turn="turn-stop", epoch=3)
    stop = effects.accept_and_claim(stop_admission,
        intent("interrupt", source="source-stop", turn="turn-stop"))
    assert len({first["effect"]["target_key"], second["effect"]["target_key"],
        stop["effect"]["target_key"]}) == 3
    assert store.get_effect(first["effect"]["effect_id"])["state"] == (
        "unknown_reconciling")
    assert store.get_effect(second["effect"]["effect_id"])["state"] == (
        "unknown_reconciling")
    assert store.get_effect(stop["effect"]["effect_id"])["state"] == "dispatching"
    store.close()


def test_admission_identity_mismatch_and_no_effect_settlement(tmp_path):
    store = ControlLedgerStore(tmp_path/"control.sqlite3")
    effects = CooperativeProviderEffectLedger(store)
    admission = admitted(store, source="source-a", turn="turn-a", epoch=1)
    with pytest.raises(ControlLedgerConflict, match="does not match"):
        effects.accept_and_claim(admission,
            intent("start", source="source-a", turn="different-turn"))
    assert store.get_admission(admission.root_id)["plan_id"] is None
    settled = effects.accept_no_effect(admission, reason="conversation_only")
    assert settled["disposition"] == "no_effect_accepted"
    assert store.pending_effects() == []
    store.close()


def test_provider_effect_identity_and_payload_are_stable_across_restart(tmp_path):
    path = Path(tmp_path)/"control.sqlite3"
    store = ControlLedgerStore(path)
    effects = CooperativeProviderEffectLedger(store)
    admission = admitted(store, source="source-a", turn="turn-a", epoch=1)
    expected = intent("start", source="source-a", turn="turn-a")
    accepted = effects.accept_and_claim(admission, expected)
    effect_id = accepted["effect"]["effect_id"]
    store.close()

    reopened = ControlLedgerStore(path)
    row = reopened.get_effect(effect_id)
    assert row["target_key"] == expected.target_key
    assert '"source_utterance_id":"source-a"' in row["payload_json"]
    replay = CooperativeProviderEffectLedger(reopened).accept_and_claim(admission, expected)
    assert replay["replayed"] is True and replay["effect"]["effect_id"] == effect_id
    reopened.close()


def test_provider_intake_authority_allows_non_work_effect_but_not_partial_work_lineage():
    authority = ProviderRunIntakeAuthority("provider-effect",
        kind="cooperative_provider_effect")
    receipt = ProviderRunIntakeReceipt(effect_id=authority.effect_id, run_id="run-a")
    assert receipt.work_item_id == receipt.operation_id == receipt.attempt_id == ""
    with pytest.raises(ValueError, match="lineage must be complete"):
        ProviderRunIntakeReceipt(effect_id="provider-effect", run_id="run-a",
            work_item_id="work-only")


def test_legacy_effect_defaults_source_binding_to_its_target_context():
    legacy = {"operation":"start", "session_id":"session-a",
        "context_id":"context-a", "binding_token":"binding-a",
        "source_utterance_id":"source-a", "turn_id":"turn-a",
        "provider":"codex", "run_id":"", "workspace":"C:/workspace"}
    restored = CooperativeProviderEffectIntent(**legacy)
    explicit = CooperativeProviderEffectIntent(**legacy,
        source_binding_context_id="context-a")
    assert restored.source_binding_context_id == "context-a"
    assert restored.target_key == explicit.target_key
