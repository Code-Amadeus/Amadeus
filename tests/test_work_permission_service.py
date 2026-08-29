"""Contract tests for Attempt-scoped permission authority."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from server.work_export_service import WorkExportService
from server.work_permission_service import WorkPermissionService


def _fixture(store: WorkLedgerStore, workspace: Path):
    project = store.create_or_get_project(workspace, name="Permission")
    item = store.create_work_item(
        project.project_id,
        title="Approve one action",
        workspace_path=workspace,
    )
    _, attempt = store.create_operation_attempt(
        item.work_item_id,
        intent="execute",
        instruction="Perform one bounded action.",
        provider="locus",
        task="Perform one bounded action.",
    )
    return item, attempt


def _service(store: WorkLedgerStore, desktop: Path) -> WorkPermissionService:
    return WorkPermissionService(
        store,
        WorkExportService(store, desktop_path=desktop),
        auto_accept_approved_exports=False,
    )


def test_exact_work_and_attempt_identity_are_required_before_resolution() -> None:
    with tempfile.TemporaryDirectory(prefix="permission_identity_") as temp:
        root = Path(temp)
        workspace = root / "project"
        desktop = root / "Desktop"
        workspace.mkdir()
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            item, attempt = _fixture(store, workspace)
            request = store.create_permission_request(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
                capability="shell",
                action="run",
                options=["allow_once", "deny"],
            )
            service = _service(store, desktop)
            for work_item_id, attempt_id in (
                ("", attempt.attempt_id),
                ("work-other", attempt.attempt_id),
                (item.work_item_id, ""),
                (item.work_item_id, "attempt-other"),
            ):
                try:
                    service.context(
                        request.request_id,
                        work_item_id=work_item_id,
                        attempt_id=attempt_id,
                    )
                except WorkLedgerConflict:
                    pass
                else:  # pragma: no cover - assertion branch
                    raise AssertionError("mismatched permission identity was accepted")
            assert store.get_permission_request(request.request_id).status == "pending"  # type: ignore[union-attr]


def test_permission_options_bound_the_decision_not_the_renderer() -> None:
    with tempfile.TemporaryDirectory(prefix="permission_options_") as temp:
        root = Path(temp)
        workspace = root / "project"
        desktop = root / "Desktop"
        workspace.mkdir()
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            item, attempt = _fixture(store, workspace)
            request = store.create_permission_request(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
                capability="shell",
                action="run",
                options=["deny"],
            )
            service = _service(store, desktop)
            context = service.context(
                request.request_id,
                work_item_id=item.work_item_id,
                attempt_id=attempt.attempt_id,
            )
            try:
                service.resolve(context, allow=True)
            except WorkLedgerConflict:
                pass
            else:  # pragma: no cover - assertion branch
                raise AssertionError("deny-only request accepted approval")
            assert store.get_permission_request(request.request_id).status == "pending"  # type: ignore[union-attr]
            denied = service.resolve(context, allow=False)
            assert denied.permission.status == "denied"
            assert denied.permission.metadata["resolution"] == "user_denied"


def test_desktop_disposition_is_classified_from_the_durable_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="permission_export_") as temp:
        root = Path(temp)
        workspace = root / "project"
        desktop = root / "Desktop"
        workspace.mkdir()
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            item, attempt = _fixture(store, workspace)
            request = store.create_permission_request(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
                capability="filesystem.export",
                action="copy_to_desktop",
                scope_paths=[str(desktop / "result.txt")],
                options=["allow_once", "deny"],
                metadata={"kind": "desktop_export", "entries": []},
            )
            service = _service(store, desktop)
            context = service.context(
                request.request_id,
                work_item_id=item.work_item_id,
                attempt_id=attempt.attempt_id,
            )
            assert context.desktop_export is True
            denied = service.resolve(context, allow=False)
            assert denied.permission.status == "denied"
            assert denied.exported_paths == ()
            assert list(desktop.iterdir()) == []


def test_terminal_attempt_expires_provider_checkpoint_not_product_permission() -> None:
    with tempfile.TemporaryDirectory(prefix="permission_terminal_scope_") as temp:
        root = Path(temp)
        workspace = root / "project"
        desktop = root / "Desktop"
        workspace.mkdir()
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            item, attempt = _fixture(store, workspace)
            provider = store.create_permission_request(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
                capability="shell.execute",
                action="execute_command",
                options=["allow_once", "deny"],
                metadata={"kind": "provider_permission"},
            )
            product = store.create_permission_request(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
                capability="filesystem.export",
                action="copy_to_desktop",
                scope_paths=[str(desktop / "result.txt")],
                options=["allow_once", "deny"],
                metadata={"kind": "desktop_export", "entries": []},
            )
            terminal = store.update_attempt(
                attempt.attempt_id,
                execution_status="succeeded",
            )
            expired = _service(store, desktop).expire_provider_checkpoints(
                terminal,
                resolution="attempt_terminal",
            )

            assert expired == 1
            assert store.get_permission_request(provider.request_id).status == "expired"  # type: ignore[union-attr]
            assert store.get_permission_request(product.request_id).status == "pending"  # type: ignore[union-attr]


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all work permission service tests passed")


if __name__ == "__main__":
    _main()
