"""Cold Host context records are not a second native execution owner."""
import asyncio
import gc
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import weakref

import pytest

from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderSessionHandle
from server.attention_request import AttentionRequestCoordinator
from server.control_ledger import ControlLedgerStore
from server.cooperative_context_store import CooperativeContextStore
from server.cooperative_provider_loop import CooperativeProviderLoop
from server.cooperative_chat_ingress import CooperativeChatManager
from test_cooperative_context_recovery import NativeFixture, host_factory  # noqa: F401


@pytest.fixture
def cache_host(tmp_path):
    ledger = ControlLedgerStore(tmp_path / "contexts.sqlite3")
    state = CooperativeContextStore(ledger, "cache-session")
    runtime, adapter = ProviderRuntime(), NativeFixture()
    runtime.register(adapter)
    query = AsyncMock(return_value='{"action":null,"say":"了解。"}')
    requirements = ProviderRequirements(workspace_access="write", workspace_ownership="caller", resume="attach")

    def allocate(_label, context_id):
        path = tmp_path / context_id
        path.mkdir()
        return path

    def make_loop(durable=True):
        loop = CooperativeProviderLoop(runtime, query, allocate, provider=adapter.provider_id,
            context_requirements={adapter.provider_id:requirements}, owns_runtime=False,
            idle_context_budget=2, publish=lambda _:True)
        if durable:
            loop.attach_state(state, install_runtime_hooks=False)
        return loop

    host = SimpleNamespace(ledger=ledger, state=state, runtime=runtime, adapter=adapter,
        query=query, make_loop=make_loop, loop=make_loop())
    try:
        yield host
    finally:
        ledger.close()


def seed(host, number, *, status="done"):
    child = host.loop._create_context(f"Context {number}", host.adapter.provider_id)
    host.loop._save_child(child, run_status=status, run_id=f"old-run-{number}",
        output=f"payload-{number}:" + ("x" * 100_000),
        native_session=ProviderSessionHandle(provider=host.adapter.provider_id,
            session_id=f"native-{number}", scope="interaction"))
    return child.child_id, weakref.ref(child)


async def test_startup_and_ordinary_frame_keep_idle_binding_cold(cache_host):
    host = cache_host
    ids, refs = zip(*(seed(host, number) for number in range(12)))
    host.loop.bind_context(ids[0])
    binding = host.state.load_catalog()[0]
    host.loop.children.clear()
    gc.collect()
    assert all(ref() is None for ref in refs)
    with patch.object(host.state, "load_context", wraps=host.state.load_context) as full_read:
        restored = host.make_loop()
        assert restored.children == {} and restored.bound_context_id == ids[0]
        result = await restored._decide({"source":"user", "text":"こんにちは"})
        assert result["action"] is None
        frame = json.loads(host.query.call_args.args[0][-1]["content"])
        assert frame["context"]["id"] == ids[0]
        assert len(frame["context"]["last_provider_output"]) == 4000
        assert len(frame["retained_contexts"]) == 2
        assert frame["retained_contexts_complete"] is False
        assert len(restored.context_catalog()) == 12
        assert restored.children == {}
        full_read.assert_not_called()
    assert host.state.load_catalog()[0] == binding
    assert host.adapter.requests == [] and host.adapter.inspections == []


async def test_eviction_releases_objects_but_borrowed_context_keeps_one_lock(cache_host):
    host = cache_host
    ids, refs = zip(*(seed(host, number) for number in range(10)))
    gc.collect()
    assert len(host.loop.children) == 2
    assert sum(ref() is not None for ref in refs) == 2
    borrowed = host.loop.get_context(ids[0])
    original_lock = borrowed.lock
    for context_id in ids[1:]:
        host.loop.get_context(context_id)
    assert ids[0] not in host.loop.children
    assert host.loop.get_context(ids[0]) is borrowed
    assert host.loop.get_context(ids[0]).lock is original_lock
    await original_lock.acquire()
    for context_id in ids[1:]:
        host.loop.get_context(context_id)
    assert ids[0] in host.loop.children
    assert host.loop.get_context(ids[0]) is borrowed
    original_lock.release()
    host.loop.trim_contexts()
    assert len(host.loop.children) == 2
    assert borrowed.native_session.session_id == "native-0"
    assert host.adapter.requests == []


def test_active_and_unresolved_contexts_are_restored_and_not_evicted(cache_host):
    host = cache_host
    protected = [seed(host, number, status=status)[0]
        for number, status in enumerate(("dispatching", "queued", "running", "orphaned"))]
    for number in range(4, 10):
        seed(host, number)
    with patch.object(host.state, "load_context", wraps=host.state.load_context) as full_read:
        restored = host.make_loop()
        assert set(restored.children) == set(protected)
        assert full_read.call_count == 4
        for row in restored.context_catalog():
            restored.get_context(row["context_id"])
        assert set(protected) <= set(restored.children)
        assert len(restored.children) == 6
    assert host.adapter.requests == [] and host.adapter.inspections == []


async def test_complete_address_and_scope_candidates_survive_eviction(cache_host):
    host = cache_host
    ids = {seed(host, number)[0] for number in range(8)}
    host.query.return_value = json.dumps({"action":{"op":"send_to","provider":host.adapter.provider_id},
        "say":"前の宛先へ送るわ。"})
    with patch.object(host.state, "load_context", wraps=host.state.load_context) as full_read:
        receipt = await host.loop.submit("前のProviderに返信して", input_id="address")
        frame = json.loads(host.query.call_args.args[0][-1]["content"])
        assert len(frame["retained_contexts"]) == 2
        assert frame["retained_contexts_complete"] is False
        assert receipt["state"] == "address_selection_required"
        assert set(receipt["candidate_context_ids"]) == ids
        manager = CooperativeChatManager.__new__(CooperativeChatManager)
        manager.runtime = host.runtime
        manager.destination = None
        manager.attention = AttentionRequestCoordinator()
        ingress = SimpleNamespace(loop=host.loop, session_id="cache-session")
        await manager.request_scope_change(ingress, "scope", {"target":""})
        request, = manager.attention.list_pending("cache-session")
        existing = [option for option in request["options"] if option.get("metadata", {}).get("relation") == "existing"]
        assert len(existing) == 8
        full_read.assert_not_called()
    assert len(host.loop.children) == 2


