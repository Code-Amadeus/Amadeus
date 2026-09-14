from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest

from agent_host.provider_contract import (
    ProviderCapabilities,
    ProviderManifest,
    ProviderRequirements,
)
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import (
    ProviderRunRequest,
    ProviderRunResult,
    ProviderSessionHandle,
)
from agent_host.work_ledger_store import WorkLedgerStore
from config import settings
from server.auip_app_source import discover_registered_auip_app
from server.work_context import run_work_notes
from server.work_export_service import WorkExportService
from server.work_ledger_coordinator import WorkLedgerCoordinator
from test_auip_entry_preflight import _write_fixture
from test_work_effect_executor import _admission, _host, _payload
from tools.sync_auip_manifest import sync_manifest


@pytest.fixture(autouse=True)
def trusted_recovery_workspace(tmp_path, monkeypatch):
    # These recovery journeys deliberately reuse a persistent test workspace.
    # A previous Attempt is not trust: configure the real registry for each case.
    monkeypatch.setattr(settings, "WORK_PROJECT_ALLOWLIST", str(tmp_path))


APP_ERROR = {
    "verified": False,
    "kind": "app_error",
    "code": "choice_compact_unavailable_option",
    "detail": "choice option assist is unavailable in the initial snapshot",
    "checks": ["manifest", "embedded_manifest_sync", "runtime_asset_integrity"],
    "entry": "index.html",
    "manifest": "auip.manifest.json",
    "boot": {
        "ok": False,
        "kind": "app_error",
        "diagnostics": [
            {
                "kind": "pageerror",
                "code": "choice_compact_unavailable_option",
                "message": "choice option assist is unavailable in the initial snapshot",
            }
        ],
    },
}

VALID = {
    "verified": True,
    "kind": "ok",
    "code": "ok",
    "detail": "",
    "checks": [
        "manifest",
        "embedded_manifest_sync",
        "runtime_asset_integrity",
        "entry_wiring",
        "entry_boot",
    ],
    "entry": "index.html",
    "manifest": "auip.manifest.json",
    "boot": {"ok": True, "kind": "ok", "diagnostics": []},
}


class _AuipRepairAdapter:
    provider_id = "auip_repair_test"
    manifest = ProviderManifest(
        provider_id=provider_id,
        display_name="AUIP repair test",
        capabilities=ProviderCapabilities(
            task_kinds=("general", "workspace_mutation"),
            workspace_access="write",
            workspace_ownership="caller",
            durability="process",
            resume="attach",
            event_model="canonical+native",
        ),
    )

    def __init__(self) -> None:
        self.requests: list[ProviderRunRequest] = []
        self.first_ready = asyncio.Event()
        self.release_first = asyncio.Event()
        self.second_returned = asyncio.Event()
        self.release_second = asyncio.Event()
        self.release_second.set()

    async def run(self, request, _run_id, _emit):
        self.requests.append(request)
        export_plan = (
            request.metadata.get("export_plan")
            if isinstance(request.metadata.get("export_plan"), dict)
            else {}
        )
        root = Path(str(export_plan.get("staging_root") or request.cwd))
        manifest = {
            "schema": "amadeus.auip/v0",
            "app": {
                "id": "repair-test",
                "title": "Repair Test",
                "version": "0.1.0",
            },
            "events": {"app.ready": {"beat": True}},
            "actions": {},
            "stances": ["spectator"],
        }
        rendered = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        (root / "auip.manifest.json").write_text(rendered, encoding="utf-8")
        if len(self.requests) == 1:
            (root / "index.html").write_text(
                "<!doctype html><script>throw new Error('bad initial snapshot')</script>",
                encoding="utf-8",
            )
            self.first_ready.set()
            await self.release_first.wait()
        else:
            assert request.recovery is not None
            assert request.recovery.reason == "auip_validation_failed"
            assert request.session == ProviderSessionHandle(
                provider=self.provider_id,
                session_id="native-auip-thread",
            )
            (root / "index.html").write_text(
                "<!doctype html>\n"
                '<script id="auip-manifest" type="application/json">\n'
                f"{rendered}\n"
                "</script>\n"
                '<script src="./sdk/auip-core/managed-v0.js"></script>\n'
                '<script src="./sdk/auip-core/situations-v0.js"></script>\n'
                '<script src="./sdk/auip-web/auip-v0.js"></script>\n',
                encoding="utf-8",
            )
            self.second_returned.set()
            await self.release_second.wait()
        return ProviderRunResult(
            status="done",
            result="Prepared the AUIP application.",
            metadata={
                "artifacts": [
                    {"path": "index.html", "kind": "business.file"},
                    {"path": "auip.manifest.json", "kind": "business.file"},
                ]
            },
            session=ProviderSessionHandle(
                provider=self.provider_id,
                session_id="native-auip-thread",
            ),
        )

    async def cancel(self, _run_id):
        return {"cancelled": True}


