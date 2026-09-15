"""Recovery ancestry preserves accepted input facts without copying authority."""

from __future__ import annotations

import pytest

from agent_host.provider_types import ProviderRecoveryContext
from agent_host.work_ledger_store import (
    PROVIDER_RECOVERY_METADATA_KEYS,
    WorkLedgerStore,
)
from server.work_read_model import WorkReadModel


def _model(store: WorkLedgerStore) -> WorkReadModel:
    return WorkReadModel(
        store,
        is_unkept_draft=lambda _path:False,
        is_desktop_export_permission=lambda _request:False,
        can_resume_authorized_export=lambda _request:False,
    )


def _recovery(reason: str, predecessor, *, root: str = "") -> ProviderRecoveryContext:
    return ProviderRecoveryContext(
        reason=reason,
        root_attempt_id=root or predecessor.attempt_id,
        predecessor_attempt_id=predecessor.attempt_id,
        ordinal=1,
        feedback=("AUIP preflight reported an application error."
            if reason == "auip_validation_failed" else ""),
    )


def _marker(reason: str, predecessor, successor, *, state: str = "started") -> dict:
    specific = ({"classification":"progress_only_completion"}
        if reason == "progress_only_completion"
        else {"verified":False, "kind":"app_error"})
    return {
        **specific,
        "recovery_state":state,
        "recovery_root_attempt_id":predecessor.attempt_id,
        "recovery_ordinal":1,
        "recovery_claimed_at":10.0,
        **({
            "successor_attempt_id":successor.attempt_id,
            "successor_run_id":successor.provider_run_id,
        } if successor is not None else {}),
    }


def _seed_parent(store: WorkLedgerStore, tmp_path, *, delivery_state: str = "delivered"):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    project = store.create_or_get_project(workspace)
    item = store.create_work_item(
        project.project_id,
        title="Recovered application",
        workspace_path=workspace,
    )
    operation, parent = store.create_operation_attempt(
        item.work_item_id,
        intent="execute",
        instruction="Create the application.",
        provider="codex",
        task="Create the application.",
        provider_run_id="run-parent",
    )
    parent = store.update_attempt(parent.attempt_id, execution_status="running")
    requirement = store.create_operation(
        item.work_item_id,
        intent="amend",
        instruction="Keep the accepted extra requirement.",
        metadata={
            "work_input_id":"input-parent",
            "attempt_id":parent.attempt_id,
        },
    )
    store.accept_provider_input(
        input_id="input-parent",
        work_item_id=item.work_item_id,
        run_id=parent.provider_run_id,
        text=requirement.instruction,
    )
    if delivery_state != "unknown":
        store.finish_provider_input(
            "input-parent", state=delivery_state,
            reason="delivery rejected" if delivery_state == "rejected" else "")
    parent = store.update_attempt(parent.attempt_id, execution_status="succeeded")
    return item, operation, parent, requirement


def _add_successor(store: WorkLedgerStore, item, operation, parent, *, reason: str):
    recovery = _recovery(reason, parent)
    successor = store.create_attempt(
        item.work_item_id,
        operation_id=operation.operation_id,
        provider=parent.provider,
        task="Repair the same application.",
        provider_run_id="run-successor",
        metadata={"provider_recovery":recovery.to_dict()},
    )
    successor = store.update_attempt(successor.attempt_id, execution_status="running")
    parent = store.update_attempt(
        parent.attempt_id,
        metadata={PROVIDER_RECOVERY_METADATA_KEYS[reason]:
            _marker(reason, parent, successor)},
    )
    return parent, successor


@pytest.mark.parametrize("reason", ["progress_only_completion", "auip_validation_failed"])
@pytest.mark.parametrize("delivery_state", ["delivered", "rejected", "unknown"])
def test_recovery_successor_inherits_original_requirement_and_receipt_state(
        tmp_path, reason, delivery_state) -> None:
    with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
        item, operation, parent, requirement = _seed_parent(
            store, tmp_path, delivery_state=delivery_state)
        parent, successor = _add_successor(
            store, item, operation, parent, reason=reason)
        model = _model(store)

        assert store.get_recovery_predecessor(successor) == parent
        projected = model.input_requirements(
            item.work_item_id, attempt_id=successor.attempt_id)
        assert projected == [{
            "operation_id":requirement.operation_id,
            "input_id":"input-parent",
            "attempt_id":parent.attempt_id,
            "text":requirement.instruction,
            "delivery_state":delivery_state,
            "delivery_reason":("delivery rejected"
                if delivery_state == "rejected" else
                "delivery_unconfirmed" if delivery_state == "unknown" else ""),
        }]
        assert len(store.list_attempts(item.work_item_id)) == 2
        assert len(store.list_operations(item.work_item_id)) == 2


