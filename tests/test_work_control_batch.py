from __future__ import annotations

from dataclasses import fields
import json

import pytest

from agent_host.provider_contract import ProviderRequirements
from agent_host.work_ledger_store import WorkLedgerNotFound, WorkLedgerStore
from server.control_ledger import (
    ControlLedgerConflict,
    ControlLedgerStore,
    ReconciliationPolicy,
)
from server.provider_event_ingestion import ProviderEventIngestor
from server.turn_admission import capture_turn_admission
from server.work_control import (
    CurrentTurnSourceSpanV1,
    WorkAmendPayloadV4,
    WorkControl,
    WorkEffectPayloadV3,
)


SOURCE = "Build the alpha artifact. Then build the beta artifact."
ALPHA = "Build the alpha artifact."
BETA = "build the beta artifact."


def _admission(*, suffix: str = "batch", epoch: int = 1, source: str = SOURCE):
    admission = capture_turn_admission(
        utterance_id="utterance-" + suffix,
        turn_id="turn-" + suffix,
        session_id="session-batch",
        transcript=source,
        input_source="text",
        chat_epoch=epoch,
        pending=False,
        authority_mode="turn_decision",
    )
    assert admission is not None
    return admission


def _requirements() -> ProviderRequirements:
    return ProviderRequirements(
        task_kind="workspace_mutation",
        workspace_access="write",
        workspace_ownership="caller",
        ownership="managed",
    )


def _payload(
    admission,
    project_id: str,
    task: str,
    *,
    source: str = SOURCE,
) -> WorkEffectPayloadV3:
    start = source.index(task)
    return WorkEffectPayloadV3(
        provider="inert-provider",
        task=task,
        title=ProviderEventIngestor.task_title(task),
        project_id=project_id,
        session_id=admission.session_id,
        utterance_id=admission.utterance_id,
        turn_id=admission.turn_id,
        source_user_text=source,
        source_user_context="",
        source_context_scope=admission.dialogue_source_scope,
        source_proof=CurrentTurnSourceSpanV1.capture(
            admission,
            source,
            start=start,
            end=start + len(task),
        ),
        requirements=_requirements(),
    )


def _open(tmp_path, *, source: str = SOURCE):
    database = tmp_path / "shared.sqlite3"
    workspace = tmp_path / "project"
    workspace.mkdir()
    work = WorkLedgerStore(database)
    project = work.create_or_get_project(workspace)
    ledger = ControlLedgerStore(database)
    control = WorkControl(ledger, work)
    admission = _admission(source=source)
    control.admit(admission, fence_scope="foreground-chat")
    return ledger, work, control, project, admission


def _batch(control, admission, project_id):
    payloads = (
        _payload(admission, project_id, ALPHA),
        _payload(admission, project_id, BETA),
    )
    return payloads, control.seal_many(admission, payloads)