class _RealBootRepairAdapter(_AuipRepairAdapter):
    async def run(self, request, _run_id, _emit):
        self.requests.append(request)
        first = len(self.requests) == 1
        if not first:
            assert request.recovery is not None
            assert request.recovery.reason == "auip_validation_failed"
            assert "choice_compact_unavailable_option" in request.recovery.feedback
            assert request.session == ProviderSessionHandle(
                provider=self.provider_id,
                session_id="native-auip-thread",
            )
        root = Path(str(request.cwd))
        fixture_root = root.parent / (
            "real-boot-invalid" if first else "real-boot-valid"
        )
        manifest_path, entry_path = _write_fixture(
            fixture_root, option_available=not first
        )
        shutil.copyfile(manifest_path, root / "auip.manifest.json")
        shutil.copyfile(entry_path, root / "index.html")
        shutil.rmtree(fixture_root)
        if not first:
            self.second_returned.set()
        return ProviderRunResult(
            status="done",
            result="Prepared the AUIP application.",
            metadata={
                "artifacts": [
                    {"path": "index.html", "kind": "business.file"},
                    {"path": "auip.manifest.json", "kind": "business.file"},
                ]
            },
            session=ProviderSessionHandle(
                provider=self.provider_id,
                session_id="native-auip-thread",
            ),
        )


