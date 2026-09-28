from __future__ import annotations

from dataclasses import replace
import json

import pytest

from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_types import ProviderRunIntakeAuthority
from agent_host.work_ledger_types import CompletionDecision
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from server.control_ledger import (
    ControlEffect,
    ControlLedgerConflict,
    ControlLedgerStore,
    ReconciliationPolicy,
)
from server.provider_event_ingestion import ProviderEventIngestor
from server.turn_admission import capture_turn_admission
from server.work_control import (
    CurrentTurnSourceSpanV1,
    WorkControl,
    WorkEffectPayloadV2,
    WorkEffectPayloadV3,
)


def _admission(text: str, *, suffix: str = "one"):
    admission = capture_turn_admission(
        utterance_id="utterance-source-" + suffix,
        turn_id="turn-source-" + suffix,
        session_id="session-source",
        transcript=text,
        input_source="voice",
        chat_epoch=1,
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


def _v3_payload(
    project_id: str,
    source: str,
    *,
    task: str | None = None,
    suffix: str = "one",
    source_user_context: str = "User: prior assignment evidence",
) -> WorkEffectPayloadV3:
    admission = _admission(source, suffix=suffix)
    selected = task if task is not None else source.strip()
    start = source.index(selected)
    proof = CurrentTurnSourceSpanV1.capture(
        admission,
        source,
        start=start,
        end=start + len(selected),
    )
    return WorkEffectPayloadV3(
        provider="generic-source-provider",
        task=selected,
        title=ProviderEventIngestor.task_title(selected),
        project_id=project_id,
        session_id="session-source",
        utterance_id=admission.utterance_id,
        turn_id=admission.turn_id,
        source_user_text=source,
        source_user_context=source_user_context,
        source_context_scope="chat:session-source",
        source_proof=proof,
        requirements=_requirements(),
    )


def _stores(tmp_path):
    database = tmp_path / "shared.sqlite3"
    workspace = tmp_path / "project"
    workspace.mkdir()
    work = WorkLedgerStore(database)
    project = work.create_or_get_project(workspace)
    ledger = ControlLedgerStore(database)
    return ledger, work, project


def test_source_span_round_trip_preserves_exact_unicode_and_repeated_occurrence() -> None:
    source = "  🙂e\u0301 做这个。\r\n再说👩\u200d💻e\u0301，做这个。  "
    admission = _admission(source)
    selected = "做这个。"
    start = source.rindex(selected)
    proof = CurrentTurnSourceSpanV1.capture(
        admission,
        source,
        start=start,
        end=start + len(selected),
    )

    assert proof.start == start
    assert proof.start != len(source[:start].encode("utf-8"))
    assert proof.selected_text(source) == selected
    assert CurrentTurnSourceSpanV1.from_payload(proof.to_payload()) == proof

    inner_start = len(source) - len(source.lstrip())
    inner_end = len(source.rstrip())
    whole = CurrentTurnSourceSpanV1.capture(
        admission,
        source,
        start=inner_start,
        end=inner_end,
    )
    assert "\r\n" in whole.selected_text(source)
    assert "👩\u200d💻" in whole.selected_text(source)


def test_source_span_does_not_normalize_nfc_or_nfd() -> None:
    source = "é / e\u0301"
    admission = _admission(source)
    nfc = CurrentTurnSourceSpanV1.capture(admission, source, start=0, end=1)
    nfd_start = source.index("e")
    nfd = CurrentTurnSourceSpanV1.capture(
        admission,
        source,
        start=nfd_start,
        end=len(source),
    )

    assert nfc.selected_text(source) == "é"
    assert nfd.selected_text(source) == "e\u0301"
    assert nfc.selected_text_sha256 != nfd.selected_text_sha256


@pytest.mark.parametrize("surrogate", ["\ud800", "\ud801"])
def test_source_span_rejects_unpaired_surrogates(surrogate: str) -> None:
    source = "current" + surrogate + "task"
    with pytest.raises(ValueError, match="Unicode scalar"):
        _admission(source)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (True, 2),
        (0, False),
        (-1, 2),
        (1, 1),
        (2, 1),
        (0, 8),
    ],
)
def test_source_span_rejects_noncanonical_ranges(start, end) -> None:
    source = "任务文本"
    admission = _admission(source)

    with pytest.raises(ValueError, match="source span"):
        CurrentTurnSourceSpanV1.capture(
            admission,
            source,
            start=start,
            end=end,
        )


