"""AUIP preparation enters the existing Work owner through the actual handler."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_host.provider_authoring import auip_authoring_outcome_requirement
from server.auip_runtime import AuipRuntime
from server.auip_app_source import discover_launchable_auip_app
from server.auip_bundle_validation import finalize_staged_auip_web_bundle
from server.handlers.auip_handler import AuipHandler
from server.protocol import Method
from server.cooperative_provider_loop import ChildConversation, ContextBinding
from test_auip_bundle_validation import _bundle
from test_cooperative_auip_entry import entry_host as entry_host
from test_cooperative_pending_turn import pending_host as pending_host


@pytest.mark.parametrize("entry_host", [False], indirect=True)
@pytest.mark.parametrize("role_action", [{"op":"auip"}, None])
@pytest.mark.parametrize("bound_source", [False, True])
async def test_preparation_amends_original_work_then_launches_once(entry_host, role_action, bound_source, tmp_path):
    context, state, launch, item, _, artifact = entry_host
    state.role_action = role_action
    handler = AuipHandler(AuipRuntime(), artifacts=context.host.work, launch=launch,
        current_session_id=lambda:context.session_id)
    context.manager.auip_router = AsyncMock(side_effect=handler.route_control)
    if bound_source:
        ingress = await context.manager._ingress_for(context.session_id)
        loop = ingress.loop
        source = tmp_path / "readonly-conversation"
        source.mkdir()
        child = ChildConversation("read-context", "別の相談", str(source), context.manager.provider,
            replace(context.manager.context_requirements[context.manager.provider], workspace_access="read"))
        loop._state.register(child, initial_binding_token=loop._binding.token)
        loop._binding = ContextBinding(child.child_id, loop._binding.token)
        loop.children[child.child_id] = child
    original_run = context.host.adapter.run
    original_bytes = Path(item.workspace_path, "index.html").read_bytes()
    # Unlike the launch-only fixture, amendment needs the registry's original
    # relative-path ownership fact, as a real completed Work already has.
    context.host.work.register_artifact(item.work_item_id, attempt_id=artifact.attempt_id,
        kind="business.file", title=artifact.title, path=artifact.path, status="registered",
        sha256=artifact.sha256, metadata={"relative_path":"index.html", "attribution":"workspace_window"})
    context.host.adapter.release.clear()

    async def run(request, run_id, emit):
        result = await original_run(request, run_id, emit)
        root = Path(request.cwd)
        assets = _bundle(root)
        manifest = Path(__file__).resolve().parents[1] / "examples/auip-2048/auip.manifest.json"
        (root / "auip.manifest.json").write_bytes(manifest.read_bytes())
        entry = root / "index.html"
        entry.write_bytes(original_bytes + b"\n" + entry.read_bytes())
        finalize_staged_auip_web_bundle(root, materialized_files=tuple(assets))
        return result

    context.host.adapter.run = run
    text = "把刚才那个打开，咱们一起用吧。"
    try:
        await context.handler.send_text(text, session_id=context.session_id, turn_id="prepare-app")
        await asyncio.wait_for(context.handler._stream_task, 4)
        assert context.host.adapter.calls == 1
        assert len(context.host.work.list_work_items()) == 1
        assert len(context.host.work.list_attempts(item.work_item_id)) == 2
        request = context.host.adapter.requests[0]["request"]
        assert request.task == text and request.cwd == item.workspace_path
        assert request.metadata["work"]["work_item_id"] == item.work_item_id
        assert request.metadata["host_outcome_requirement"] == auip_authoring_outcome_requirement(mode="collaborate")
        assert not [p for method,p in state.events if method == Method.AUIP_LAUNCH_REQUESTED]
        receipt = context.manager.ingresses[context.session_id].receipts["prepare-app"]
        assert receipt["state"] == "auip_entry_pending" and receipt["outcome"]["preparing"]
        assert (await context.handler.send_text(text, session_id=context.session_id,
            turn_id="prepare-app"))["status"] == "replayed"
        assert context.host.adapter.calls == 1
        context.host.adapter.release.set()
        await context.finish()
        attempts = context.host.work.list_attempts(item.work_item_id)
        assert attempts[-1].execution_status == "succeeded", attempts[-1]
        app = discover_launchable_auip_app(context.host.work, item.work_item_id)
        assert app is not None
        await launch.on_work_updated(Method.WORK_UPDATED, {"reason":"provider.result"})
        await launch.on_work_updated(Method.WORK_UPDATED, {"reason":"provider.result"})
        opened, = [p for method,p in state.events if method == Method.AUIP_LAUNCH_REQUESTED]
        assert opened["work_item_id"] == item.work_item_id
        assert opened["artifact_id"] == app["artifact_id"] != artifact.artifact_id
        assert original_bytes in Path(item.workspace_path, "index.html").read_bytes()
        if bound_source:
            assert list(source.iterdir()) == [] and loop._binding.child_id == child.child_id
            assert child.work_item_id == "" and child.run_id == ""
    finally:
        context.host.adapter.release.set()
        await context.finish()


@pytest.mark.parametrize("entry_host", [False], indirect=True)
@pytest.mark.parametrize("confirm", [False, True])
async def test_preparation_waits_for_voice_confirmation(entry_host, confirm):
    context, state, launch, item, *_ = entry_host
    handler = AuipHandler(AuipRuntime(), artifacts=context.host.work, launch=launch)
    context.manager.auip_router = AsyncMock(side_effect=handler.route_control)
    context.host.adapter.release.clear()
    text = "把刚才那个打开，咱们一起用吧。"
    try:
        assert await context.launcher.launch(text)
        await asyncio.wait_for(state.queried.wait(), 3)
        assert context.host.adapter.calls == 0
        assert len(context.host.work.list_attempts(item.work_item_id)) == 1
        if confirm:
            assert await context.launcher.resolve(text)
        else:
            await context.launcher.abandon("test_discard")
        await asyncio.gather(context.handler._stream_task, return_exceptions=True)
        assert context.host.adapter.calls == int(confirm)
        assert len(context.host.work.list_attempts(item.work_item_id)) == 1 + int(confirm)
        assert not [p for method,p in state.events if method == Method.AUIP_LAUNCH_REQUESTED]
    finally:
        context.host.adapter.release.set()
        await context.finish()


@pytest.mark.parametrize("entry_host", [False], indirect=True)
@pytest.mark.parametrize("failure", ["rejected", "unknown"])
async def test_preparation_retains_work_handoff_truth_without_replay(entry_host, failure):
    context, state, launch, item, *_ = entry_host
    handler = AuipHandler(AuipRuntime(), artifacts=context.host.work, launch=launch)
    context.manager.auip_router = AsyncMock(side_effect=handler.route_control)
    if failure == "rejected":
        context.manager.work_control = None
    else:
        context.manager.work_executor.dispatch = AsyncMock(side_effect=RuntimeError("handoff uncertain"))
    text = "把刚才那个打开，咱们一起用吧。"
    await context.handler.send_text(text, session_id=context.session_id, turn_id="failed-prepare")
    await context.finish()
    receipt = context.manager.ingresses[context.session_id].receipts["failed-prepare"]
    assert receipt["state"] == ("auip_rejected" if failure == "rejected" else "auip_unknown")
    assert bool(launch._deferred) is (failure == "unknown")
    assert context.host.adapter.calls == 0
    assert len(context.host.work.list_attempts(item.work_item_id)) == 1
    assert not [p for method,p in state.events if method == Method.AUIP_LAUNCH_REQUESTED]
    assert (await context.handler.send_text(text, session_id=context.session_id,
        turn_id="failed-prepare"))["status"] == "replayed"
    assert context.manager.auip_router.await_count == 1


@pytest.mark.parametrize("entry_host", [False], indirect=True)
async def test_preparation_survives_chat_and_explicit_abort_reaches_its_work(entry_host):
    context, state, launch, item, *_ = entry_host
    handler = AuipHandler(AuipRuntime(), artifacts=context.host.work, launch=launch)
    context.manager.auip_router = AsyncMock(side_effect=handler.route_control)
    original_query = context.manager.query

    async def query(messages, **kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] == "user" and frame["current"]["text"] == "换个话题吧。":
            return json.dumps({"action":None, "say":"少し休もう。"})
        return await original_query(messages, **kwargs)

    context.manager.query = query
    context.host.adapter.release.clear()
    try:
        await context.handler.send_text("把刚才那个打开，咱们一起用吧。",
            session_id=context.session_id, turn_id="prep-stop")
        await context.handler._stream_task
        run_id = context.host.adapter.requests[0]["run_id"]
        await context.handler.send_text("换个话题吧。", session_id=context.session_id, turn_id="later-chat")
        await context.handler._stream_task
        assert context.host.runtime.get_run(run_id).status == "running"
        context.host.adapter.result_status = "cancelled"
        result = await context.manager.abort_turn("prep-stop", context.session_id)
        assert result["state"] == "stopped"
        await context.finish()
        attempts = context.host.work.list_attempts(item.work_item_id)
        assert context.host.work.get_attempt(result["attempt_id"]).execution_status == "cancelled", (
            result, [(a.attempt_id, a.execution_status) for a in attempts])
        await launch.on_work_updated(Method.WORK_UPDATED, {"reason":"provider.result"})
        assert launch._deferred == {}
        assert not [p for method,p in state.events if method == Method.AUIP_LAUNCH_REQUESTED]
        assert context.host.adapter.calls == 1
    finally:
        context.host.adapter.release.set()
        await context.finish()