class _ModeRepairAdapter(_AuipRepairAdapter):
    async def run(self, request, _run_id, _emit):
        self.requests.append(request)
        first = len(self.requests) == 1
        if not first:
            assert request.recovery is not None
            assert request.recovery.reason == "auip_validation_failed"
            assert "auip_engagement_mode_unsupported" in request.recovery.feedback
            assert request.session == ProviderSessionHandle(
                provider=self.provider_id,
                session_id="native-auip-thread",
            )
        export_plan = (
            request.metadata.get("export_plan")
            if isinstance(request.metadata.get("export_plan"), dict)
            else {}
        )
        root = Path(str(export_plan.get("staging_root") or request.cwd))
        fixture_root = root.parent / (
            "mode-spectator" if first else "mode-participant"
        )
        manifest_path, entry_path = _write_fixture(
            fixture_root, option_available=True
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["stances"] = (
            ["spectator"] if first else ["spectator", "participant"]
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        sync_manifest(manifest_path, entry_path)
        shutil.copyfile(manifest_path, root / "auip.manifest.json")
        shutil.copyfile(entry_path, root / "index.html")
        shutil.rmtree(fixture_root)
        if not first:
            self.second_returned.set()
        return ProviderRunResult(
            status="done",
            result="Prepared the AUIP application.",
            metadata={
                "artifacts": [
                    {"path": "index.html", "kind": "business.file"},
                    {"path": "auip.manifest.json", "kind": "business.file"},
                ]
            },
            session=ProviderSessionHandle(
                provider=self.provider_id,
                session_id="native-auip-thread",
            ),
        )


def _init_repository(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "auip-recovery@example.invalid"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "AUIP Recovery Test"],
        cwd=root,
        check=True,
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)


def _request(
    adapter: _AuipRepairAdapter,
    root: Path,
    *,
    required_mode: str = "",
    desktop: bool = False,
) -> ProviderRunRequest:
    return ProviderRunRequest(
        provider=adapter.provider_id,
        task="Build and validate the shared AUIP game.",
        cwd=str(root),
        mode="agent",
        requirements=ProviderRequirements(
            task_kind="workspace_mutation",
            workspace_access="write",
            workspace_ownership="caller",
            preferred_provider=adapter.provider_id,
            preference_policy="require",
        ),
        metadata={
            "source": "test_auip_recovery",
            "session_id": "auip-recovery-session",
            "host_outcome_requirement": {
                "operation": "prepare",
                "facet": "auip.application",
                "expected": {
                    "current_attempt_contribution": True,
                    **(
                        {"engagement_mode": required_mode}
                        if required_mode
                        else {}
                    ),
                },
            },
            "provider_manifest": adapter.manifest.to_dict(),
            **(
                {
                    "external_export": {
                        "target": "desktop",
                        "filename": "index.html",
                    }
                }
                if desktop
                else {}
            ),
        },
    )


@pytest.mark.asyncio
async def test_real_boot_error_is_repaired_by_same_native_work_attempt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _init_repository(root)
    adapter = _RealBootRepairAdapter()
    runtime = ProviderRuntime()
    runtime.register(adapter)
    store = WorkLedgerStore(tmp_path / "real-boot-recovery.sqlite3")
    coordinator = WorkLedgerCoordinator(
        store,
        provider_start=runtime.start,
        provider_cancel=runtime.cancel,
    )
    runtime.set_request_preparer(coordinator.prepare_request)
    coordinator.configure()
    previous_isolation = settings.WORK_WORKTREE_ISOLATION
    settings.WORK_WORKTREE_ISOLATION = False
    try:
        first = await runtime.start(_request(adapter, root))
        assert first.task_handle is not None
        await asyncio.wait_for(first.task_handle, timeout=30)
        await asyncio.wait_for(adapter.second_returned.wait(), timeout=30)
        runs = runtime.list_runs()
        assert len(runs) == 2
        successor = next(
            runtime.get_run(str(row["run_id"]))
            for row in runs
            if row["run_id"] != first.run_id
        )
        assert successor is not None and successor.task_handle is not None
        await asyncio.wait_for(successor.task_handle, timeout=30)

        work_item_id = first.metadata["work"]["work_item_id"]
        attempts = store.list_attempts(work_item_id)
        assert len(attempts) == 2
        assert len(store.list_operations(work_item_id)) == 1
        assert attempts[0].operation_id == attempts[1].operation_id
        first_validation = attempts[0].metadata["host_auip_bundle_validation"]
        assert first_validation["verified"] is False
        assert first_validation["kind"] == "app_error"
        assert first_validation["code"] == "javascript_page_error"
        assert "choice_compact_unavailable_option" in first_validation["detail"]
        assert first_validation["boot"]["diagnostics"]
        assert attempts[1].metadata["provider_session_attach"][
            "previous_attempt_id"
        ] == attempts[0].attempt_id
        assert attempts[1].metadata["host_auip_bundle_validation"][
            "verified"
        ] is True
        assert adapter.requests[1].session == ProviderSessionHandle(
            provider=adapter.provider_id,
            session_id="native-auip-thread",
        )
        discovered = discover_registered_auip_app(store, work_item_id)
        assert discovered is not None
        assert attempts[1].attempt_id in discovered["contributing_attempt_ids"]
        assert discovered["entry_path"] == str(root / "index.html")
    finally:
        settings.WORK_WORKTREE_ISOLATION = previous_isolation
        await runtime.close()
        coordinator.close()


@pytest.mark.asyncio
async def test_collaborate_mode_mismatch_repairs_same_operation_to_participant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _init_repository(root)
    monkeypatch.setattr(
        "server.auip_bundle_validation.validate_entry",
        AsyncMock(return_value={"ok": True, "kind": "ok", "diagnostics": []}),
    )
    adapter = _ModeRepairAdapter()
    runtime = ProviderRuntime()
    runtime.register(adapter)
    store = WorkLedgerStore(tmp_path / "mode-recovery.sqlite3")
    coordinator = WorkLedgerCoordinator(
        store,
        provider_start=runtime.start,
        provider_cancel=runtime.cancel,
    )
    runtime.set_request_preparer(coordinator.prepare_request)
    coordinator.configure()
    previous_isolation = settings.WORK_WORKTREE_ISOLATION
    settings.WORK_WORKTREE_ISOLATION = False
    try:
        first = await runtime.start(
            _request(adapter, root, required_mode="collaborate")
        )
        assert first.task_handle is not None
        await asyncio.wait_for(first.task_handle, timeout=10)
        await asyncio.wait_for(adapter.second_returned.wait(), timeout=10)
        runs = runtime.list_runs()
        successor = next(
            runtime.get_run(str(row["run_id"]))
            for row in runs
            if row["run_id"] != first.run_id
        )
        assert successor is not None and successor.task_handle is not None
        await asyncio.wait_for(successor.task_handle, timeout=10)

        work_item_id = first.metadata["work"]["work_item_id"]
        attempts = store.list_attempts(work_item_id)
        assert len(attempts) == 2
        assert len(store.list_operations(work_item_id)) == 1
        assert attempts[0].operation_id == attempts[1].operation_id
        mismatch = attempts[0].metadata["host_auip_bundle_validation"]
        assert mismatch["code"] == "auip_engagement_mode_unsupported"
        assert mismatch["boot"]["ok"] is True
        assert mismatch["supported_modes"] == ["observe"]
        assert mismatch["recovery_state"] == "started"
        repaired = attempts[1].metadata["host_auip_bundle_validation"]
        assert repaired["verified"] is True
        assert repaired["required_mode"] == "collaborate"
        assert "collaborate" in repaired["supported_modes"]
        assert adapter.requests[1].session == ProviderSessionHandle(
            provider=adapter.provider_id,
            session_id="native-auip-thread",
        )
        discovered = discover_registered_auip_app(store, work_item_id)
        assert discovered is not None
        assert discovered["stances"] == ["spectator", "participant"]
    finally:
        settings.WORK_WORKTREE_ISOLATION = previous_isolation
        await runtime.close()
        coordinator.close()


@pytest.mark.asyncio
async def test_mode_mismatch_never_reaches_desktop_export_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    desktop = tmp_path / "Desktop"
    root.mkdir()
    desktop.mkdir()
    _init_repository(root)
    monkeypatch.setattr(
        "server.auip_bundle_validation.validate_entry",
        AsyncMock(return_value={"ok": True, "kind": "ok", "diagnostics": []}),
    )
    adapter = _ModeRepairAdapter()
    runtime = ProviderRuntime()
    runtime.register(adapter)
    store = WorkLedgerStore(tmp_path / "mode-export.sqlite3")
    coordinator = WorkLedgerCoordinator(
        store,
        export_service=WorkExportService(store, desktop_path=desktop),
    )
    runtime.set_request_preparer(coordinator.prepare_request)
    coordinator.configure()
    previous_isolation = settings.WORK_WORKTREE_ISOLATION
    settings.WORK_WORKTREE_ISOLATION = False
    try:
        first = await runtime.start(
            _request(
                adapter,
                root,
                required_mode="collaborate",
                desktop=True,
            )
        )
        assert first.task_handle is not None
        await asyncio.wait_for(first.task_handle, timeout=10)

        work = first.metadata["work"]
        attempt = store.get_attempt(work["attempt_id"])
        assert attempt is not None
        validation = attempt.metadata["host_auip_bundle_validation"]
        assert validation["code"] == "auip_engagement_mode_unsupported"
        assert validation["boot"]["ok"] is True
        assert store.list_permission_requests(
            work["work_item_id"], attempt_id=work["attempt_id"]
        ) == []
        assert list(desktop.iterdir()) == []
        assert len(adapter.requests) == 1
    finally:
        settings.WORK_WORKTREE_ISOLATION = previous_isolation
        await runtime.close()
        coordinator.close()


@pytest.mark.asyncio
async def test_app_boot_failure_retries_same_work_operation_with_host_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _init_repository(root)
    validations = [dict(APP_ERROR), dict(VALID)]

    async def validate(*_args, **_kwargs):
        return validations.pop(0)

    monkeypatch.setattr(
        "server.auip_bundle_validation.validate_auip_web_bundle_execution",
        validate,
    )
    adapter = _AuipRepairAdapter()
    runtime = ProviderRuntime()
    runtime.register(adapter)
    store = WorkLedgerStore(tmp_path / "ledger.sqlite3")
    coordinator = WorkLedgerCoordinator(
        store,
        provider_start=runtime.start,
        provider_cancel=runtime.cancel,
    )
    runtime.set_request_preparer(coordinator.prepare_request)
    coordinator.configure()
    previous_isolation = settings.WORK_WORKTREE_ISOLATION
    settings.WORK_WORKTREE_ISOLATION = False
    try:
        first = await runtime.start(_request(adapter, root))
        await asyncio.wait_for(adapter.first_ready.wait(), timeout=5)
        work = first.metadata["work"]
        accepted_requirement = (
            'Keep the compact "assist" choice available.\n'
            "Preserve the original controller instruction."
        )
        receipt, created = store.accept_provider_input(
            input_id="input-extra",
            work_item_id=work["work_item_id"],
            run_id=first.run_id,
            text=accepted_requirement,
        )
        assert created
        store.finish_provider_input(
            receipt["input_id"], state="delivered", reason="native_ack"
        )
        store.create_operation(
            work["work_item_id"],
            intent="amend",
            instruction=receipt["text"],
            metadata={
                "work_input_id": receipt["input_id"],
                "attempt_id": work["attempt_id"],
            },
        )
        adapter.release_first.set()
        assert first.task_handle is not None
        await asyncio.wait_for(first.task_handle, timeout=10)
        await asyncio.wait_for(adapter.second_returned.wait(), timeout=10)
        runs = runtime.list_runs()
        assert len(runs) == 2
        successor = next(
            runtime.get_run(str(row["run_id"]))
            for row in runs
            if row["run_id"] != first.run_id
        )
        assert successor is not None and successor.task_handle is not None
        await asyncio.wait_for(successor.task_handle, timeout=10)

        attempts = store.list_attempts(work["work_item_id"])
        operations = store.list_operations(work["work_item_id"])
        assert len(attempts) == 2
        assert len(operations) == 2
        assert adapter.requests[0].metadata["host_auip_bundle_validation"] == {
            "verified": False,
            "kind": "pending",
            "code": "auip_validation_pending",
            "detail": "",
            "checks": [],
            "boot": None,
        }
        assert attempts[0].operation_id == attempts[1].operation_id
        marker = attempts[0].metadata["host_auip_bundle_validation"]
        assert marker["recovery_state"] == "started"
        assert marker["successor_attempt_id"] == attempts[1].attempt_id
        recovery = adapter.requests[1].recovery
        assert recovery is not None
        assert recovery.feedback == attempts[1].metadata["provider_recovery"]["feedback"]
        assert "choice_compact_unavailable_option" in recovery.feedback
        assert accepted_requirement in recovery.feedback
        assert attempts[1].metadata["provider_requirements"]["workspace_access"] == "write"
        assert attempts[1].metadata["provider_session_attach"]["recovery_reason"] == (
            "auip_validation_failed"
        )
        assert attempts[1].metadata["host_auip_bundle_validation"]["verified"] is True
        assert not any(
            note.get("metadata", {}).get("work_event") == "work.attempt_finished"
            for note in run_work_notes(first.run_id, limit=100)
        )
    finally:
        settings.WORK_WORKTREE_ISOLATION = previous_isolation
        await runtime.close()
        coordinator.close()


@pytest.mark.asyncio
async def test_control_work_effect_can_start_its_claimed_auip_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "Build a small game, then let us play it together."
    task = "Build a small game"
    validations = [dict(APP_ERROR), dict(VALID)]

    async def validate(*_args, **_kwargs):
        return validations.pop(0)

    monkeypatch.setattr(
        "server.auip_bundle_validation.validate_auip_web_bundle_execution",
        validate,
    )
    async with _host(tmp_path, source=source, task=task) as host:
        host.adapter.manifest = replace(
            host.adapter.manifest,
            capabilities=replace(
                host.adapter.manifest.capabilities,
                resume="attach",
                event_model="canonical+native",
            ),
        )
        host.runtime.register(host.adapter)
        host.coordinator._provider_start = host.runtime.start
        host.coordinator._provider_cancel = host.runtime.cancel
        requests: list[ProviderRunRequest] = []

        async def run(request, _run_id, _emit):
            requests.append(request)
            root = Path(str(request.cwd))
            manifest = {
                "schema": "amadeus.auip/v0",
                "app": {
                    "id": "control-repair",
                    "title": "Control Repair",
                    "version": "0.1.0",
                },
                "events": {"app.ready": {"beat": True}},
                "actions": {},
                "stances": ["spectator"],
            }
            rendered = json.dumps(manifest, sort_keys=True)
            (root / "auip.manifest.json").write_text(rendered, encoding="utf-8")
            if len(requests) == 1:
                html = "<!doctype html><script>throw new Error('invalid')</script>"
            else:
                assert request.recovery is not None
                html = (
                    "<!doctype html>\n"
                    '<script id="auip-manifest" type="application/json">\n'
                    f"{rendered}\n</script>\n"
                    '<script src="./sdk/auip-core/managed-v0.js"></script>\n'
                    '<script src="./sdk/auip-core/situations-v0.js"></script>\n'
                    '<script src="./sdk/auip-web/auip-v0.js"></script>\n'
                )
            (root / "index.html").write_text(html, encoding="utf-8")
            return ProviderRunResult(
                status="done",
                result="Prepared the application.",
                metadata={
                    "artifacts": [
                        {"path": "index.html", "kind": "business.file"},
                        {"path": "auip.manifest.json", "kind": "business.file"},
                    ]
                },
                session=ProviderSessionHandle(
                    provider=host.adapter.provider_id,
                    session_id="control-native-thread",
                ),
            )

        host.adapter.run = run
        admission = _admission(suffix="auip-repair", epoch=2, text=source)
        host.control.admit(admission, fence_scope="foreground-chat")
        payload = _payload(
            host.project.project_id,
            host.adapter.provider_id,
            suffix="auip-repair",
            source=source,
            task=task,
        )
        evidence = {
            "cooperative_batch": {
                "version": 1,
                "kind": "work_then_auip_after_work",
                "actions": [
                    {
                        "index": 0,
                        "op": "work",
                        "intent": "execute",
                        "source_start": source.index(task),
                        "source_end": source.index(task) + len(task),
                        "source_sha256": hashlib.sha256(task.encode()).hexdigest(),
                    },
                    {
                        "index": 1,
                        "op": "auip_after_work",
                        "mode": "collaborate",
                        "source_start": 0,
                        "source_end": len(source),
                        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                    },
                ],
            }
        }
        effect = host.control.seal(admission, payload, plan_evidence=evidence)
        dispatch = await host.executor.dispatch(effect["effect_id"])
        assert dispatch.record is not None and dispatch.record.task_handle is not None
        await asyncio.wait_for(dispatch.record.task_handle, timeout=10)
        for _ in range(100):
            if len(requests) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(requests) == 2
        successor = next(
            record
            for record in host.runtime._runs.values()
            if record.run_id != dispatch.record.run_id
        )
        assert successor.task_handle is not None
        await asyncio.wait_for(successor.task_handle, timeout=10)
        assert requests[0].metadata["source"] == "control_work_effect"
        assert requests[1].metadata["source"] == "control_work_effect"
        assert requests[1].recovery is not None
        assert requests[1].recovery.reason == "auip_validation_failed"


@pytest.mark.asyncio
async def test_stop_during_real_boot_wait_retires_validation_without_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _init_repository(root)
    validation_started = asyncio.Event()
    release_validation = asyncio.Event()

    async def validate(*_args, **_kwargs):
        validation_started.set()
        await release_validation.wait()
        return dict(VALID)

    monkeypatch.setattr(
        "server.auip_bundle_validation.validate_auip_web_bundle_execution",
        validate,
    )
    adapter = _AuipRepairAdapter()
    runtime = ProviderRuntime()
    runtime.register(adapter)
    store = WorkLedgerStore(tmp_path / "stop-during-boot.sqlite3")
    coordinator = WorkLedgerCoordinator(
        store,
        provider_start=runtime.start,
        provider_cancel=runtime.cancel,
    )
    runtime.set_request_preparer(coordinator.prepare_request)
    coordinator.configure()
    previous_isolation = settings.WORK_WORKTREE_ISOLATION
    settings.WORK_WORKTREE_ISOLATION = False
    try:
        first = await runtime.start(_request(adapter, root))
        await asyncio.wait_for(adapter.first_ready.wait(), timeout=5)
        adapter.release_first.set()
        await asyncio.wait_for(validation_started.wait(), timeout=10)
        work = first.metadata["work"]
        processing = store.get_attempt(work["attempt_id"])
        assert processing is not None
        assert processing.execution_status == "succeeded"
        assert processing.metadata["host_auip_bundle_validation"][
            "recovery_state"
        ] == "validation_pending"
        pending = coordinator.pending_provider_recoveries()
        assert [row["attempt_id"] for row in pending] == [work["attempt_id"]]

        unrelated_progressed = asyncio.Event()

        async def unrelated_turn() -> None:
            await asyncio.sleep(0)
            unrelated_progressed.set()

        unrelated = asyncio.create_task(unrelated_turn())
        await asyncio.wait_for(unrelated_progressed.wait(), timeout=1)
        await unrelated
        assert coordinator.cancel_pending_provider_recovery(work["attempt_id"])
        release_validation.set()
        assert first.task_handle is not None
        await asyncio.wait_for(first.task_handle, timeout=10)

        attempt = store.get_attempt(work["attempt_id"])
        assert attempt is not None
        assert attempt.execution_status == "succeeded"
        marker = attempt.metadata["host_auip_bundle_validation"]
        assert marker["code"] == "auip_validation_pending"
        assert marker["recovery_state"] == "cancelled"
        assert marker["recovery_error"] == "user_retracted"
        assert len(adapter.requests) == 1
        assert len(store.list_attempts(work["work_item_id"])) == 1
        assert "outcome_verdict" not in attempt.metadata
        assert discover_registered_auip_app(store, work["work_item_id"]) is None
        assert store.list_permission_requests(work["work_item_id"]) == []
        assert not any(
            note.get("metadata", {}).get("work_event") == "work.attempt_finished"
            for note in run_work_notes(first.run_id, limit=100)
        )
        assert coordinator.pending_provider_recoveries() == []
    finally:
        release_validation.set()
        settings.WORK_WORKTREE_ISOLATION = previous_isolation
        await runtime.close()
        coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["host_materialization_error", "browser_environment_error", "tool_error"]
)
async def test_non_app_validation_failure_never_starts_provider_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    root = tmp_path / kind
    root.mkdir()
    _init_repository(root)

    async def validate(*_args, **_kwargs):
        return {
            "verified": False,
            "kind": kind,
            "code": f"{kind}_fixture",
            "detail": "Host environment could not validate the bundle.",
            "checks": [],
            "boot": None,
        }

    monkeypatch.setattr(
        "server.auip_bundle_validation.validate_auip_web_bundle_execution",
        validate,
    )
    adapter = _AuipRepairAdapter()
    runtime = ProviderRuntime()
    runtime.register(adapter)
    store = WorkLedgerStore(tmp_path / f"{kind}.sqlite3")
    coordinator = WorkLedgerCoordinator(store, provider_start=runtime.start)
    runtime.set_request_preparer(coordinator.prepare_request)
    coordinator.configure()
    previous_isolation = settings.WORK_WORKTREE_ISOLATION
    settings.WORK_WORKTREE_ISOLATION = False
    try:
        first = await runtime.start(_request(adapter, root))
        await asyncio.wait_for(adapter.first_ready.wait(), timeout=5)
        adapter.release_first.set()
        assert first.task_handle is not None
        await asyncio.wait_for(first.task_handle, timeout=10)
        assert len(adapter.requests) == 1
        attempt = store.get_attempt(first.metadata["work"]["attempt_id"])
        assert attempt is not None
        marker = attempt.metadata["host_auip_bundle_validation"]
        assert marker["kind"] == kind
        assert "recovery_state" not in marker
    finally:
        settings.WORK_WORKTREE_ISOLATION = previous_isolation
        await runtime.close()
        coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_state", ["unknown", "rejected"])