def test_v3_preserves_raw_source_and_enforces_source_limit() -> None:
    selected = "x" * 3998
    source = " " + selected + " "
    payload = _v3_payload("project-source", source, task=selected)

    assert len(payload.source_user_text) == 4000
    assert payload.source_user_text == source

    oversized = source + "x"
    admission = _admission(oversized)
    with pytest.raises(ValueError, match="too long"):
        CurrentTurnSourceSpanV1.capture(
            admission,
            oversized,
            start=1,
            end=3999,
        )


def test_prior_context_cannot_become_current_task() -> None:
    source = "现在只检查状态。"
    prior_task = "创建植物大战僵尸游戏"
    admission = _admission(source)
    proof = CurrentTurnSourceSpanV1.capture(
        admission,
        source,
        start=0,
        end=len(source),
    )

    with pytest.raises(ValueError, match="task must equal"):
        WorkEffectPayloadV3(
            provider="generic-source-provider",
            task=prior_task,
            title=prior_task,
            project_id="project-source",
            session_id="session-source",
            utterance_id=admission.utterance_id,
            turn_id=admission.turn_id,
            source_user_text=source,
            source_user_context="User: " + prior_task,
            source_context_scope="chat:session-source",
            source_proof=proof,
            requirements=_requirements(),
        )


def test_noncanonical_provider_requirements_fail_before_acceptance(tmp_path) -> None:
    source = "创建一个棋盘。"
    admission = _admission(source)
    ledger, work, project = _stores(tmp_path)
    control = WorkControl(ledger, work)
    control.admit(admission, fence_scope="foreground-chat")
    proof = CurrentTurnSourceSpanV1.capture(
        admission,
        source,
        start=0,
        end=len(source),
    )
    try:
        with pytest.raises(ValueError, match="canonical typed form"):
            WorkEffectPayloadV3(
                provider="generic-source-provider",
                task=source,
                title=source,
                project_id=project.project_id,
                session_id="session-source",
                utterance_id=admission.utterance_id,
                turn_id=admission.turn_id,
                source_user_text=source,
                source_user_context="",
                source_context_scope="chat:session-source",
                source_proof=proof,
                requirements=ProviderRequirements(
                    task_kind="GENERAL",
                    workspace_access="write",
                    workspace_ownership="caller",
                    ownership="managed",
                ),
            )
        assert ledger.get_admission(admission.root_id)["plan_id"] is None
        assert ledger.pending_effects() == []
    finally:
        ledger.close()
        work.close()


def test_v3_payload_round_trip_is_exact_and_rejects_proof_shape_drift() -> None:
    source = "先说明一下，然后创建棋盘。"
    payload = _v3_payload("project-source", source, task="创建棋盘。")
    encoded = payload.to_payload()

    assert WorkEffectPayloadV3.from_payload(encoded) == payload
    assert json.loads(json.dumps(encoded, ensure_ascii=False)) == encoded

    unknown = json.loads(json.dumps(encoded, ensure_ascii=False))
    unknown["source_proof"]["extra"] = True
    with pytest.raises(ControlLedgerConflict, match="source-span"):
        WorkEffectPayloadV3.from_payload(unknown)

    boolean_version = json.loads(json.dumps(encoded, ensure_ascii=False))
    boolean_version["source_proof"]["version"] = True
    with pytest.raises(ControlLedgerConflict, match="version/kind"):
        WorkEffectPayloadV3.from_payload(boolean_version)

    float_version = json.loads(json.dumps(encoded, ensure_ascii=False))
    float_version["version"] = 3.0
    with pytest.raises(ControlLedgerConflict, match="version/operation"):
        WorkEffectPayloadV3.from_payload(float_version)
    float_version = json.loads(json.dumps(encoded, ensure_ascii=False))
    float_version["source_proof"]["version"] = 1.0
    with pytest.raises(ControlLedgerConflict, match="version/kind"):
        WorkEffectPayloadV3.from_payload(float_version)

    algorithm = json.loads(json.dumps(encoded, ensure_ascii=False))
    algorithm["source_proof"]["digest_algorithm"] = "sha256_utf8_v2"
    with pytest.raises(ControlLedgerConflict, match="digest algorithm"):
        WorkEffectPayloadV3.from_payload(algorithm)

    uppercase = json.loads(json.dumps(encoded, ensure_ascii=False))
    uppercase["source_proof"]["selected_text_sha256"] = uppercase[
        "source_proof"
    ]["selected_text_sha256"].upper()
    with pytest.raises(ControlLedgerConflict, match="SHA-256"):
        WorkEffectPayloadV3.from_payload(uppercase)


