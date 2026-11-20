"""自动审批必须先提交真实权限记录和授权决定。"""

from contextlib import asynccontextmanager
import asyncio
import sqlite3

import pytest

from config import settings
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderEvent, ProviderRunRequest
from agent_host.work_ledger_store import WorkLedgerStore
from server.work_ledger_coordinator import WorkLedgerCoordinator
from server.event_bus import bus
from server.protocol import Method
from test_provider_abstraction_quality import _InteractiveAdapter


@asynccontextmanager
async def approval_host(tmp_path, monkeypatch, *, automatic):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", automatic)
    monkeypatch.setattr(settings, "WORK_WORKTREE_ISOLATION", False)
    monkeypatch.setattr(settings, "WORK_PROJECT_ALLOWLIST", str(workspace))
    store = WorkLedgerStore(tmp_path / "ledger.sqlite3")
    coordinator = WorkLedgerCoordinator(store)
    runtime = ProviderRuntime()
    adapter = _InteractiveAdapter()
    runtime.register(adapter)
    runtime.set_request_preparer(coordinator.prepare_request)
    runtime.set_automatic_permission_authorizer(coordinator.authorize_automatic_permission)
    coordinator.configure()
    record = None
    try:
        record = await runtime.start(ProviderRunRequest(
            provider=adapter.provider_id, task="Synthetic permission persistence",
            cwd=str(workspace), metadata={"session_id": "permission-persistence"},
        ))
        await asyncio.wait_for(adapter.started.wait(), 2)
        yield store, coordinator, runtime, adapter, record
    finally:
        adapter.release.set()
        if record is not None and record.task_handle is not None:
            await record.task_handle
        await runtime.close()
        await coordinator.drain_provider_facts()
        coordinator.close()


def permission_event(record):
    return ProviderEvent(
        provider=record.provider, run_id=record.run_id,
        type="permission.requested", payload={"permissionRequest": {
            "request_id": "call-persistence", "capability": "mcp.tool.call",
            "options": ["allow_once", "deny"],
        }},
    )


@pytest.mark.parametrize(("automatic", "failure", "expected_responses"), [
    (False, "none", 0), (True, "none", 1),
    (True, "request", 0), (True, "decision", 0),
])
async def test_automatic_permission_waits_for_committed_authority(
    tmp_path, monkeypatch, automatic, failure, expected_responses,
):
    async with approval_host(tmp_path, monkeypatch, automatic=automatic) as host:
        store, _coordinator, runtime, adapter, record = host
        if failure != "none":
            def fail(*args, **kwargs):
                raise sqlite3.OperationalError("synthetic permission persistence failure")
            monkeypatch.setattr(store, "create_permission_request" if failure == "request"
                else "resolve_permission_request", fail)
        result, = await asyncio.gather(runtime._emit(record, permission_event(record)), return_exceptions=True)
        permissions = store.list_permission_requests(record.metadata["work"]["work_item_id"])
        assert len(adapter.responses) == expected_responses
        assert len(permissions) == (0 if failure == "request" else 1)
        if automatic and failure != "none":
            assert isinstance(result, Exception)
            assert all(permission.status == "pending" for permission in permissions)
        elif automatic:
            assert result is None
            assert permissions[0].status == "allowed"
            assert permissions[0].metadata["resolved_automatically"] is True
        else:
            assert permissions[0].status == "pending"


async def test_hot_enable_cannot_approve_a_request_that_failed_to_commit(tmp_path, monkeypatch):
    async with approval_host(tmp_path, monkeypatch, automatic=False) as host:
        store, _coordinator, runtime, adapter, record = host

        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("synthetic permission persistence failure")

        monkeypatch.setattr(store, "create_permission_request", fail)
        await runtime._emit(record, permission_event(record))
        monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", True)
        await asyncio.gather(runtime.apply_permission_policy(), return_exceptions=True)
        await runtime.apply_permission_policy()
        assert adapter.responses == []
        assert store.list_permission_requests(record.metadata["work"]["work_item_id"]) == []