async def test_unsettled_or_rejected_requirement_blocks_auip_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delivery_state: str,
) -> None:
    root = tmp_path / delivery_state
    root.mkdir()
    _init_repository(root)

    async def validate(*_args, **_kwargs):
        return dict(APP_ERROR)

    monkeypatch.setattr(
        "server.auip_bundle_validation.validate_auip_web_bundle_execution",
        validate,
    )
    adapter = _AuipRepairAdapter()
    runtime = ProviderRuntime()
    runtime.register(adapter)
    store = WorkLedgerStore(tmp_path / f"{delivery_state}.sqlite3")
    coordinator = WorkLedgerCoordinator(store, provider_start=runtime.start)
    runtime.set_request_preparer(coordinator.prepare_request)
    coordinator.configure()
    previous_isolation = settings.WORK_WORKTREE_ISOLATION
    settings.WORK_WORKTREE_ISOLATION = False
    try:
        first = await runtime.start(_request(adapter, root))
        await asyncio.wait_for(adapter.first_ready.wait(), timeout=5)
        work = first.metadata["work"]
        receipt, _created = store.accept_provider_input(
            input_id=f"input-{delivery_state}",
            work_item_id=work["work_item_id"],
            run_id=first.run_id,
            text="Add a second controller mode.",
        )
        if delivery_state == "rejected":
            store.finish_provider_input(
                receipt["input_id"], state="rejected", reason="native_rejected"
            )
        store.create_operation(
            work["work_item_id"],
            intent="amend",
            instruction=receipt["text"],
            metadata={
                "work_input_id": receipt["input_id"],
                "attempt_id": work["attempt_id"],
            },
        )
        adapter.release_first.set()
        assert first.task_handle is not None
        await asyncio.wait_for(first.task_handle, timeout=10)
        assert len(adapter.requests) == 1
        marker = store.get_attempt(work["attempt_id"])
        assert marker is not None
        assert marker.metadata["host_auip_bundle_validation"][
            "recovery_state"
        ] == "unclaimed"
    finally:
        settings.WORK_WORKTREE_ISOLATION = previous_isolation
        await runtime.close()
        coordinator.close()


