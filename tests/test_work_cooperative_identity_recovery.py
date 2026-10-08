"""Accepted V6 Work resolves its persisted recipient without a speaking loop."""
from dataclasses import fields, replace
import json
from unittest.mock import AsyncMock

import pytest

from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderRunIntakeAuthority, ProviderSessionHandle
from agent_host.work_ledger_store import WorkLedgerStore
from server.control_ledger import ControlLedgerConflict, ControlLedgerStore
from server.cooperative_chat_ingress import CooperativeChatManager
from server.cooperative_context_store import CooperativeContextStore
from server.cooperative_provider_loop import ChildConversation
from server.handlers.chat_handler import ChatHandler
from server.work_control import WorkControl, WorkCooperativeContextPayloadV6
from server.work_destination_service import WorkDestinationService
from server.work_effect_executor import WorkEffectExecutor
from server.work_ledger_coordinator import WorkLedgerCoordinator
from test_provider_request_role_identity import mira_startup_without_old_pack as mira_startup_without_old_pack
from test_work_control_batch import ALPHA, _admission, _payload


KURISU = {"character_id": "kurisu", "display_name": "Makise Kurisu (牧瀬紅莉栖)"}


def manager(ledger, work, requirements):
    return CooperativeChatManager(ChatHandler(), ledger=ledger, fence_scope="v6-role-recovery",
        provider="inert-provider", runtime=ProviderRuntime(),
        context_requirements={"inert-provider": requirements}, query=AsyncMock(),
        allocate=lambda *_args: (_ for _ in ()).throw(AssertionError("recovery cannot create a context")),
        destination=WorkDestinationService(work, registry_check=lambda _path: True))


@pytest.fixture
def accepted(tmp_path):
    database = tmp_path / "shared.sqlite3"
    workspace = tmp_path / "project"
    workspace.mkdir()
    work, ledger = WorkLedgerStore(database), ControlLedgerStore(database)
    project = work.create_or_get_project(workspace)
    admission = _admission(suffix="v6-old-character")
    base = _payload(admission, project.project_id, ALPHA)
    context = CooperativeContextStore(ledger, admission.session_id)
    binding, _ = context.load()
    child = ChildConversation("accepted-context", "Existing context", str(workspace),
        base.provider, base.requirements, workspace_route={"projectId": project.project_id})
    context.register(child, initial_binding_token=binding["token"])
    child.native_session = ProviderSessionHandle(base.provider, "old-native-context", "interaction")
    child.run_status = "done"
    context.checkpoint(child)
    payload = WorkCooperativeContextPayloadV6(**{field.name: getattr(base, field.name) for field in fields(base)},
        cooperative_context_id=child.child_id, cooperative_binding_token=binding["token"],
        cooperative_context_revision=child.revision)
    host = manager(ledger, work, payload.requirements)
    control = WorkControl(ledger, work, cooperative_context_resolver=host.resolve_work_recipient,
                          source_role_identity_resolver=lambda _session: KURISU)
    control.admit(admission, fence_scope="v6-role-recovery")
    effect_id = control.seal(admission, payload)["effect_id"]
    request = control.provider_request(effect_id)
    plan = ledger.get_admission(admission.root_id)["plan_json"]
    assert host.ingresses == {}
    ledger.close()
    work.close()
    return database, payload, effect_id, request, plan


def reopened(accepted):
    database, payload, effect_id, request, plan = accepted
    work, ledger = WorkLedgerStore(database), ControlLedgerStore(database)
    host = manager(ledger, work, payload.requirements)
    control = WorkControl(ledger, work, cooperative_context_resolver=host.resolve_work_recipient,
        source_role_identity_resolver=lambda _session: (_ for _ in ()).throw(AssertionError("accepted identity is frozen")))
    return work, ledger, host, control


