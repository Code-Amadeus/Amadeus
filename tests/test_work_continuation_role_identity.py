"""Same-Operation recovery preserves accepted identity at the native boundary."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import fields, replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_host.adapters.codex_app_server import CodexAppServerAdapter
from agent_host.provider_contract import ProviderCapabilities, ProviderManifest
from agent_host.provider_identity import HISTORICAL_MAIN_ROLE_NAME
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderEvent, ProviderRunIntakeAuthority, ProviderRunRequest, ProviderRunResult
from server.control_ledger import ControlLedgerConflict
from server.event_bus import bus
from server.handlers.provider_handler import ProviderHandler
from server.handlers.work_ledger_handler import WorkLedgerHandler
from server.protocol import Method
from server.work_control import WorkAmendPayloadV4, WorkControl, WorkEffectPayloadV3
from server.work_ledger_coordinator import WorkLedgerCoordinator
from test_codex_app_server_adapter import _FakeCodex, _FakeThread, _FakeTurn, _turn_completed
from test_provider_request_role_identity import mira_startup_without_old_pack as mira_startup_without_old_pack
from test_work_control_batch import _admission, _open, _payload
from test_work_role_identity import MIRA, _legacy_accept


TASK = "You yourself build the accepted artifact."


@asynccontextmanager
async def journey(tmp_path, monkeypatch, *, action="retry", legacy=False, origin=True, projection=None):
    ledger, work, _, project, admission = _open(tmp_path, source=TASK)
    resolver = Mock(return_value=MIRA)
    control = WorkControl(ledger, work, source_role_identity_resolver=resolver)
    sdk = _FakeCodex([_FakeThread(f"native-{index}", _FakeTurn(f"turn-{index}",
        [_turn_completed(f"turn-{index}", "failed")])) for index in range(3)])
    adapter = CodexAppServerAdapter(codex=sdk)
    payload = replace(_payload(admission, project.project_id, TASK, source=TASK), provider=adapter.provider_id)
    effect_id = (_legacy_accept(control, admission, (payload,), {})[0] if legacy
        else control.seal(admission, payload)["effect_id"]) if origin else None
    plan = ledger.get_admission(admission.root_id)["plan_json"]
    coordinator = WorkLedgerCoordinator(work, work_control=control)
    runtime = ProviderRuntime()
    runtime.register(adapter)
    runtime.set_request_preparer(coordinator.prepare_request)
    coordinator.configure()
    monkeypatch.setattr("config.settings.WORK_WORKTREE_ISOLATION", False)
    monkeypatch.setattr("server.work_ledger_coordinator.cwd_in_project_registry", lambda _path: True)
    monkeypatch.setattr("server.handlers.provider_handler.runtime", runtime)
    provider_handler = ProviderHandler.__new__(ProviderHandler)
    handler = WorkLedgerHandler(coordinator, provider_run=provider_handler.run_provider)
    try:
        request = (control.provider_request(effect_id) if effect_id else
            ProviderRunRequest(adapter.provider_id, TASK, cwd=project.canonical_path,
                requirements=payload.requirements))
        prepared = coordinator.prepare_request(request, "initial-run",
            ProviderRunIntakeAuthority(effect_id) if effect_id else None)
        request = prepared.request if effect_id else prepared
        binding = request.metadata["work"]
        attempt_id = binding["attempt_id"]
        # This is the crash/failure boundary before run.created: the durable
        # Operation already owns acceptance; no event has copied request facts.
        assert "main_role_name" not in work.get_attempt(attempt_id).metadata
        work.update_attempt(attempt_id, execution_status="orphaned" if action == "resume" else "failed",
            metadata={**(projection or {}), **({"runtime_resumable": True} if action == "resume" else {})})
        work.release_writer_lease(attempt_id, status="stale")
        if action == "resume":
            runtime.add_orphaned_run(provider=adapter.provider_id, run={"run_id": "initial-run",
                "task": TASK, "cwd": request.cwd,
                "metadata": {"runtime_resumable": True, "main_role_name": "Runtime projection"}})
        # A different startup role and missing/renamed original pack must never
        # be consulted by the recovery path.
        resolver.side_effect = AssertionError("continuation cannot resolve the current role")
        yield SimpleNamespace(ledger=ledger, work=work, control=control, coordinator=coordinator,
            runtime=runtime, sdk=sdk, handler=handler, request=request, binding=binding,
            effect_id=effect_id, admission=admission, payload=payload, plan=plan, resolver=resolver)
    finally:
        await runtime.close()
        await coordinator.drain_provider_facts()
        coordinator.close()
        ledger.close()
        work.close()


async def recover(host, action, *, metadata=None):
    result = await host.handler.handle(Method.WORK_RESUME if action == "resume" else Method.WORK_RETRY,
        {"work_item_id": host.binding["work_item_id"], "metadata": metadata or {}})
    record = host.runtime.get_run(result["run"]["run_id"])
    await asyncio.wait_for(record.task_handle, timeout=10)
    await host.coordinator.drain_provider_facts()
    return record


def assert_native_identity(host, expected):
    prompt = host.sdk.injected_items[-1][1][0]["content"][0]["text"]
    assert f'main role is "{expected}"' in prompt
    assert "'yourself'" in prompt
    assert "Mira after rename" not in prompt and "Caller override" not in prompt


@pytest.mark.parametrize("action", ["retry", "resume"])
async def test_handler_recovers_accepted_role_before_first_event_and_overrides_caller(
        tmp_path, monkeypatch, mira_startup_without_old_pack, action):
    async with journey(tmp_path, monkeypatch, action=action,
            projection={"main_role_name": None, "provider_result": {"main_role_name": "Wrong projection"}}) as host:
        first = await recover(host, action, metadata={"main_role_name": "Caller override"})
        assert first.metadata["main_role_name"] == "Mira"
        assert_native_identity(host, "Mira")
        attempts = host.work.list_attempts(host.binding["work_item_id"])
        assert len(attempts) == (1 if action == "resume" else 2)
        if action == "retry":
            second = await recover(host, action, metadata={"main_role_name": False})
            assert second.metadata["main_role_name"] == "Mira"
            attempts = host.work.list_attempts(host.binding["work_item_id"])
            assert len(attempts) == 3 and attempts[-1].origin_effect_id == ""
            assert {attempt.operation_id for attempt in attempts} == {host.binding["operation_id"]}
            assert_native_identity(host, "Mira")
        assert host.ledger.get_admission(host.admission.root_id)["plan_json"] == host.plan
        mira_startup_without_old_pack.assert_not_called()


@pytest.mark.parametrize("action", ["retry", "resume"])
async def test_old_accepted_field_absence_uses_historical_name_without_rewriting_plan(
        tmp_path, monkeypatch, mira_startup_without_old_pack, action):
    async with journey(tmp_path, monkeypatch, action=action, legacy=True,
            projection={"main_role_name": "Mira"}) as host:
        assert "role_identity" not in json.loads(host.plan)["evidence"]
        record = await recover(host, action, metadata={"main_role_name": "Caller override"})
        assert record.metadata["main_role_name"] == HISTORICAL_MAIN_ROLE_NAME
        assert_native_identity(host, HISTORICAL_MAIN_ROLE_NAME)
        assert host.ledger.get_admission(host.admission.root_id)["plan_json"] == host.plan
        mira_startup_without_old_pack.assert_not_called()


@pytest.mark.parametrize("action", ["retry", "resume"])
@pytest.mark.parametrize("projection,expected", [
    ({"main_role_name": "Mira", "provider_result": {"main_role_name": "Old result"}}, "Mira"),
    ({"provider_result": {"main_role_name": "Mira"}}, "Mira"),
    ({"provider_result": {}}, HISTORICAL_MAIN_ROLE_NAME),
])
async def test_legacy_attempt_reads_existing_persisted_request_projection(
        tmp_path, monkeypatch, action, projection, expected):
    async with journey(tmp_path, monkeypatch, action=action, origin=False, projection=projection) as host:
        record = await recover(host, action, metadata={"main_role_name": "Caller override"})
        assert record.metadata["main_role_name"] == expected
        assert_native_identity(host, expected)


@pytest.mark.parametrize("action", ["retry", "resume"])
@pytest.mark.parametrize("source", ["canonical", "attempt", "provider_result"])
@pytest.mark.parametrize("bad", [None, "", False])
async def test_bad_persisted_identity_rejects_before_dispatch_or_writer_lease(
        tmp_path, monkeypatch, action, source, bad):
    projection = ({"main_role_name": bad} if source == "attempt" else
        {"provider_result": {"main_role_name": bad}} if source == "provider_result" else {})
    async with journey(tmp_path, monkeypatch, action=action, origin=source == "canonical", projection=projection) as host:
        if source == "canonical":
            plan = json.loads(host.plan)
            plan["evidence"]["role_identity"]["display_name"] = bad
            with host.ledger._transaction() as db:
                db.execute("UPDATE control_admissions SET plan_json=? WHERE root_id=?",
                    (json.dumps(plan), host.admission.root_id))
        before = host.work.get_attempt(host.binding["attempt_id"])
        lease = host.work.get_writer_lease(before.attempt_id)
        with pytest.raises((ControlLedgerConflict, ValueError), match="role_identity|main_role_name"):
            await recover(host, action, metadata={"main_role_name": "Mira"})
        assert host.work.get_attempt(before.attempt_id) == before
        assert host.work.get_writer_lease(before.attempt_id) == lease
        assert host.sdk.injected_items == [] and len(host.work.list_attempts(before.work_item_id)) == 1


async def test_new_accepted_amend_and_its_retry_use_new_role(tmp_path, monkeypatch):
    async with journey(tmp_path, monkeypatch) as host:
        host.resolver.side_effect = None
        host.resolver.return_value = {"character_id": "other", "display_name": "New Role"}
        source = "You yourself add the revised requirement."
        admission = _admission(suffix="new-role", epoch=2, source=source)
        host.control.admit(admission, fence_scope="foreground-chat")
        base = replace(_payload(admission, host.payload.project_id, source, source=source),
            provider=host.payload.provider)
        payload = WorkAmendPayloadV4(**{field.name: getattr(base, field.name)
            for field in fields(WorkEffectPayloadV3)}, work_item_id=host.binding["work_item_id"])
        effect_id = host.control.seal(admission, payload)["effect_id"]
        request = host.control.provider_request(effect_id)
        record = await host.runtime.start_accepted(request, ProviderRunIntakeAuthority(effect_id))
        await asyncio.wait_for(record.task_handle, timeout=10)
        await host.coordinator.drain_provider_facts()
        assert record.metadata["main_role_name"] == "New Role"
        assert_native_identity(host, "New Role")
        retried = await recover(host, "retry", metadata={"main_role_name": "Mira"})
        assert retried.metadata["main_role_name"] == "New Role"
        assert_native_identity(host, "New Role")
        assert host.control.accepted_main_role_name(host.effect_id) == "Mira"
        assert host.ledger.get_admission(host.admission.root_id)["plan_json"] == host.plan


@pytest.mark.parametrize("name", [None, "Mira"])
async def test_runtime_provider_events_and_results_cannot_mint_or_replace_host_role(name):
    class SpoofingAdapter:
        provider_id = "role-spoof"
        manifest = ProviderManifest(provider_id=provider_id, display_name="Role spoof",
            capabilities=ProviderCapabilities(task_kinds=("general",), workspace_access="none",
                workspace_ownership="none"))

        async def run(self, _request, run_id, emit):
            await emit(ProviderEvent(self.provider_id, run_id, "assistant.update",
                payload={"text": "observed"}, metadata={"main_role_name": "Provider spoof"}))
            return ProviderRunResult(status="done", metadata={"main_role_name": "Provider spoof"})

    runtime = ProviderRuntime()
    runtime.register(SpoofingAdapter())
    events, results = [], []
    async def capture_event(_method, params):
        if params["provider"] == SpoofingAdapter.provider_id:
            events.append(params)
    async def capture_result(_method, params):
        if params["provider"] == SpoofingAdapter.provider_id:
            results.append(params)
    bus.on(Method.PROVIDER_EVENT, capture_event)
    bus.on(Method.PROVIDER_RESULT, capture_result)
    try:
        record = await runtime.start(ProviderRunRequest(SpoofingAdapter.provider_id, "Observe.",
            metadata={"main_role_name": name} if name is not None else {}))
        await asyncio.wait_for(record.task_handle, timeout=10)
        assert record.metadata.get("main_role_name") == name
        assert results and events
        assert all(row["metadata"].get("main_role_name") == name for row in events + results)
        if name is None:
            assert "main_role_name" not in record.metadata
            assert all("main_role_name" not in row["metadata"] for row in events + results)
    finally:
        bus.off(Method.PROVIDER_EVENT, capture_event)
        bus.off(Method.PROVIDER_RESULT, capture_result)
        await runtime.close()