@pytest.mark.asyncio
async def test_second_invalid_attempt_exhausts_single_repair_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _init_repository(root)

    async def validate(*_args, **_kwargs):
        return dict(APP_ERROR)

    monkeypatch.setattr(
        "server.auip_bundle_validation.validate_auip_web_bundle_execution",
        validate,
    )
    adapter = _AuipRepairAdapter()
    runtime = ProviderRuntime()
    runtime.register(adapter)
    store = WorkLedgerStore(tmp_path / "budget.sqlite3")
    coordinator = WorkLedgerCoordinator(store, provider_start=runtime.start)
    runtime.set_request_preparer(coordinator.prepare_request)
    coordinator.configure()
    previous_isolation = settings.WORK_WORKTREE_ISOLATION
    settings.WORK_WORKTREE_ISOLATION = False
    try:
        first = await runtime.start(_request(adapter, root))
        await asyncio.wait_for(adapter.first_ready.wait(), timeout=5)
        adapter.release_first.set()
        assert first.task_handle is not None
        await asyncio.wait_for(first.task_handle, timeout=10)
        await asyncio.wait_for(adapter.second_returned.wait(), timeout=10)
        runs = runtime.list_runs()
        successor = next(
            runtime.get_run(str(row["run_id"]))
            for row in runs
            if row["run_id"] != first.run_id
        )
        assert successor is not None and successor.task_handle is not None
        await asyncio.wait_for(successor.task_handle, timeout=10)
        assert len(runtime.list_runs()) == 2
        attempts = store.list_attempts(first.metadata["work"]["work_item_id"])
        assert attempts[1].metadata["provider_recovery"]["ordinal"] == 1
        assert attempts[1].metadata["host_auip_bundle_validation"][
            "recovery_state"
        ] == "failed"
        assert attempts[1].metadata["host_auip_bundle_validation"][
            "recovery_error"
        ] == "recovery_budget_exhausted"
    finally:
        settings.WORK_WORKTREE_ISOLATION = previous_isolation
        await runtime.close()
        coordinator.close()