async def test_disabling_policy_after_commit_does_not_abandon_the_committed_response(tmp_path, monkeypatch):
    async with approval_host(tmp_path, monkeypatch, automatic=True) as host:
        store, coordinator, runtime, adapter, record = host
        committed, release = asyncio.Event(), asyncio.Event()

        async def authorize(run_id, request_id):
            result = coordinator.authorize_automatic_permission(run_id, request_id)
            committed.set()
            await release.wait()
            return result

        runtime.set_automatic_permission_authorizer(authorize)
        posting = asyncio.create_task(runtime._emit(record, permission_event(record)))
        try:
            await asyncio.wait_for(committed.wait(), 2)
            permission, = store.list_permission_requests(record.metadata["work"]["work_item_id"])
            assert permission.status == "allowed"
            monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", False)
            release.set()
            await posting
            response, = adapter.responses
            assert response.allow is True and response.automatic is True
            later = permission_event(record)
            later.payload["permissionRequest"]["request_id"] = "after-policy-disabled"
            await runtime._emit(record, later)
            assert len(adapter.responses) == 1
            permissions = store.list_permission_requests(record.metadata["work"]["work_item_id"])
            assert [p.status for p in permissions] == ["allowed", "pending"]
        finally:
            release.set()
            await posting


async def test_unknown_response_keeps_committed_decision_without_replaying(tmp_path, monkeypatch):
    async with approval_host(tmp_path, monkeypatch, automatic=True) as host:
        store, _coordinator, runtime, adapter, record = host

        async def lose_ack(run_id, response):
            adapter.responses.append(response)
            raise ConnectionError("synthetic acknowledgement loss")

        monkeypatch.setattr(adapter, "resolve_permission", lose_ack)
        with pytest.raises(ConnectionError, match="acknowledgement loss"):
            await runtime._emit(record, permission_event(record))
        await runtime.apply_permission_policy()
        permissions = store.list_permission_requests(record.metadata["work"]["work_item_id"])
        assert len(adapter.responses) == 1
        assert len(permissions) == 1 and permissions[0].status == "allowed"
        assert permissions[0].metadata["resolved_automatically"] is True


async def test_presentation_failure_does_not_replace_the_committed_permission_owner(tmp_path, monkeypatch):
    async with approval_host(tmp_path, monkeypatch, automatic=True) as host:
        store, _coordinator, runtime, adapter, record = host

        async def presentation_failure(_method, params):
            if params.get("type") == "permission.requested":
                raise RuntimeError("synthetic presentation failure")

        bus.on(Method.PROVIDER_EVENT, presentation_failure)
        try:
            await runtime._emit(record, permission_event(record))
            permissions = store.list_permission_requests(record.metadata["work"]["work_item_id"])
            assert len(adapter.responses) == 1
            assert len(permissions) == 1 and permissions[0].status == "allowed"
            assert permissions[0].metadata["resolved_automatically"] is True
        finally:
            bus.off(Method.PROVIDER_EVENT, presentation_failure)


@pytest.mark.parametrize("changed", [False, True])
async def test_failed_expiry_cannot_reuse_the_old_pending_authority(tmp_path, monkeypatch, caplog, changed):
    async with approval_host(tmp_path, monkeypatch, automatic=False) as host:
        store, _coordinator, runtime, adapter, record = host
        original = permission_event(record)
        original.payload["permissionRequest"]["mcpCall"] = {
            "toolName": "status", "arguments": {"value": "original"},
        }
        await runtime._emit(record, original)

        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("synthetic expiry commit failure")

        with monkeypatch.context() as expiry:
            expiry.setattr(store, "resolve_permission_request", fail)
            await runtime._emit(record, ProviderEvent(
                provider=record.provider, run_id=record.run_id, type="permission.expired",
                payload={"request_id": "call-persistence", "automatic": True},
            ))
        assert "synthetic expiry commit failure" in caplog.text
        monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", True)
        repeated = permission_event(record)
        repeated.payload["permissionRequest"]["mcpCall"] = {
            "toolName": "status", "arguments": {"value": "changed" if changed else "original"},
        }
        if changed:
            with pytest.raises(ValueError, match="permission request identity"):
                await runtime._emit(record, repeated)
        else:
            await runtime._emit(record, repeated)
        await runtime.apply_permission_policy()
        permissions = store.list_permission_requests(record.metadata["work"]["work_item_id"])
        assert adapter.responses == []
        assert len(permissions) == 1 and permissions[0].status == "pending"
        assert permissions[0].metadata["mcp_call"]["arguments"] == {"value": "original"}
