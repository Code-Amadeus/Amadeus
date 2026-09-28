from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_host.work_ledger_store import WorkLedgerStore
from server.attention_request import AttentionRequestCoordinator
from server.auip_launch import AuipLaunchCoordinator
from server.protocol import Method
from server.work_ledger_coordinator import WorkLedgerCoordinator
from test_auip_launch import SESSION, _manifest, _register_file, _seed_app


def _new_amendment(store, item, suffix):
    operation, attempt = store.create_operation_attempt(
        item.work_item_id,
        intent="amend",
        instruction="Update the application " + suffix,
        provider="codex",
        task="Update the application " + suffix,
        provider_run_id="run-" + suffix,
        attempt_metadata={"session_id": SESSION, "turn_id": "turn-" + suffix},
    )
    attempt = store.update_attempt(attempt.attempt_id, execution_status="running")
    return operation, attempt


def _case(tmp_path, suffix="current"):
    store = WorkLedgerStore(tmp_path / "ledger.sqlite3")
    project = store.create_or_get_project(tmp_path / "project")
    item, old_attempt, old_entry = _seed_app(
        store,
        project,
        tmp_path,
        title="Deferred Input App",
        turn_id="turn-original",
    )
    operation, attempt = _new_amendment(store, item, suffix)
    emitted = []

    async def emit(method, payload):
        emitted.append((method, payload))

    coordinator = AuipLaunchCoordinator(
        artifacts=store,
        work_roster=WorkLedgerCoordinator(store),
        attention=AttentionRequestCoordinator(),
        emit=emit,
    )
    return SimpleNamespace(
        store=store,
        item=item,
        old_attempt=old_attempt,
        old_entry=old_entry,
        operation=operation,
        attempt=attempt,
        emitted=emitted,
        coordinator=coordinator,
    )


async def _reserve(case, input_id="input-current", source_app_session_id=""):
    result = await case.coordinator.route_control(
        {
            "action": "launch",
            "target": "delivery",
            "mode": "observe",
            "after": "work",
            "_host_work_binding": "active",
            "_host_active_work_attempt_ids": (case.attempt.attempt_id,),
            "_host_work_input_id": input_id,
        },
        session_id=SESSION,
        turn_id="turn-open-after-input",
        source_app_session_id=source_app_session_id,
    )
    assert result["deferred"] is True
    pending = case.coordinator._deferred[(SESSION, "turn-open-after-input")]
    assert pending.work_item_id == case.item.work_item_id
    assert pending.operation_id == case.operation.operation_id
    assert pending.input_id == input_id
    return pending


async def test_active_input_binding_preserves_result_entry_source(tmp_path) -> None:
    case = _case(tmp_path)
    try:
        pending = await _reserve(
            case,
            source_app_session_id="app-before-active-input",
        )
        assert pending.source_app_session_id == "app-before-active-input"
        assert case.emitted == []
    finally:
        case.store.close()


def _accept_input(case, input_id="input-current"):
    row, created = case.store.accept_provider_input(
        input_id=input_id,
        work_item_id=case.item.work_item_id,
        run_id=case.attempt.provider_run_id,
        text="Also open it when this update is ready.",
    )
    assert created and row["state"] == "unknown"
    return row


def _finish_application(case):
    workspace = Path(case.item.workspace_path)
    entry = workspace / "index.html"
    entry.write_text("<!doctype html><title>updated</title>", encoding="utf-8")
    latest_entry = _register_file(case.store, case.item, case.attempt, entry)
    manifest = workspace / "auip.manifest.json"
    manifest.write_text(json.dumps(_manifest("Deferred Input App v2")), encoding="utf-8")
    _register_file(case.store, case.item, case.attempt, manifest)
    case.store.update_attempt(case.attempt.attempt_id, execution_status="succeeded")
    return latest_entry


@pytest.mark.parametrize("order", ["input_first", "work_first"])
async def test_launch_requires_exact_delivered_input_and_terminal_work(
        tmp_path, order) -> None:
    case = _case(tmp_path)
    try:
        await _reserve(case)
        _accept_input(case)
        if order == "input_first":
            case.store.finish_provider_input("input-current", state="delivered")
            await case.coordinator.on_work_updated(
                Method.WORK_INPUT_UPDATED, {"input_id": "untrusted-event-value"})
            assert case.emitted == []
            latest_entry = _finish_application(case)
            await case.coordinator.on_work_updated(Method.WORK_UPDATED, {})
        else:
            latest_entry = _finish_application(case)
            await case.coordinator.on_work_updated(Method.WORK_UPDATED, {})
            assert case.emitted == []
            case.store.finish_provider_input("input-current", state="delivered")
            await case.coordinator.on_work_updated(
                Method.WORK_INPUT_UPDATED, {"state": "rejected"})

        assert case.coordinator._deferred == {}
        assert len(case.emitted) == 1
        assert case.emitted[0][0] == Method.AUIP_LAUNCH_REQUESTED
        assert case.emitted[0][1]["artifact_id"] == latest_entry.artifact_id
        await case.coordinator.on_work_updated(Method.WORK_INPUT_UPDATED, {})
        await case.coordinator.on_work_updated(Method.WORK_UPDATED, {})
        assert len(case.emitted) == 1
    finally:
        case.store.close()