def _counts(work: WorkLedgerStore) -> tuple[int, int, int]:
    return tuple(
        int(work._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("work_items", "work_operations", "run_attempts")
    )


def _policy() -> ReconciliationPolicy:
    return ReconciliationPolicy(
        "provider-submission",
        max_probes=2,
        interval_seconds=3,
        ttl_seconds=20,
    )


def test_single_seal_keeps_legacy_identity_and_encoded_plan(tmp_path) -> None:
    ledger, work, control, project, admission = _open(tmp_path)
    try:
        payload = _payload(admission, project.project_id, ALPHA)
        accepted = control.seal(admission, payload)
        effect_id = control._identity(admission.root_id, "work:0")
        expected = {
            "effects": [
                {
                    "effect_id": effect_id,
                    "kind": "work",
                    "payload": payload.to_payload(),
                    "target_key": (
                        f"work-source:{admission.session_id}:{admission.utterance_id}"
                    ),
                }
            ],
            "evidence": {
                "adapter": "proposal_gated_current_turn_work:v3",
                "transcript_hash": admission.transcript_hash,
            },
        }
        stored = ledger.get_admission(admission.root_id)
        assert accepted["effect_id"] == effect_id
        assert "effect_ids" not in accepted
        assert stored["plan_id"] == control._identity(
            admission.root_id, "work-plan:v3"
        )
        assert stored["plan_json"] == json.dumps(
            expected, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    finally:
        ledger.close()
        work.close()


def test_one_acceptance_dispatches_two_independent_work_bindings(
    tmp_path, monkeypatch
) -> None:
    ledger, work, control, project, admission = _open(tmp_path)
    try:
        accept_calls = 0
        accept = ledger.accept

        def counted_accept(*args, **kwargs):
            nonlocal accept_calls
            accept_calls += 1
            return accept(*args, **kwargs)

        monkeypatch.setattr(ledger, "accept", counted_accept)
        payloads, accepted = _batch(control, admission, project.project_id)
        assert accept_calls == 1
        assert accepted["effect_count"] == 2
        assert len(set(accepted["effect_ids"])) == 2
        effects = [ledger.get_effect(effect_id) for effect_id in accepted["effect_ids"]]
        assert [effect["ordinal"] for effect in effects] == [0, 1]
        assert len({effect["target_key"] for effect in effects}) == 2

        requests = [control.provider_request(effect_id) for effect_id in accepted["effect_ids"]]
        assert [request.task for request in requests] == [ALPHA, BETA]
        bindings = [
            control.bind_dispatch_intent(
                effect_id,
                provider_run_id=f"inert-run-{ordinal}",
                lease_seconds=5,
                reconciliation=_policy(),
            )["binding"]
            for ordinal, effect_id in enumerate(accepted["effect_ids"])
        ]
        assert len({binding["work_item_id"] for binding in bindings}) == 2
        assert len({binding["operation_id"] for binding in bindings}) == 2
        assert len({binding["attempt_id"] for binding in bindings}) == 2
        assert _counts(work) == (2, 2, 2)
        for effect_id, payload, binding in zip(
            accepted["effect_ids"], payloads, bindings
        ):
            assert control.binding(effect_id) == binding
            item = work.get_work_item(binding["work_item_id"])
            attempt = work.get_attempt(binding["attempt_id"])
            assert item is not None and item.origin_effect_id == effect_id
            assert item.goal == payload.task
            assert attempt is not None and attempt.origin_effect_id == effect_id
    finally:
        ledger.close()
        work.close()


def test_exact_batch_replay_creates_no_sibling_work(tmp_path) -> None:
    ledger, work, control, project, admission = _open(tmp_path)
    try:
        payloads, first = _batch(control, admission, project.project_id)
        for ordinal, effect_id in enumerate(first["effect_ids"]):
            control.bind_dispatch_intent(
                effect_id,
                provider_run_id=f"inert-run-{ordinal}",
                lease_seconds=5,
                reconciliation=_policy(),
            )
        before = (_counts(work), tuple(ledger.get_effect(key) for key in first["effect_ids"]))
        replay = control.seal_many(admission, payloads)
        assert replay["replayed"] is True
        assert replay["effect_ids"] == first["effect_ids"]
        assert (_counts(work), tuple(ledger.get_effect(key) for key in replay["effect_ids"])) == before
    finally:
        ledger.close()
        work.close()


@pytest.mark.parametrize("tamper", ["membership", "ordinal", "source"])
def test_batch_effect_rejects_tampered_plan_membership_ordinal_or_source(
    tmp_path, tamper: str
) -> None:
    ledger, work, control, project, admission = _open(tmp_path)
    try:
        _payloads, accepted = _batch(control, admission, project.project_id)
        effect_id = accepted["effect_ids"][1]
        with ledger._transaction() as db:
            if tamper == "ordinal":
                db.execute(
                    "UPDATE control_effect_outbox SET ordinal=7 WHERE effect_id=?",
                    (effect_id,),
                )
            elif tamper == "source":
                db.execute(
                    "UPDATE control_admissions SET transcript_hash=? WHERE root_id=?",
                    ("0" * 64, admission.root_id),
                )
            else:
                plan = json.loads(
                    db.execute(
                        "SELECT plan_json FROM control_admissions WHERE root_id=?",
                        (admission.root_id,),
                    ).fetchone()[0]
                )
                plan["effects"][1]["effect_id"] = "tampered-effect"
                db.execute(
                    "UPDATE control_admissions SET plan_json=? WHERE root_id=?",
                    (
                        json.dumps(plan, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")),
                        admission.root_id,
                    ),
                )
        with pytest.raises(ControlLedgerConflict):
            control.provider_request(effect_id)
        assert _counts(work) == (0, 0, 0)
    finally:
        ledger.close()
        work.close()


def test_invalid_second_payload_leaves_no_partial_acceptance(tmp_path) -> None:
    ledger, work, control, project, admission = _open(tmp_path)
    try:
        payloads = (
            _payload(admission, project.project_id, ALPHA),
            _payload(admission, "missing-project", BETA),
        )
        with pytest.raises(WorkLedgerNotFound, match="unknown project"):
            control.seal_many(admission, payloads)
        assert ledger.get_admission(admission.root_id)["plan_id"] is None
        assert ledger.pending_effects() == []
        assert _counts(work) == (0, 0, 0)
    finally:
        ledger.close()
        work.close()


def test_overlapping_sources_and_duplicate_existing_work_mutations_fail_closed(
    tmp_path,
) -> None:
    source = "Build alpha artifact and beta artifact."
    ledger, work, control, project, admission = _open(tmp_path, source=source)
    try:
        left = _payload(
            admission, project.project_id, "Build alpha artifact", source=source
        )
        right = _payload(
            admission,
            project.project_id,
            "artifact and beta artifact.",
            source=source,
        )
        with pytest.raises(ControlLedgerConflict, match="disjoint"):
            control.seal_many(admission, (left, right))

        initial = control.seal(admission, left)
        binding = control.bind_dispatch_intent(
            initial["effect_id"],
            provider_run_id="initial-run",
            lease_seconds=5,
            reconciliation=_policy(),
        )["binding"]
        work.update_attempt(binding["attempt_id"], execution_status="succeeded")

        next_source = "Append alpha details. Then revise beta details."
        next_admission = _admission(suffix="amend", epoch=2, source=next_source)
        control.admit(next_admission, fence_scope="foreground-chat")
        bases = (
            _payload(
                next_admission,
                project.project_id,
                "Append alpha details.",
                source=next_source,
            ),
            _payload(
                next_admission,
                project.project_id,
                "revise beta details.",
                source=next_source,
            ),
        )
        amendments = tuple(
            WorkAmendPayloadV4(
                **{field.name: getattr(base, field.name)
                   for field in fields(WorkEffectPayloadV3)},
                work_item_id=binding["work_item_id"],
            )
            for base in bases
        )
        with pytest.raises(ControlLedgerConflict, match="same existing Work twice"):
            control.seal_many(next_admission, amendments)
        assert ledger.get_admission(next_admission.root_id)["plan_id"] is None
    finally:
        ledger.close()
        work.close()


def test_retired_batch_source_cannot_claim_or_create_work(tmp_path) -> None:
    ledger, work, control, project, admission = _open(tmp_path)
    try:
        _payloads, accepted = _batch(control, admission, project.project_id)
        replacement = _admission(
            suffix="replacement",
            epoch=2,
            source="A newer unrelated request.",
        )
        control.admit(replacement, fence_scope="foreground-chat")
        assert [ledger.get_effect(key)["state"] for key in accepted["effect_ids"]] == [
            "cancelled",
            "cancelled",
        ]
        with pytest.raises(ControlLedgerConflict):
            control.bind_dispatch_intent(
                accepted["effect_ids"][0],
                provider_run_id="stale-run",
                lease_seconds=5,
                reconciliation=_policy(),
            )
        assert _counts(work) == (0, 0, 0)
    finally:
        ledger.close()
        work.close()