@pytest.mark.parametrize(
    "mutation",
    ["root", "scope", "utterance", "transcript", "selection", "session", "turn"],
)
def test_seal_rejects_source_identity_mutation(tmp_path, mutation: str) -> None:
    source = "说明背景，然后创建一个棋盘。"
    admission = _admission(source)
    ledger, work, project = _stores(tmp_path)
    control = WorkControl(ledger, work)
    control.admit(admission, fence_scope="foreground-chat")
    payload = _v3_payload(project.project_id, source, task="创建一个棋盘。")
    proof = payload.source_proof
    try:
        if mutation == "root":
            payload = replace(payload, source_proof=replace(proof, root_id="other-root"))
        elif mutation == "scope":
            payload = replace(
                payload,
                source_proof=replace(proof, source_scope="chat:other"),
            )
        elif mutation == "utterance":
            payload = replace(
                payload,
                source_proof=replace(proof, utterance_id="other-utterance"),
            )
        elif mutation == "transcript":
            payload = replace(
                payload,
                source_proof=replace(proof, transcript_hash="0" * 64),
            )
        elif mutation == "selection":
            with pytest.raises(ValueError, match="digest"):
                replace(
                    payload,
                    source_proof=replace(proof, selected_text_sha256="0" * 64),
                )
            assert ledger.get_admission(admission.root_id)["plan_id"] is None
            return
        elif mutation == "session":
            with pytest.raises(ValueError, match="source_context_scope"):
                replace(payload, session_id="other-session")
            assert ledger.get_admission(admission.root_id)["plan_id"] is None
            return
        else:
            payload = replace(payload, turn_id="other-turn")
        with pytest.raises(ControlLedgerConflict):
            control.seal(admission, payload)
        assert ledger.get_admission(admission.root_id)["plan_id"] is None
        assert ledger.pending_effects() == []
    finally:
        ledger.close()
        work.close()


def test_source_bound_effect_keeps_proof_out_of_provider_request(tmp_path) -> None:
    source = "先解释，然后创建一个棋盘。"
    selected = "创建一个棋盘。"
    admission = _admission(source)
    ledger, work, project = _stores(tmp_path)
    control = WorkControl(ledger, work)
    control.admit(admission, fence_scope="foreground-chat")
    payload = _v3_payload(
        project.project_id,
        source,
        task=selected,
        source_user_context=" User: 保留蓝色。 \n\n Assistant: 已记录。 ",
    )
    try:
        sealed = control.seal(admission, payload)
        effect_id = sealed["effect_id"]
        replay = control.seal(admission, payload)
        assert sealed["replayed"] is False and replay["replayed"] is True
        request = control.provider_request(effect_id)
        assert request.task == selected
        assert request.metadata["source_user_text"] == source
        assert request.metadata["source_user_context"] == (
            "User: 保留蓝色。\nAssistant: 已记录。"
        )
        assert request.metadata["payload_continuity"] == "current_turn"
        assert "source_proof" not in request.metadata
        assert "root_id" not in request.metadata
        assert "effect_id" not in request.metadata
    finally:
        ledger.close()
        work.close()