def test_recovery_projection_keeps_predecessor_and_current_requirements_without_copying(
        tmp_path) -> None:
    with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
        item, operation, parent, parent_requirement = _seed_parent(store, tmp_path)
        parent, successor = _add_successor(
            store, item, operation, parent, reason="auip_validation_failed")
        current_requirement = store.create_operation(
            item.work_item_id,
            intent="amend",
            instruction="Also retain the new repair constraint.",
            metadata={
                "work_input_id":"input-current",
                "attempt_id":successor.attempt_id,
            },
        )
        store.accept_provider_input(
            input_id="input-current",
            work_item_id=item.work_item_id,
            run_id=successor.provider_run_id,
            text=current_requirement.instruction,
        )
        model = _model(store)

        projected = model.input_requirements(
            item.work_item_id, attempt_id=successor.attempt_id)
        assert [row["operation_id"] for row in projected] == [
            parent_requirement.operation_id,
            current_requirement.operation_id,
        ]
        assert [row["attempt_id"] for row in projected] == [
            parent.attempt_id,
            successor.attempt_id,
        ]
        assert [row["delivery_state"] for row in projected] == [
            "delivered", "unknown"]
        assert model.detail(item.work_item_id)["inputRequirements"] == projected
        assert len(store.list_attempts(item.work_item_id)) == 2
        assert len(store.list_operations(item.work_item_id)) == 3


@pytest.mark.parametrize(
    "state", ["claimed", "started", "failed", "cancelled", "cancel_pending"])
def test_claimed_and_terminal_recovery_states_preserve_historical_ancestry(
        tmp_path, state) -> None:
    with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
        item, operation, parent, _requirement = _seed_parent(store, tmp_path)
        recovery = _recovery("auip_validation_failed", parent)
        successor = store.create_attempt(
            item.work_item_id,
            operation_id=operation.operation_id,
            provider=parent.provider,
            task="Repair the same application.",
            provider_run_id="run-successor",
            metadata={"provider_recovery":recovery.to_dict()},
        )
        marker = _marker(
            "auip_validation_failed",
            parent,
            None if state == "claimed" else successor,
            state=state,
        )
        parent = store.update_attempt(
            parent.attempt_id,
            metadata={"host_auip_bundle_validation":marker},
        )
        assert store.get_recovery_predecessor(successor) == parent


@pytest.mark.parametrize(
    "defect",
    [
        "work", "operation", "provider", "root", "marker_root",
        "marker_ordinal", "successor", "successor_run", "unclaimed",
    ],
)
def test_invalid_recovery_lineage_never_inherits_predecessor_requirements(
        tmp_path, defect) -> None:
    with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
        item, operation, parent, _requirement = _seed_parent(store, tmp_path)
        target_item, target_operation = item, operation
        if defect == "work":
            other_workspace = tmp_path / "other"
            other_workspace.mkdir()
            project = store.create_or_get_project(other_workspace)
            target_item = store.create_work_item(
                project.project_id, title="Other Work", workspace_path=other_workspace)
            target_operation = store.create_operation(
                target_item.work_item_id, intent="execute", instruction="Other operation")
        elif defect == "operation":
            target_operation = store.create_operation(
                item.work_item_id, intent="amend", instruction="Different operation")

        recovery = _recovery(
            "progress_only_completion",
            parent,
            root="attempt-wrong-root" if defect == "root" else "",
        )
        successor = store.create_attempt(
            target_item.work_item_id,
            operation_id=target_operation.operation_id,
            provider="other-provider" if defect == "provider" else parent.provider,
            task="Recovery candidate",
            provider_run_id="run-successor",
            metadata={"provider_recovery":recovery.to_dict()},
        )
        marker = _marker(
            "progress_only_completion",
            parent,
            successor,
            state="unclaimed" if defect == "unclaimed" else "started",
        )
        if defect == "successor":
            marker["successor_attempt_id"] = "attempt-other"
        elif defect == "successor_run":
            marker["successor_run_id"] = "run-other"
        elif defect == "marker_root":
            marker["recovery_root_attempt_id"] = "attempt-other"
        elif defect == "marker_ordinal":
            marker["recovery_ordinal"] = 2
        parent = store.update_attempt(
            parent.attempt_id,
            metadata={"provider_completion":marker},
        )

        assert store.get_recovery_predecessor(successor) is None
        assert _model(store).input_requirements(
            target_item.work_item_id, attempt_id=successor.attempt_id) == []
        assert len(store.list_operations(item.work_item_id)) == (
            3 if defect == "operation" else 2)


def test_ordinary_terminal_attempt_does_not_inherit_or_query_recovery_ancestry(
        tmp_path, monkeypatch) -> None:
    with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
        item, operation, _parent, _requirement = _seed_parent(store, tmp_path)
        ordinary = store.create_attempt(
            item.work_item_id,
            operation_id=operation.operation_id,
            provider="codex",
            task="Answer an ordinary terminal question.",
            provider_run_id="run-question",
        )
        ordinary = store.update_attempt(ordinary.attempt_id, execution_status="succeeded")
        original_get_attempt = store.get_attempt
        monkeypatch.setattr(store, "get_attempt",
            lambda *_args, **_kwargs:(_ for _ in ()).throw(
                AssertionError("ordinary ancestry must perform zero SQL")))
        assert store.get_recovery_predecessor(ordinary) is None
        monkeypatch.setattr(store, "get_attempt", original_get_attempt)

        assert _model(store).input_requirements(
            item.work_item_id, attempt_id=ordinary.attempt_id) == []
        assert len(store.list_attempts(item.work_item_id)) == 2
        assert len(store.list_operations(item.work_item_id)) == 2
