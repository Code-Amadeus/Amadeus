"""持久自动审批由 Host 统一生效，关闭后恢复逐次审批。"""
import asyncio

import pytest

from config import settings
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderEvent, ProviderRunRequest
from test_provider_abstraction_quality import _InteractiveAdapter


def runtime_with_authorizer():
    runtime = ProviderRuntime()
    runtime.set_automatic_permission_authorizer(lambda _run_id, _request_id: True)
    return runtime


async def test_automatic_policy_without_durable_owner_does_not_dispatch(monkeypatch):
    monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", True)
    runtime = ProviderRuntime()
    adapter = _InteractiveAdapter()
    runtime.register(adapter)
    record = await runtime.start(ProviderRunRequest(provider=adapter.provider_id, task="Missing owner"))
    try:
        await asyncio.wait_for(adapter.started.wait(), 2)
        with pytest.raises(RuntimeError, match="durable Host owner"):
            await runtime._emit(record, ProviderEvent(
                provider=adapter.provider_id, run_id=record.run_id, type="permission.requested",
                payload={"permissionRequest": {"request_id": "no-owner", "options": ["allow_once", "deny"]}},
            ))
        await runtime.apply_permission_policy()
        assert adapter.responses == []
    finally:
        adapter.release.set()
        await record.task_handle
        await runtime.close()


async def test_cancelled_owner_checkpoint_never_dispatches_or_retries(monkeypatch):
    monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", True)
    runtime = ProviderRuntime()
    adapter = _InteractiveAdapter()
    entered = asyncio.Event()

    async def checkpoint(_run_id, _request_id):
        entered.set()
        await asyncio.Future()

    runtime.set_automatic_permission_authorizer(checkpoint)
    runtime.register(adapter)
    record = await runtime.start(ProviderRunRequest(provider=adapter.provider_id, task="Cancelled owner"))
    posting = None
    try:
        await asyncio.wait_for(adapter.started.wait(), 2)
        posting = asyncio.create_task(runtime._emit(record, ProviderEvent(
            provider=adapter.provider_id, run_id=record.run_id, type="permission.requested",
            payload={"permissionRequest": {"request_id": "cancel-owner", "options": ["allow_once", "deny"]}},
        )))
        await asyncio.wait_for(entered.wait(), 2)
        await runtime.apply_permission_policy()
        posting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await posting
        await runtime.apply_permission_policy()
        assert adapter.responses == []
    finally:
        if posting is not None:
            posting.cancel()
            await asyncio.gather(posting, return_exceptions=True)
        adapter.release.set()
        await record.task_handle
        await runtime.close()


async def test_changed_payload_cannot_reuse_a_pending_permission_identity(monkeypatch):
    monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", False)
    runtime = runtime_with_authorizer()
    adapter = _InteractiveAdapter()
    runtime.register(adapter)
    record = await runtime.start(ProviderRunRequest(provider=adapter.provider_id, task="Request identity"))

    def event(arguments):
        return ProviderEvent(provider=adapter.provider_id, run_id=record.run_id,
            type="permission.requested", payload={"permissionRequest": {
                "request_id": "same-id", "options": ["allow_once", "deny"],
                "mcpCall": {"toolName": "status", "arguments": arguments},
            }})

    try:
        await asyncio.wait_for(adapter.started.wait(), 2)
        await runtime._emit(record, event({"value": "original"}))
        monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", True)
        with pytest.raises(ValueError, match="permission request identity"):
            await runtime._emit(record, event({"value": "changed"}))
        await runtime.apply_permission_policy()
        assert adapter.responses == []
    finally:
        adapter.release.set()
        await record.task_handle
        await runtime.close()


@pytest.mark.parametrize("capability", ["mcp.tool.call", "provider.tool.execute"])
async def test_persistent_auto_approval_applies_to_each_work_and_turns_off_live(monkeypatch, capability):
    monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", True, raising=False)
    runtime = runtime_with_authorizer()
    adapter = _InteractiveAdapter()
    runtime.register(adapter)
    records = []
    try:
        for index in range(2):
            record = await runtime.start(ProviderRunRequest(provider=adapter.provider_id, task="Approval fixture"))
            records.append(record)
            await asyncio.wait_for(adapter.started.wait(), 2)
            await runtime._emit(record, ProviderEvent(provider=adapter.provider_id, run_id=record.run_id,
                type="permission.requested", payload={"permissionRequest": {
                    "request_id": f"call-{index}", "capability": capability,
                    "options": ["allow_once", "deny"], "diagnosticOnly": False,
                }}))
        assert [response.request_id for response in adapter.responses] == ["call-0", "call-1"]
        assert all(response.allow and response.automatic for response in adapter.responses)
        for record in records:
            assert any(event["type"] == "permission.allowed" and event["payload"]["automatic"]
                       for event in record.events)
        monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", False)
        await runtime._emit(records[-1], ProviderEvent(provider=adapter.provider_id, run_id=records[-1].run_id,
            type="permission.requested", payload={"permissionRequest": {
                "request_id": "after-off", "options": ["allow_once", "deny"],
            }}))
        assert len(adapter.responses) == 2
    finally:
        adapter.release.set()
        await asyncio.gather(*(record.task_handle for record in records if record.task_handle))
        await runtime.close()