@pytest.mark.parametrize("receipt", ["missing", "unknown"])
async def test_missing_or_unknown_input_waits_after_work_terminal(tmp_path, receipt) -> None:
    case = _case(tmp_path)
    try:
        pending = await _reserve(case)
        if receipt == "unknown":
            _accept_input(case)
        _finish_application(case)

        await case.coordinator.on_work_updated(Method.WORK_UPDATED, {})
        await case.coordinator.on_work_updated(
            Method.WORK_INPUT_UPDATED,
            {"input_id": pending.input_id, "state": "delivered"},
        )

        assert case.coordinator._deferred[
            (SESSION, "turn-open-after-input")
        ] == pending
        assert case.emitted == []
    finally:
        case.store.close()


async def test_rejected_input_retires_without_opening_old_or_new_artifact(tmp_path) -> None:
    case = _case(tmp_path)
    try:
        await _reserve(case)
        _accept_input(case)
        case.store.finish_provider_input("input-current", state="rejected")

        await case.coordinator.on_work_updated(
            Method.WORK_INPUT_UPDATED, {"state": "delivered"})
        assert case.coordinator._deferred == {}
        assert case.emitted == []

        _finish_application(case)
        await case.coordinator.on_work_updated(Method.WORK_UPDATED, {})
        assert case.emitted == []
    finally:
        case.store.close()


@pytest.mark.parametrize("mismatch", ["attempt", "run"])
async def test_input_from_wrong_attempt_or_provider_run_never_opens(tmp_path, mismatch) -> None:
    case = _case(tmp_path, suffix="earlier")
    try:
        _accept_input(case)
        case.store.finish_provider_input("input-current", state="delivered")
        case.store.update_attempt(case.attempt.attempt_id, execution_status="succeeded")

        operation, attempt = _new_amendment(case.store, case.item, "expected")
        case.operation, case.attempt = operation, attempt
        if mismatch == "run":
            with case.store._transaction() as cursor:
                cursor.execute(
                    "UPDATE provider_inputs SET attempt_id=?,provider_run_id=? WHERE input_id=?",
                    (attempt.attempt_id, "wrong-provider-run", "input-current"),
                )
        pending = await _reserve(case)
        _finish_application(case)

        await case.coordinator.on_work_updated(Method.WORK_UPDATED, {})
        await case.coordinator.on_work_updated(Method.WORK_INPUT_UPDATED, {})

        assert case.coordinator._deferred[
            (SESSION, "turn-open-after-input")
        ] == pending
        assert case.emitted == []
    finally:
        case.store.close()


async def test_input_binding_prevents_terminal_immediate_launch_shortcut(tmp_path) -> None:
    case = _case(tmp_path)
    try:
        _finish_application(case)
        pending = await _reserve(case)

        assert pending.input_id == "input-current"
        assert case.emitted == []
        assert (SESSION, "turn-open-after-input") in case.coordinator._deferred
    finally:
        case.store.close()


async def test_failed_input_attempt_cannot_open_prior_success_from_same_operation(
        tmp_path) -> None:
    case = _case(tmp_path, suffix="first-operation-attempt")
    try:
        prior_entry = _finish_application(case)
        retry = case.store.create_attempt(
            case.item.work_item_id,
            operation_id=case.operation.operation_id,
            provider="codex",
            task="Retry the application update",
            provider_run_id="run-input-retry",
            metadata={"session_id": SESSION, "turn_id": "turn-input-retry"},
        )
        case.attempt = case.store.update_attempt(
            retry.attempt_id, execution_status="running"
        )
        await _reserve(case)
        _accept_input(case)
        case.store.finish_provider_input("input-current", state="delivered")
        case.store.update_attempt(case.attempt.attempt_id, execution_status="failed")

        await case.coordinator.on_work_updated(Method.WORK_INPUT_UPDATED, {})
        await case.coordinator.on_work_updated(Method.WORK_UPDATED, {})

        assert case.coordinator._deferred == {}
        assert case.emitted == []
        assert case.store.get_artifact(prior_entry.artifact_id) is not None
    finally:
        case.store.close()