def test_runtime_rejoin_does_not_coerce_source_metadata_types(tmp_path) -> None:
    source = "123"
    admission = _admission(source)
    ledger, work, project = _stores(tmp_path)
    control = WorkControl(ledger, work)
    control.admit(admission, fence_scope="foreground-chat")
    payload = _v3_payload(project.project_id, source)
    try:
        effect_id = control.seal(admission, payload)["effect_id"]
        request = control.provider_request(effect_id)
        request.metadata["source_user_text"] = 123
        with pytest.raises(WorkLedgerConflict, match="does not match"):
            control.validate_runtime_request(
                ProviderRunIntakeAuthority(effect_id),
                request,
            )
        assert control.binding(effect_id) is None
        assert ledger.get_effect(effect_id)["state"] == "pending"
    finally:
        ledger.close()
        work.close()


@pytest.mark.parametrize("continuity", ["current_turn", "confirmed_prior_request"])
def test_new_v2_effect_is_rejected_without_source_selection(tmp_path, continuity: str) -> None:
    source = "创建一个棋盘。"
    admission = _admission(source)
    ledger, work, project = _stores(tmp_path)
    control = WorkControl(ledger, work)
    control.admit(admission, fence_scope="foreground-chat")
    payload = WorkEffectPayloadV2(
        provider="generic-source-provider",
        task=source,
        title=source,
        project_id=project.project_id,
        session_id="session-source",
        utterance_id=admission.utterance_id,
        turn_id=admission.turn_id,
        source_user_text=source,
        source_user_context="User: prior assignment evidence",
        source_context_scope="chat:session-source",
        payload_continuity=continuity,
        requirements=_requirements(),
    )
    try:
        with pytest.raises(ControlLedgerConflict, match="source proof v3"):
            control.seal(admission, payload)
        assert ledger.get_admission(admission.root_id)["plan_id"] is None
        assert ledger.pending_effects() == []
    finally:
        ledger.close()
        work.close()