@pytest.mark.asyncio
async def test_claimed_auip_repair_cancel_races_with_successor_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _init_repository(root)

    async def validate(*_args, **_kwargs):
        return dict(APP_ERROR)

    monkeypatch.setattr(
        "server.auip_bundle_validation.validate_auip_web_bundle_execution",
        validate,
    )
    adapter = _AuipRepairAdapter()
    adapter.release_second.clear()
    runtime = ProviderRuntime()
    runtime.register(adapter)
    recovery_entered = asyncio.Event()
    release_recovery = asyncio.Event()

    async def delayed_start(request: ProviderRunRequest):
        record = await runtime.start(request)
        recovery_entered.set()
        await release_recovery.wait()
        return record

    store = WorkLedgerStore(tmp_path / "cancel.sqlite3")
    coordinator = WorkLedgerCoordinator(
        store,
        provider_start=delayed_start,
        provider_cancel=runtime.cancel,
    )
    runtime.set_request_preparer(coordinator.prepare_request)
    coordinator.configure()
    previous_isolation = settings.WORK_WORKTREE_ISOLATION
    settings.WORK_WORKTREE_ISOLATION = False
    try:
        first = await runtime.start(_request(adapter, root))
        await asyncio.wait_for(adapter.first_ready.wait(), timeout=5)
        adapter.release_first.set()
        await asyncio.wait_for(recovery_entered.wait(), timeout=10)
        pending = coordinator.pending_provider_recoveries()
        assert len(pending) == 1
        assert coordinator.cancel_pending_provider_recovery(
            pending[0]["attempt_id"]
        )
        release_recovery.set()
        assert first.task_handle is not None
        await asyncio.wait_for(first.task_handle, timeout=10)
        attempts = store.list_attempts(first.metadata["work"]["work_item_id"])
        assert len(attempts) == 2
        recovery_state = attempts[0].metadata["host_auip_bundle_validation"][
            "recovery_state"
        ]
        assert recovery_state in {"cancelled", "cancel_pending"}
        assert attempts[0].metadata["host_auip_bundle_validation"][
            "successor_attempt_id"
        ] == attempts[1].attempt_id
        if recovery_state == "cancel_pending":
            assert attempts[1].execution_status == "running"
        assert len(runtime.list_runs()) == 2
        assert coordinator.pending_provider_recoveries() == []
    finally:
        settings.WORK_WORKTREE_ISOLATION = previous_isolation
        adapter.release_second.set()
        release_recovery.set()
        await runtime.close()
        coordinator.close()