async def test_reusable_authority_is_removed_if_its_canonical_audit_is_cancelled(monkeypatch):
    from agent_host.provider_types import ProviderPermissionResponse
    from server.event_bus import bus
    from server.protocol import Method

    runtime = runtime_with_authorizer()
    adapter = _InteractiveAdapter()
    granted = False

    async def resolve(run_id, response):
        nonlocal granted
        granted = response.allow
        return {"accepted": True}

    async def fail_audit(_method, params):
        if params.get("type") == "permission.allowed":
            raise asyncio.CancelledError("synthetic durable audit cancellation")

    monkeypatch.setattr(adapter, "resolve_permission", resolve)
    runtime.register(adapter)
    record = await runtime.start(ProviderRunRequest(provider=adapter.provider_id, task="Audited authority"))
    bus.on(Method.PROVIDER_EVENT, fail_audit)
    try:
        await asyncio.wait_for(adapter.started.wait(), 2)
        with pytest.raises(asyncio.CancelledError, match="synthetic durable audit cancellation"):
            await runtime.resolve_permission(record.run_id, ProviderPermissionResponse("grant", True, scope="work"))
        assert granted is False
    finally:
        bus.off(Method.PROVIDER_EVENT, fail_audit)
        adapter.release.set()
        await record.task_handle
        await runtime.close()


async def test_hot_enable_waits_for_pending_request_publication(monkeypatch):
    from server.event_bus import bus
    from server.protocol import Method

    monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", False, raising=False)
    runtime = runtime_with_authorizer()
    adapter = _InteractiveAdapter()
    runtime.register(adapter)
    record = await runtime.start(ProviderRunRequest(provider=adapter.provider_id, task="Publication race"))
    publishing, release = asyncio.Event(), asyncio.Event()
    order = []

    async def persist(_method, params):
        if params.get("type") == "permission.requested":
            publishing.set()
            await release.wait()
            order.append("requested")
        elif params.get("type") == "permission.allowed":
            order.append("allowed")

    bus.on(Method.PROVIDER_EVENT, persist)
    posting = None
    try:
        await asyncio.wait_for(adapter.started.wait(), 2)
        posting = asyncio.create_task(runtime._emit(record, ProviderEvent(
            provider=adapter.provider_id, run_id=record.run_id, type="permission.requested",
            payload={"permissionRequest": {"request_id": "publishing", "options": ["allow_once", "deny"]}})))
        await asyncio.wait_for(publishing.wait(), 2)
        monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", True)
        await runtime.apply_permission_policy()
        assert adapter.responses == []
        release.set()
        await posting
        assert len(adapter.responses) == 1 and order == ["requested", "allowed"]
    finally:
        release.set()
        if posting is not None:
            await posting
        bus.off(Method.PROVIDER_EVENT, persist)
        adapter.release.set()
        await record.task_handle
        await runtime.close()


@pytest.mark.parametrize("extra", [
    {"diagnosticOnly": True}, {"retryRequired": True}, {"options": ["deny"]},
])
async def test_auto_policy_does_not_replay_retrospective_or_unsupported_requests(monkeypatch, extra):
    monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", True, raising=False)
    runtime = runtime_with_authorizer()
    adapter = _InteractiveAdapter()
    runtime.register(adapter)
    record = await runtime.start(ProviderRunRequest(provider=adapter.provider_id, task="Approval fixture"))
    try:
        await asyncio.wait_for(adapter.started.wait(), 2)
        await runtime._emit(record, ProviderEvent(provider=adapter.provider_id, run_id=record.run_id,
            type="permission.requested", payload={"permissionRequest": {
                "request_id": "retrospective", "options": ["allow_once", "deny"], **extra,
            }}))
        assert adapter.responses == []
    finally:
        adapter.release.set()
        await record.task_handle
        await runtime.close()