def test_exact_v2_plan_can_replay_but_cannot_start_an_unbound_dispatch(tmp_path) -> None:
    source = "创建一个棋盘。"
    admission = _admission(source)
    ledger, work, project = _stores(tmp_path)
    control = WorkControl(ledger, work)
    control.admit(admission, fence_scope="foreground-chat")
    payload = WorkEffectPayloadV2(
        provider="generic-source-provider",
        task=source,
        title=source,
        project_id=project.project_id,
        session_id="session-source",
        utterance_id=admission.utterance_id,
        turn_id=admission.turn_id,
        source_user_text=source,
        source_user_context="User: historical accepted context",
        source_context_scope="chat:session-source",
        payload_continuity="current_turn",
        requirements=_requirements(),
    )
    effect_id = control._identity(admission.root_id, "work:0")
    plan_id = control._identity(admission.root_id, "work-plan:v2")
    try:
        ledger.accept(
            admission.root_id,
            chat_epoch=admission.chat_epoch,
            plan_id=plan_id,
            effects=(
                ControlEffect(
                    effect_id,
                    "work",
                    "work-project:" + project.project_id,
                    payload.to_payload(),
                ),
            ),
            evidence={
                "adapter": "proposal_gated_work_c2:v2",
                "transcript_hash": admission.transcript_hash,
            },
        )
        replay = control.seal(admission, payload)
        assert replay["replayed"] is True
        with pytest.raises(ControlLedgerConflict, match="source proof v3"):
            control.provider_request(effect_id)
        with pytest.raises(ControlLedgerConflict, match="cannot create"):
            control.bind_dispatch_intent(
                effect_id,
                provider_run_id="v2-run-must-not-start",
                lease_seconds=15.0,
                reconciliation=ReconciliationPolicy(
                    "provider_submission",
                    max_probes=3,
                    interval_seconds=5.0,
                    ttl_seconds=60.0,
                ),
            )
        assert control.binding(effect_id) is None

        # Simulate a pre-upgrade atomic C2 binding without using the upgraded
        # WorkControl entry. Only this already-bound shape remains readable.
        def write_historical_binding(cursor, frozen_payload):
            assert frozen_payload == payload.to_payload()
            item, operation, attempt = work.write_effect_work_item_with_attempt(
                cursor,
                project.project_id,
                title=payload.title,
                goal=payload.task,
                intent="execute",
                instruction=payload.task,
                provider=payload.provider,
                task=payload.task,
                mode=payload.mode,
                origin_effect_id=effect_id,
                provider_run_id="historical-v2-bound-run",
            )
            return {
                "origin_effect_id": effect_id,
                "work_item_id": item.work_item_id,
                "operation_id": operation.operation_id,
                "attempt_id": attempt.attempt_id,
                "provider_run_id": attempt.provider_run_id,
            }

        historical = ledger.claim_with_local_intent(
            effect_id,
            owner="historical-c2-fixture",
            lease_seconds=15.0,
            reconciliation=ReconciliationPolicy(
                "provider_submission",
                max_probes=3,
                interval_seconds=5.0,
                ttl_seconds=60.0,
            ),
            apply=write_historical_binding,
        )
        assert control.runtime_binding(effect_id) == historical["details"]
        with pytest.raises(ControlLedgerConflict, match="source proof v3"):
            control.provider_request(effect_id)
        assert control.bind_dispatch_intent(
            effect_id,
            provider_run_id="historical-v2-bound-run",
            lease_seconds=15.0,
            reconciliation=ReconciliationPolicy(
                "provider_submission",
                max_probes=3,
                interval_seconds=5.0,
                ttl_seconds=60.0,
            ),
        )["replayed"] is True

        binding = historical["details"]
        work.update_attempt(
            binding["attempt_id"],
            execution_status="succeeded",
            metadata={
                "provider_terminal_pipeline": {
                    "version": 1,
                    "state": "completed",
                    "provider": payload.provider,
                    "run_id": binding["provider_run_id"],
                    "status": "succeeded",
                    "receipt_sha256": "historical-v2-terminal-receipt",
                }
            },
        )
        work.record_completion(
            binding["work_item_id"],
            CompletionDecision(
                execution_status="succeeded",
                completeness="complete",
                attention="none",
                work_item_state="review_ready",
                rationale="Historical v2 terminal fixture completed.",
                terminal=True,
            ),
            attempt_id=binding["attempt_id"],
            source="host",
        )
        terminal = control.record_terminal_receipt(effect_id)
        assert terminal["receipt"]["outcome"] == "succeeded"
        assert control.record_terminal_receipt(effect_id)["replayed"] is True
        stored_payload = json.loads(ledger.get_effect(effect_id)["payload_json"])
        assert stored_payload == payload.to_payload()
        assert stored_payload["version"] == 2
        assert "source_proof" not in stored_payload
    finally:
        ledger.close()
        work.close()


def test_different_accepted_plan_does_not_authorize_v2_replay(tmp_path) -> None:
    source = "创建一个棋盘。"
    admission = _admission(source)
    ledger, work, project = _stores(tmp_path)
    control = WorkControl(ledger, work)
    control.admit(admission, fence_scope="foreground-chat")
    payload = WorkEffectPayloadV2(
        provider="generic-source-provider",
        task=source,
        title=source,
        project_id=project.project_id,
        session_id="session-source",
        utterance_id=admission.utterance_id,
        turn_id=admission.turn_id,
        source_user_text=source,
        source_user_context="",
        source_context_scope="chat:session-source",
        payload_continuity="current_turn",
        requirements=_requirements(),
    )
    try:
        ledger.accept(
            admission.root_id,
            chat_epoch=admission.chat_epoch,
            plan_id="different-plan",
            effects=(),
            evidence={"kind": "different"},
        )
        with pytest.raises(ControlLedgerConflict, match="immutable"):
            control.seal(admission, payload)
        assert ledger.pending_effects() == []
    finally:
        ledger.close()
        work.close()