async def test_preview_prefers_resident_access_without_claiming_cold_recency(cache_host):
    host = cache_host
    ids = [seed(host, number)[0] for number in range(6)]
    host.loop.get_context(ids[0])
    host.loop.get_context(ids[2])
    await host.loop._decide({"source":"user", "text":"以前の宛先について話したい"})
    frame = json.loads(host.query.call_args.args[0][-1]["content"])
    assert [row["label"] for row in frame["retained_contexts"]] == ["Context 2", "Context 0"]
    assert frame["retained_contexts_complete"] is False
    assert len(host.loop.context_catalog()) == 6


async def test_small_preview_reports_complete_only_when_all_retained_contexts_fit(cache_host):
    host = cache_host
    seed(host, 0)
    seed(host, 1)
    await host.loop._decide({"source":"user", "text":"こんにちは"})
    frame = json.loads(host.query.call_args.args[0][-1]["content"])
    assert len(frame["retained_contexts"]) == 2
    assert frame["retained_contexts_complete"] is True


async def test_preview_completeness_covers_open_noncurrent_contexts(cache_host):
    host = cache_host
    current, _ = seed(host, 0)
    seed(host, 1)
    closed, _ = seed(host, 2)
    host.loop._save_child(host.loop.get_context(closed), closed=True)
    host.loop.bind_context(current)
    await host.loop._decide({"source":"user", "text":"以前の宛先は？"})
    frame = json.loads(host.query.call_args.args[0][-1]["content"])
    assert [row["label"] for row in frame["retained_contexts"]] == ["Context 1"]
    assert frame["retained_contexts_complete"] is True
    assert len(host.loop.context_catalog()) == 3


def test_transient_loop_never_evicts_its_only_copy(cache_host):
    host = cache_host
    host.loop = host.make_loop(durable=False)
    ids = {seed(host, number)[0] for number in range(7)}
    host.loop.trim_contexts()
    assert set(host.loop.children) == ids
    assert len(host.loop.snapshot()) == 7


async def test_cold_bound_resume_reuses_native_identity_and_source_replay(request):
    host = request.getfixturevalue("host_factory")()
    host.loop._idle_context_budget = 2
    try:
        first = await host.send("检查目录。", "cache-first")
        await host.loop.wait()
        original = host.loop.get_context(first["child_id"])
        native, workspace, context_id = original.native_session, original.workspace, original.child_id
        token = host.loop._binding.token
        del original
        for number in range(6):
            host.loop._create_context(f"Other {number}", host.adapter.provider_id)
        gc.collect()
        assert context_id not in host.loop.children
        with patch.object(host.loop._state, "load_context", wraps=host.loop._state.load_context) as load:
            second = await host.send("再检查原目录。", "cache-resume")
            await host.loop.wait()
            assert second["state"] == "started"
            assert any(call.args == (context_id,) for call in load.call_args_list)
        assert len(host.adapter.requests) == 2
        assert host.adapter.requests[-1].session == native
        assert host.adapter.requests[-1].cwd == workspace
        assert host.loop._binding.token == token
        assert (await host.send("再检查原目录。", "cache-resume"))["status"] == "replayed"
        assert len(host.adapter.requests) == 2
    finally:
        await host.close()


async def test_cache_pressure_does_not_cancel_live_run_and_settlement_trims_it(request):
    host = request.getfixturevalue("host_factory")()
    host.loop._idle_context_budget = 2
    host.adapter.release.clear()
    try:
        first = await host.send("检查目录。", "active-cache")
        await host.adapter.started.wait()
        for number in range(8):
            host.loop._create_context(f"Idle {number}", host.adapter.provider_id)
        assert host.runtime.get_run(first["run_id"]).status == "running"
        assert first["child_id"] in host.loop.children
        assert not host.adapter.release.is_set()
        assert len(host.loop.children) == 3  # One active owner plus two idle entries.
        host.adapter.release.set()
        await host.loop.wait()
        assert host.runtime.get_run(first["run_id"]).status == "done"
        assert len(host.loop.children) == 2
    finally:
        host.adapter.release.set()
        await host.close()


async def test_monitor_retains_canonical_object_even_if_idle_entry_is_trimmed(request):
    host = request.getfixturevalue("host_factory")()
    host.loop._idle_context_budget = 2
    entered, release = asyncio.Event(), asyncio.Event()
    query = host.loop.query

    async def slow_expression(messages):
        if json.loads(messages[-1]["content"])["source_kind"] == "provider":
            entered.set()
            await release.wait()
            return "確認できたわ。"
        return await query(messages)

    host.loop.query = slow_expression
    try:
        first = await host.send("检查目录。", "monitor-cache")
        await entered.wait()
        original = weakref.ref(host.loop.get_context(first["child_id"]))
        for number in range(8):
            host.loop._create_context(f"Idle {number}", host.adapter.provider_id)
        gc.collect()
        assert first["child_id"] not in host.loop.children
        assert original() is not None  # The unfinished monitor still owns this object.
        assert host.loop.get_context(first["child_id"]) is original()
        release.set()
        await host.loop.wait()
        assert len(host.adapter.requests) == 1
    finally:
        release.set()
        await host.close()