def test_startup_reconciles_claimed_auip_recovery_without_execution(
    tmp_path: Path,
) -> None:
    for name, with_successor in (("missing", False), ("linked", True)):
        root = tmp_path / name
        root.mkdir()
        workspace = root / "workspace"
        workspace.mkdir()
        store = WorkLedgerStore(root / "ledger.sqlite3")
        project = store.create_or_get_project(workspace)
        item = store.create_work_item(
            project.project_id,
            title="Repair AUIP app",
            goal="Repair the Host validation failure.",
        )
        predecessor = store.create_attempt(
            item.work_item_id,
            provider="codex",
            task="Build the AUIP app.",
            provider_run_id="run-predecessor",
            metadata={
                "host_auip_bundle_validation": {
                    **APP_ERROR,
                    "recovery_state": "claimed",
                    "recovery_root_attempt_id": "attempt-predecessor",
                    "recovery_ordinal": 1,
                    "recovery_claimed_at": 1.0,
                }
            },
            attempt_id="attempt-predecessor",
        )
        store.update_attempt(predecessor.attempt_id, execution_status="succeeded")
        successor = None
        if with_successor:
            successor = store.create_attempt(
                item.work_item_id,
                provider="codex",
                task="Build the AUIP app.",
                provider_run_id="run-successor",
                operation_id=predecessor.operation_id,
                metadata={
                    "provider_recovery": {
                        "reason": "auip_validation_failed",
                        "root_attempt_id": predecessor.attempt_id,
                        "predecessor_attempt_id": predecessor.attempt_id,
                        "ordinal": 1,
                        "feedback": "Host AUIP validation failed.",
                    }
                },
            )
        starts = 0

        async def must_not_start(_request):
            nonlocal starts
            starts += 1

        coordinator = WorkLedgerCoordinator(store, provider_start=must_not_start)
        coordinator.configure()
        try:
            recovered = store.get_attempt(predecessor.attempt_id)
            assert recovered is not None
            marker = recovered.metadata["host_auip_bundle_validation"]
            assert marker["recovery_state"] == (
                "started" if with_successor else "failed"
            )
            if successor is not None:
                assert marker["successor_attempt_id"] == successor.attempt_id
                assert marker["successor_run_id"] == "run-successor"
            assert starts == 0
        finally:
            coordinator.close()
            store.close()
