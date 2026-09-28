"""Recovery identity reaches Work addressing and exact after-Work result entry."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_host.provider_types import ProviderRecoveryContext
from agent_host.work_ledger_store import WorkLedgerStore
from server.attention_request import AttentionRequestCoordinator
from server.auip_app_source import discover_launchable_auip_app
from server.auip_bundle_validation import validate_staged_auip_web_bundle
from server.auip_launch import AuipLaunchCoordinator
from server.protocol import Method
from server.work_ledger_coordinator import WorkLedgerCoordinator
from test_auip_bundle_validation import _bundle
from test_auip_launch import SESSION, _register_file, _seed_app
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_work import send
from test_cooperative_work_conversation import work_conversation_host as work_conversation_host


def recovery_marker(parent, child):
    return {"verified":False, "kind":"app_error", "code":"javascript_page_error",
        "recovery_state":"started", "recovery_root_attempt_id":parent.attempt_id,
        "recovery_ordinal":1, "recovery_claimed_at":1.0,
        "successor_attempt_id":child.attempt_id, "successor_run_id":child.provider_run_id}


def successor(store, parent, *, valid=True, session_id=SESSION):
    recovery = ProviderRecoveryContext(reason="auip_validation_failed",
        root_attempt_id=parent.attempt_id, predecessor_attempt_id=parent.attempt_id,
        feedback="The application entry failed its boot check.")
    child = store.create_attempt(parent.work_item_id, provider=parent.provider,
        task=parent.task, operation_id=parent.operation_id,
        provider_run_id="repair-" + parent.attempt_id,
        metadata={"session_id":session_id,
            **({"provider_recovery":recovery.to_dict()} if valid else {})})
    store.update_attempt(parent.attempt_id,
        metadata={"host_auip_bundle_validation":recovery_marker(parent, child)})
    return child


@pytest.mark.parametrize("valid", [True, False])
async def test_current_work_follows_only_the_authorized_recovery_attempt(
        pending_host, monkeypatch, valid):
    context = pending_host
    original = await send(context, "做个测试页面。", "original-work")
    await context.finish()
    store = context.host.work
    parent = store.get_attempt(original["attempt_id"])
    original_binding = context.host.control.binding(original["effect_id"])
    child = successor(store, parent, valid=valid, session_id=context.session_id)
    child = store.update_attempt(child.attempt_id, execution_status="running")
    old_reader = context.manager.runtime.get_run
    monkeypatch.setattr(context.manager.runtime, "get_run", lambda run_id:
        SimpleNamespace(run_id=run_id, status="running")
        if run_id == child.provider_run_id else old_reader(run_id))
    active = context.manager.active_work_for_recipient(context.session_id, "",
        work_item_id=parent.work_item_id)
    if valid:
        assert active["work_item_id"] == parent.work_item_id
        assert active["attempt_id"] == child.attempt_id
        assert active["run_id"] == child.provider_run_id
        assert active["effect_id"] == original["effect_id"]
        assert active["runtime_attached"] is True
    else:
        assert active is None
    cancel = AsyncMock(return_value={"state":"stopped"})
    monkeypatch.setattr(context.manager, "_cancel_work_run", cancel)
    stopped = await context.manager._stop_active_work(context.session_id, "", {
        "effect_id":original["effect_id"], "work_item_id":parent.work_item_id,
        "attempt_id":parent.attempt_id, "run_id":parent.provider_run_id})
    if valid:
        assert stopped["state"] == "stopped"
        assert cancel.await_args.args[0]["run_id"] == child.provider_run_id
        cancel.assert_awaited_once()
    else:
        assert stopped["state"] == "not_active"
        cancel.assert_not_awaited()
    assert context.host.control.binding(original["effect_id"]) == original_binding
    assert len(store.list_operations(parent.work_item_id)) == 1


@pytest.mark.parametrize("valid", [True, False])
async def test_input_bound_result_accepts_only_its_recovery_successor(tmp_path, valid):
    store = WorkLedgerStore(tmp_path / "work.sqlite3")
    attention = AttentionRequestCoordinator()
    try:
        project = store.create_or_get_project(tmp_path / "project")
        item, parent, _ = _seed_app(store, project, tmp_path, title="Game",
            turn_id="build", terminal=False)
        store.bind_provider_run(parent.attempt_id, "original-run")
        parent = store.update_attempt(parent.attempt_id, execution_status="running")
        store.accept_provider_input(input_id="feature", work_item_id=item.work_item_id,
            run_id=parent.provider_run_id, text="加上暂停，做完再打开。")
        store.finish_provider_input("feature", state="delivered")
        emitted = []

        async def emit(method, payload):
            emitted.append((method, payload))

        launch = AuipLaunchCoordinator(artifacts=store,
            work_roster=WorkLedgerCoordinator(store), attention=attention, emit=emit)
        pending = await launch.route_control({"action":"launch", "target":"delivery",
            "mode":"observe", "after":"work", "_host_work_binding":"active",
            "_host_active_work_attempt_ids":(parent.attempt_id,),
            "_host_work_input_id":"feature"}, session_id=SESSION, turn_id="open-after-input")
        assert pending["deferred"] is True
        store.update_attempt(parent.attempt_id, execution_status="succeeded")
        child = successor(store, parent, valid=valid)
        entry = Path(item.workspace_path) / "index.html"
        entry.write_text("<!doctype html><title>Repaired game</title>", encoding="utf-8")
        artifact = _register_file(store, item, child, entry)
        store.update_attempt(child.attempt_id, execution_status="succeeded")
        await launch.on_work_updated(Method.WORK_UPDATED, {})
        if valid:
            assert len(emitted) == 1
            assert emitted[0][0] == Method.AUIP_LAUNCH_REQUESTED
            assert emitted[0][1]["artifact_id"] == artifact.artifact_id
            assert not launch._deferred
        else:
            assert emitted == []
        assert store.get_provider_input("feature")["attempt_id"] == parent.attempt_id
        assert len(store.list_operations(item.work_item_id)) == 1
    finally:
        attention.reset_for_tests()
        store.close()


@pytest.mark.parametrize("validation", ["legacy", "pending", "failed", "passed"])
def test_new_boot_contract_gates_launch_without_reclassifying_legacy_apps(tmp_path, validation):
    store = WorkLedgerStore(tmp_path / "work.sqlite3")
    try:
        project = store.create_or_get_project(tmp_path / "project")
        root = tmp_path / "app"
        assets = _bundle(root)
        item = store.create_work_item(project.project_id, title="App", workspace_path=root)
        attempt = store.create_attempt(item.work_item_id, provider="fixture", task="Build the app")
        for name in ("index.html", "auip.manifest.json"):
            _register_file(store, item, attempt, root / name)
        result = validate_staged_auip_web_bundle(root, materialized_files=tuple(assets),
            expected_assets=assets)
        if validation != "legacy":
            result.update(boot={"ok":validation == "passed"},
                verified=validation == "passed",
                kind="app_error" if validation == "failed" else validation)
        store.update_attempt(attempt.attempt_id, execution_status="succeeded", metadata={
            "auip_host_validates_bundle":True, "auip_bundle_root":str(root),
            "auip_host_materialized_files":list(assets), "auip_host_materialized_assets":assets,
            "host_auip_bundle_validation":result})
        candidate = discover_launchable_auip_app(store, item.work_item_id)
        assert bool(candidate) is (validation in {"legacy", "passed"})
    finally:
        store.close()


async def test_main_chat_and_task_stop_continue_during_host_boot_validation(
        work_conversation_host, monkeypatch):
    context, native = work_conversation_host
    entered, release = asyncio.Event(), asyncio.Event()
    work_id = ""

    async def validate(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {"verified":False, "kind":"app_error", "code":"javascript_page_error",
            "detail":"invalid initial choice", "boot":{"ok":False}, "checks":[]}

    async def role(messages, **_kwargs):
        try:
            frame = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            return json.dumps({"references":["work_item:" + work_id]})
        if frame.get("source_kind") != "user":
            return "確認したわ。"
        source = frame["current"]["text"]
        action = ({"op":"work", "intent":"execute"} if source == "做个游戏。" else
            {"op":"interrupt", "target":"work_item:" + work_id} if source == "这个任务先停下。" else None)
        return json.dumps({"say":"わかったわ。", "action":action})

    context.manager.query = role
    context.host.coordinator._provider_start = context.host.runtime.start
    context.host.coordinator._provider_cancel = context.host.runtime.cancel
    monkeypatch.setattr("server.auip_bundle_validation.validate_auip_web_bundle_execution", validate)
    native.release.clear()
    first = await send(context, "做个游戏。", "boot-work")
    await asyncio.wait_for(native.started.wait(), 3)
    work_id = first["work_item_id"]
    store = context.host.work
    item = store.get_work_item(work_id)
    # Fixed Host preparation facts isolate cancellation/delivery from authoring.
    store.update_attempt(first["attempt_id"], metadata={
        "auip_host_validates_bundle":True, "auip_bundle_root":item.workspace_path,
        "host_outcome_requirement":{"operation":"prepare", "facet":"auip.application",
            "expected":{"current_attempt_contribution":True}},
        "host_auip_bundle_validation":{"verified":False,
            "code":"auip_validation_pending", "boot":None, "checks":[]}})
    native.release.set()
    try:
        await asyncio.wait_for(entered.wait(), 3)
        chat = await send(context, "今天有点累了。", "chat-during-boot")
        assert chat["state"] == "no_action" and not release.is_set()
        stopped = await send(context, "这个任务先停下。", "stop-during-boot")
        assert stopped["state"] == "stopped", stopped
        assert not release.is_set()
        release.set()
        await context.finish()
        assert len(native.requests) == 1
        attempts = store.list_attempts(work_id)
        assert len(attempts) == 1 and attempts[0].execution_status == "succeeded"
        assert attempts[0].metadata["host_auip_bundle_validation"]["recovery_state"] == "cancelled"
    finally:
        release.set()