async def test_enabling_policy_resolves_existing_pending_request_once(monkeypatch):
    monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", False, raising=False)
    runtime = runtime_with_authorizer()
    adapter = _InteractiveAdapter()
    runtime.register(adapter)
    record = await runtime.start(ProviderRunRequest(provider=adapter.provider_id, task="Pending approval"))
    try:
        await asyncio.wait_for(adapter.started.wait(), 2)
        await runtime._emit(record, ProviderEvent(provider=adapter.provider_id, run_id=record.run_id,
            type="permission.requested", payload={"permissionRequest": {
                "request_id": "already-pending", "options": ["allow_once", "deny"],
            }}))
        assert adapter.responses == []
        monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", True)
        await runtime.apply_permission_policy()
        await runtime.apply_permission_policy()
        assert [response.request_id for response in adapter.responses] == ["already-pending"]
    finally:
        adapter.release.set()
        await record.task_handle
        await runtime.close()


async def test_unknown_automatic_response_is_not_resent(monkeypatch):
    monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", True, raising=False)
    runtime = runtime_with_authorizer()
    adapter = _InteractiveAdapter()

    async def lose_ack(run_id, response):
        adapter.responses.append(response)
        raise ConnectionError("synthetic acknowledgement loss")

    monkeypatch.setattr(adapter, "resolve_permission", lose_ack)
    runtime.register(adapter)
    record = await runtime.start(ProviderRunRequest(provider=adapter.provider_id, task="Unknown response"))
    try:
        await asyncio.wait_for(adapter.started.wait(), 2)
        with pytest.raises(ConnectionError, match="synthetic acknowledgement loss"):
            await runtime._emit(record, ProviderEvent(provider=adapter.provider_id, run_id=record.run_id,
                type="permission.requested", payload={"permissionRequest": {
                    "request_id": "unknown", "options": ["allow_once", "deny"],
                }}))
        await runtime.apply_permission_policy()
        assert len(adapter.responses) == 1
        assert not any(event["type"] == "permission.allowed" for event in record.events)
    finally:
        adapter.release.set()
        await record.task_handle
        await runtime.close()


async def test_one_unknown_response_does_not_leave_another_work_waiting(monkeypatch):
    monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", False, raising=False)
    runtime = runtime_with_authorizer()
    adapter = _InteractiveAdapter()

    async def resolve(run_id, response):
        adapter.responses.append(response)
        if response.request_id == "broken":
            raise ConnectionError("synthetic acknowledgement loss")
        return {"accepted": True}

    monkeypatch.setattr(adapter, "resolve_permission", resolve)
    runtime.register(adapter)
    records = []
    try:
        for name in ("broken", "healthy"):
            record = await runtime.start(ProviderRunRequest(provider=adapter.provider_id, task=name))
            records.append(record)
            await asyncio.wait_for(adapter.started.wait(), 2)
            await runtime._emit(record, ProviderEvent(provider=adapter.provider_id, run_id=record.run_id,
                type="permission.requested", payload={"permissionRequest": {
                    "request_id": name, "options": ["allow_once", "deny"],
                }}))
        monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", True)
        with pytest.raises(ExceptionGroup, match="pending permission"):
            await runtime.apply_permission_policy()
        assert [response.request_id for response in adapter.responses] == ["broken", "healthy"]
        await runtime.apply_permission_policy()
        assert len(adapter.responses) == 2
    finally:
        adapter.release.set()
        await asyncio.gather(*(record.task_handle for record in records if record.task_handle))
        await runtime.close()


async def test_setting_change_remains_visible_when_a_pending_response_is_unknown(monkeypatch):
    from unittest.mock import AsyncMock
    from agent_host.provider_runtime import runtime
    from server.event_bus import bus
    from server.handlers.system_handler import SystemHandler
    from server.protocol import Method

    monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", False)
    monkeypatch.setattr(runtime, "apply_permission_policy", AsyncMock(side_effect=ExceptionGroup(
        "Some pending permission responses could not be confirmed", [ConnectionError("fixture")])) )
    handler = SystemHandler()

    async def config(_params):
        return {"work_permission_auto_approve": settings.WORK_PERMISSION_AUTO_APPROVE}

    monkeypatch.setattr(handler, "_get_config", config)
    changes = []

    async def changed(_method, params):
        changes.append(params)

    bus.on(Method.SYSTEM_CONFIG, changed)
    try:
        with pytest.raises(ExceptionGroup):
            await handler._set_config({"values": {"work_permission_auto_approve": True}})
        assert settings.WORK_PERMISSION_AUTO_APPROVE is True
        assert changes and changes[-1]["values"]["work_permission_auto_approve"] is True
    finally:
        bus.off(Method.SYSTEM_CONFIG, changed)