def test_unbound_accepted_v6_reconstructs_and_validates_after_other_character_startup(
        accepted, mira_startup_without_old_pack, monkeypatch):
    _, payload, effect_id, original, plan = accepted
    work, ledger, host, control = reopened(accepted)
    monkeypatch.setattr(CooperativeContextStore, "__init__",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("recipient reads cannot initialize state")))
    try:
        rows = [dict(row) for row in ledger._db.execute("SELECT * FROM cooperative_contexts")]
        bindings = [dict(row) for row in ledger._db.execute("SELECT * FROM cooperative_bindings")]
        request = control.provider_request(effect_id)
        assert request == original
        assert request.metadata["main_role_name"] == KURISU["display_name"]
        control.validate_runtime_request(ProviderRunIntakeAuthority(effect_id), request)
        attachment = control.addressed_context(ProviderRunIntakeAuthority(effect_id))
        assert attachment.session.session_id == "old-native-context"
        assert host.ingresses == {} and ledger.get_admission(ledger.get_effect(effect_id)["root_id"])["plan_json"] == plan
        assert [dict(row) for row in ledger._db.execute("SELECT * FROM cooperative_contexts")] == rows
        assert [dict(row) for row in ledger._db.execute("SELECT * FROM cooperative_bindings")] == bindings
        # A later foreground recipient selection does not redirect an accepted
        # effect; new acceptance still validates its current binding separately.
        with ledger._transaction() as db:
            db.execute("UPDATE cooperative_bindings SET token='later-selection' WHERE session_id=?", (payload.session_id,))
        assert control.provider_request(effect_id) == original
        with pytest.raises(ControlLedgerConflict, match="binding changed"):
            CooperativeContextStore.persisted_work_recipient(ledger, payload.session_id, payload,
                                                             require_current_binding=True)
        mira_startup_without_old_pack.assert_not_called()
    finally:
        ledger.close()
        work.close()


@pytest.mark.parametrize("change", ["revision", "closed", "queued", "running", "orphaned", "provider", "native", "workspace"])
def test_persisted_v6_recipient_keeps_existing_recovery_boundaries(accepted, mira_startup_without_old_pack, change):
    work, ledger, host, control = reopened(accepted)
    try:
        changes = {
            "revision": ("revision", 99), "closed": ("closed", 1),
            "queued": ("run_status", "queued"), "running": ("run_status", "running"),
            "orphaned": ("run_status", "orphaned"), "provider": ("provider", "foreign"),
            "native": ("native_session", json.dumps(ProviderSessionHandle("foreign", "wrong", "interaction").to_dict())),
            "workspace": ("workspace", ""),
        }
        column, value = changes[change]
        with ledger._transaction() as db:
            db.execute(f"UPDATE cooperative_contexts SET {column}=?", (value,))
        with pytest.raises(ControlLedgerConflict):
            control.provider_request(accepted[2])
        assert host.ingresses == {} and control.binding(accepted[2]) is None
        mira_startup_without_old_pack.assert_not_called()
    finally:
        ledger.close()
        work.close()


async def test_bound_v6_replay_does_not_reconstruct_or_dispatch_again(accepted, mira_startup_without_old_pack, monkeypatch):
    work, ledger, host, control = reopened(accepted)
    coordinator = WorkLedgerCoordinator(work, work_control=control)
    runtime = ProviderRuntime()
    start = AsyncMock(side_effect=AssertionError("a bound effect cannot be redispatched"))
    monkeypatch.setattr(runtime, "start_accepted", start)
    try:
        authority = ProviderRunIntakeAuthority(accepted[2])
        request = control.provider_request(accepted[2])
        attachment = control.addressed_context(authority)
        prepared = replace(request, session=attachment.session)
        facts = request.metadata["work"]
        bound = control.bind_runtime_dispatch_intent(authority, prepared, provider_run_id="old-bound-run",
            project_id=facts["project_id"], title=facts["title"], goal=request.task,
            workspace_mode=facts["workspace_mode"], workspace_path=facts["workspace_path"],
            branch="", base_revision="", work_metadata={}, operation_metadata={},
            attempt_metadata={"provider_session": attachment.session.to_dict(),
                              "provider_session_attach": attachment.audit,
                              "main_role_name": request.metadata["main_role_name"]})
        original = bound["binding"]
        monkeypatch.setattr(control, "provider_request",
            lambda *_args: (_ for _ in ()).throw(AssertionError("bound replay uses its durable binding")))
        executor = WorkEffectExecutor(control, runtime, coordinator)
        first, second = await executor.dispatch(accepted[2]), await executor.dispatch(accepted[2])
        assert first.status == second.status == "bound_without_runtime"
        assert first.binding == second.binding == original
        assert len(work.list_attempts(original["work_item_id"])) == 1
        assert host.ingresses == {}
        start.assert_not_awaited()
        mira_startup_without_old_pack.assert_not_called()
    finally:
        coordinator.close()
        ledger.close()
        work.close()
