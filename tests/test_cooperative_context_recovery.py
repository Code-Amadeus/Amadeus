"""Cold Host reconstruction preserves context identity without replaying execution."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_host.provider_contract import ProviderCapabilities, ProviderManifest, ProviderRequirements
from agent_host.provider_runtime import ProviderRuntime, ProviderStartAdmissionRejected
import agent_host.provider_runtime as provider_runtime_module
from agent_host.provider_types import (ProviderRunResult, ProviderSessionHandle, ProviderEvent,
    ProviderNativeExecutionHandle, ProviderRunIntakeAuthority, ProviderRunRequest,
    ProviderSubmissionReconciliationResult, ProviderInputDelivery)
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from core import session_manager as sm
import core.turn_coordinator as tc
from server.control_ledger import ControlLedgerConflict, ControlLedgerStore
from server.cooperative_chat_ingress import CooperativeChatIngress, CooperativeChatManager
from server.cooperative_provider_loop import CooperativeProviderLoop, LoopConflict
from server.cooperative_context_store import CooperativeContextStore
from server.handlers.chat_handler import ChatHandler
from server.handlers.work_ledger_handler import WorkLedgerHandler
from server.canvas_action_router import CanvasActionRouter
from server.protocol import Method
from server.event_bus import bus
from server.attention_request import AttentionRequestCoordinator
from server.turn_admission import admission_transcript_hash
from server.turn_admission import capture_turn_admission
from server.provider_event_ingestion import ProviderEventIngestor
from server.work_control import (
    CurrentTurnSourceSpanV1,
    WorkControl,
    WorkCooperativeContextPayloadV6,
)
from server.work_destination_service import WorkDestinationService
from server.work_effect_executor import WorkEffectExecutor


def _current_task_send(frame):
    """Fixture model names the known recipient in its original decision.

    Source/target agreement is checked separately by the reference fixture.
    """
    current = frame.get("context", {}).get("task")
    return {"op":"send", **({"target":current["token"]} if current else {})}


def _current_task_query(*, frames=None):
    """Fix the intended current-task meaning at both existing model boundaries."""
    target = ""
    async def query(messages):
        nonlocal target
        try:
            frame = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            assert target and target in messages[-1]["content"]
            assert messages[-1]["content"].startswith("[Current user message]\n")
            return json.dumps({"references":[target]})
        if frames is not None:
            frames.append(frame)
        if frame["source_kind"] != "user":
            return "確認したわ。"
        action = _current_task_send(frame)
        target = action.get("target", "")
        return json.dumps({"say":"好的。", "action":action}, ensure_ascii=False)
    return query
from server.work_ledger_coordinator import WorkLedgerCoordinator
from server.interaction_branch import (
    InteractionBranchCoordinator,
    InteractionBranchState,
)


class NativeFixture:
    provider_id = "recovery-test"
    manifest = ProviderManifest(provider_id=provider_id, display_name="Recovery fixture",
        capabilities=ProviderCapabilities(workspace_access="write", workspace_ownership="caller",
            resume="attach", cancellation="confirmed", submission_reconciliation="query"))

    def __init__(self):
        self.requests, self.handles = [], {}
        self.inspections = []
        self.observation = ProviderSubmissionReconciliationResult(state="unavailable")
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()

    async def run(self, request, run_id, emit):
        self.requests.append(request)
        handle = request.session or ProviderSessionHandle(
            provider=self.provider_id, session_id="native-" + run_id, scope="interaction")
        await emit(ProviderEvent(provider=self.provider_id, run_id=run_id,
            type="session.opened", session=handle))
        marker = Path(request.cwd)/"context.txt"
        if request.session is None:
            marker.write_text(handle.session_id, encoding="utf-8")
        else:
            assert marker.read_text(encoding="utf-8") == handle.session_id
        self.handles[run_id] = handle
        self.started.set()
        await self.release.wait()
        return ProviderRunResult(status="done", result="目录已检查。", session=handle)

    async def cancel(self, run_id):
        return {"confirmed":True, "cancelled":True, "session":self.handles.get(run_id)}

    async def reconcile_submission(self, request):
        self.inspections.append(request)
        return self.observation


class InteractiveNativeFixture(NativeFixture):
    manifest = replace(NativeFixture.manifest, capabilities=replace(
        NativeFixture.manifest.capabilities, interaction="bidirectional"))

    def __init__(self):
        super().__init__()
        self.permission_responses = []

    async def resolve_permission(self, run_id, response):
        self.permission_responses.append((run_id, response))
        return {"accepted":True}


class CooperativeWorkFixture(InteractiveNativeFixture):
    manifest = replace(InteractiveNativeFixture.manifest, capabilities=replace(
        InteractiveNativeFixture.manifest.capabilities, append_input=True))

    def __init__(self):
        super().__init__()
        self.inputs = []
        self.work_runs = 0

    async def run(self, request, run_id, emit):
        self.requests.append(request)
        handle = request.session or ProviderSessionHandle(
            provider=self.provider_id, session_id="native-" + run_id,
            scope="interaction")
        await emit(ProviderEvent(provider=self.provider_id, run_id=run_id,
            type="session.opened", session=handle))
        marker = Path(request.cwd)/"context.txt"
        if request.session is None:
            marker.write_text(handle.session_id, encoding="utf-8")
        else:
            assert marker.read_text(encoding="utf-8") == handle.session_id
        self.handles[run_id] = handle
        if request.metadata.get("source") == "control_work_effect":
            self.work_runs += 1
            amendment = "report-1.md" in request.task
            output = Path(request.cwd)/(
                "report-1.md" if amendment else f"report-{self.work_runs}.md")
            await emit(ProviderEvent(provider=self.provider_id, run_id=run_id,
                type="tool.call", payload={"tool":"Write",
                    "raw":{"input":{"file_path":str(output)}}}))
            output.write_text(("report 1 amended\n" if amendment
                else f"report {self.work_runs}\n"), encoding="utf-8")
            await emit(ProviderEvent(provider=self.provider_id, run_id=run_id,
                type="tool.result", payload={"tool":"Write", "ok":True}))
        self.started.set()
        await self.release.wait()
        return ProviderRunResult(status="done", result="报告已生成。", session=handle)

    async def append_input(self, run_id, text):
        self.inputs.append((run_id, text))
        return ProviderInputDelivery("delivered")


class CloseReleasedNativeFixture(NativeFixture):
    """A run can exit only after the shared Runtime closes its adapter."""

    def __init__(self):
        super().__init__()
        self.release.clear()
        self.closed = asyncio.Event()

    async def cancel(self, run_id):
        return {"confirmed":False, "cancelled":False,
            "reason":"adapter_connection_must_close"}

    async def close(self):
        self.closed.set()
        self.release.set()


@pytest.fixture
def host_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path/"sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    emit = AsyncMock()
    monkeypatch.setattr("server.handlers.chat_handler.bus.emit", emit)
    sm.create_session("conversation-A")

    def make(*, database=None, allow_allocate=True):
        monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
        ledger = ControlLedgerStore(database or tmp_path/"host.sqlite3")
        runtime, adapter = ProviderRuntime(), NativeFixture()
        runtime.register(adapter)
        queries = []
        async def query(messages):
            frame = json.loads(messages[-1]["content"])
            queries.append(frame)
            action = None
            if frame["source_kind"] == "user":
                action = {"op":"interrupt" if frame["current"]["text"] == "先停下。" else "send"}
            else:
                return "好的。"
            return json.dumps({"say":"好的。", "action":action}, ensure_ascii=False)
        def allocate(label, context_id):
            assert allow_allocate, "cold continuation must not allocate another workspace"
            path = tmp_path/context_id
            path.mkdir()
            return path
        loop = CooperativeProviderLoop(runtime, query, allocate,
            context_requirements={adapter.provider_id:ProviderRequirements(workspace_access="write",
                workspace_ownership="caller", resume="attach")},
            provider=adapter.provider_id, publish=lambda event: True)
        try:
            ingress = CooperativeChatIngress(loop, session_id="conversation-A", ledger=ledger, fence_scope="foreground")
        except BaseException:
            ledger.close()
            raise
        # Recovery exercises the durable Chat stop owner as well as the loop.
        manager = object.__new__(CooperativeChatManager)
        manager.ledger, manager.runtime = ledger, runtime
        manager.work_executor = None
        manager.attention = AttentionRequestCoordinator()
        async def resolve_reference(_messages):
            candidates, _, _, targets = manager.task_stop_candidates(ingress)
            return json.dumps({"references":[candidate.token for candidate in candidates
                if targets[candidate.token]["child_id"] == loop.bound_context_id]})
        manager.query = resolve_reference
        ingress.work_request = manager.handle_work_action
        async def send(text, key):
            response = await ingress.handler._handle_send({"text":text, "turn_id":key,
                "utterance_id":key, "session_id":"conversation-A"})
            if response["status"] == "replayed":
                return response
            await ingress.handler._stream_task
            return await loop._inputs[key][1]
        async def close():
            try:
                await ingress.close()
            finally:
                ledger.close()
        return SimpleNamespace(ledger=ledger, runtime=runtime, adapter=adapter,
            loop=loop, ingress=ingress, send=send, close=close, queries=queries)
    return make


@pytest.mark.parametrize("stop", [False, True])
async def test_confirmed_context_continues_after_host_reconstruction(host_factory, stop):
    first = host_factory()
    try:
        if stop:
            first.adapter.release.clear()
        started = await first.send("检查目录。", "first")
        await asyncio.wait_for(first.adapter.started.wait(), 2)
        if stop:
            assert (await first.send("先停下。", "stop"))["state"] == "stopped"
        await first.loop.wait()
        child = first.loop.children[started["child_id"]]
        assert child.run_status == ("cancelled" if stop else "done")
        assert child.native_session is not None
    finally:
        await first.close()
    resumed = host_factory(allow_allocate=False)
    try:
        assert resumed.loop.bound_context_id == child.child_id
        restored = resumed.loop.get_context(child.child_id)
        assert (restored.workspace, restored.native_session) == (child.workspace, child.native_session)
        assert (await resumed.send("现在检查原目录。", "continued"))["state"] == "started"
        await resumed.loop.wait()
        request, = resumed.adapter.requests
        assert request.cwd == child.workspace and request.session == child.native_session
        assert request.metadata["session_id"] == "conversation-A"
        assert request.metadata["source_utterance_id"] == "continued"
        assert resumed.queries[0]["context"]["last_run"]["status"] == child.run_status
        assert len(resumed.loop.children) == 1
    finally:
        await resumed.close()


def backup(ledger, target):
    with sqlite3.connect(target) as db:
        ledger._db.backup(db)


@pytest.mark.parametrize("crash_point", ["before_native_start", "while_running"])
async def test_unresolved_checkpoint_never_resends_after_reconstruction(host_factory, tmp_path, monkeypatch, crash_point):
    first = host_factory()
    crash_database = tmp_path/"crash.sqlite3"
    try:
        if crash_point == "before_native_start":
            async def stop_at_dispatch(request, intake_authority):
                backup(first.ledger, crash_database)
                raise RuntimeError("simulated process loss at native dispatch boundary")
            monkeypatch.setattr(first.runtime, "start_accepted", stop_at_dispatch)
            with pytest.raises(RuntimeError, match="simulated process loss"):
                await first.send("检查目录。", "first")
        else:
            first.adapter.release.clear()
            await first.send("检查目录。", "first")
            await asyncio.wait_for(first.adapter.started.wait(), 2)
            backup(first.ledger, crash_database)
        original_id = first.loop.bound_context_id
    finally:
        await first.close()
    # The backup contains exactly the durable state at the crash boundary;
    # orderly cleanup of the original process cannot retroactively confirm it.
    resumed = host_factory(database=crash_database, allow_allocate=False)
    try:
        replay = await resumed.send("检查目录。", "first")
        assert replay["status"] == "replayed" and not resumed.queries
        for text, key in (("现在检查原目录。", "continued"), ("先停下。", "stop")):
            receipt = await resumed.send(text, key)
            assert receipt["state"] == "unknown"
            if key == "continued":
                assert receipt["reason"] == "prior_execution_unknown"
        assert resumed.loop.bound_context_id == original_id
        assert resumed.queries[0]["context"]["last_run"]["status"] == "orphaned"
        assert not resumed.adapter.requests and not resumed.runtime.list_runs()
        assert not resumed.loop.children[original_id].closed
    finally:
        await resumed.close()


async def test_missing_original_workspace_is_not_recreated(host_factory, tmp_path):
    first = host_factory()
    try:
        await first.send("检查目录。", "first")
        await first.loop.wait()
        child = first.loop.children[first.loop.bound_context_id]
    finally:
        await first.close()
    path = Path(child.workspace)
    assert path.resolve().is_relative_to(tmp_path.resolve())
    (path/"context.txt").unlink()
    path.rmdir()
    resumed = host_factory(allow_allocate=False)
    try:
        receipt = await resumed.send("现在检查原目录。", "continued")
        assert receipt["state"] == "rejected" and receipt["reason"] == "workspace_unavailable"
        assert not path.exists() and not resumed.adapter.requests
        assert resumed.loop.children[child.child_id].native_session == child.native_session
    finally:
        await resumed.close()


async def test_failed_checkpoint_prevents_native_dispatch(host_factory, monkeypatch):
    host = host_factory()
    monkeypatch.setattr(host.loop._state, "claim_provider_effect",
        lambda *args, **kwargs:(_ for _ in ()).throw(OSError("checkpoint unavailable")))
    try:
        with pytest.raises(OSError, match="checkpoint unavailable"):
            await host.send("检查目录。", "first")
        assert not host.adapter.requests and not host.runtime.list_runs()
        _, rows = host.loop._state.load()
        assert rows[0]["run_status"] == "idle"
    finally:
        await host.close()


async def test_durable_same_address_rebind_retires_old_decision(host_factory):
    host = host_factory()
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        await host.send("检查目录。", "first")
        await host.loop.wait()
        query = host.loop.query
        async def paused(messages):
            entered.set()
            await release.wait()
            return await query(messages)
        host.loop.query = paused
        pending = asyncio.create_task(host.send("现在检查原目录。", "continued"))
        await entered.wait()
        binding = host.loop._binding
        host.loop._state.bind(binding.child_id, expected_token=binding.token,
            expected_context_id=binding.child_id)
        release.set()
        with pytest.raises(ControlLedgerConflict, match="binding changed"):
            await pending
        assert len(host.adapter.requests) == 1
    finally:
        release.set()
        await host.close()


async def test_known_start_refusal_preserves_confirmed_context(host_factory):
    first = host_factory()
    try:
        await first.send("检查目录。", "first")
        await first.loop.wait()
        original = first.loop.children[first.loop.bound_context_id]
    finally:
        await first.close()
    resumed = host_factory(allow_allocate=False)
    try:
        resumed.runtime.set_start_admission_validator(
            lambda request, run_id, phase:{"accepted":phase != "reserve", "reason":"scope_denied"})
        receipt = await resumed.send("现在检查原目录。", "denied")
        assert receipt["state"] == "rejected" and receipt["reason"] == "scope_denied"
        restored = resumed.loop.children[original.child_id]
        assert (restored.run_id, restored.run_status) == (original.run_id, "done")
        assert not resumed.adapter.requests
        resumed.runtime.set_start_admission_validator(None)
        assert (await resumed.send("现在检查原目录。", "authorized"))["state"] == "started"
        await resumed.loop.wait()
        assert resumed.adapter.requests[0].session == original.native_session
    finally:
        await resumed.close()


def test_old_source_only_database_cannot_invent_a_replacement_binding(host_factory, tmp_path):
    ledger = ControlLedgerStore(tmp_path/"host.sqlite3")
    try:
        ledger.open_admission(root_id="legacy-root", source_scope="chat:conversation-A",
            fence_scope="foreground", utterance_id="legacy-input", authority_mode="legacy",
            transcript_hash=admission_transcript_hash("检查目录。"))
    finally:
        ledger.close()
    with pytest.raises(ControlLedgerConflict, match="without a durable cooperative binding"):
        host_factory(allow_allocate=False)


def test_legacy_context_schema_migrates_without_reauthorizing_or_reidentifying(tmp_path):
    database = tmp_path/"legacy-host.sqlite3"
    ledger = ControlLedgerStore(database)
    handle = ProviderSessionHandle(provider="recovery-test",
        session_id="legacy-native-thread", scope="interaction")
    source_text = "检查旧目录。"
    ledger.open_admission(root_id="legacy-root", source_scope="chat:conversation-A",
        fence_scope="foreground", utterance_id="legacy-input", authority_mode="legacy",
        transcript_hash=admission_transcript_hash(source_text))
    with ledger._transaction() as db:
        db.execute("""CREATE TABLE cooperative_contexts (
            session_id TEXT NOT NULL, context_id TEXT NOT NULL,
            label TEXT NOT NULL, provider TEXT NOT NULL, workspace TEXT NOT NULL,
            closed INTEGER NOT NULL, run_id TEXT NOT NULL, run_status TEXT NOT NULL,
            native_session TEXT NOT NULL, output TEXT NOT NULL,
            last_input_id TEXT NOT NULL, revision INTEGER NOT NULL,
            PRIMARY KEY(session_id,context_id))""")
        db.execute("""CREATE TABLE cooperative_bindings (
            session_id TEXT PRIMARY KEY, context_id TEXT, token TEXT NOT NULL)""")
        db.execute("INSERT INTO cooperative_contexts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("conversation-A", "legacy-context", "Legacy coding context", "recovery-test",
             str(tmp_path), 0, "legacy-run", "done",
             json.dumps(handle.to_dict(), sort_keys=True), "legacy output", "legacy-input", 7))
        db.execute("INSERT INTO cooperative_bindings VALUES (?,?,?)",
            ("conversation-A", "legacy-context", "legacy-binding-token"))

    try:
        store = CooperativeContextStore(ledger, "conversation-A")
        binding, rows = store.load()
        assert binding == {"session_id":"conversation-A", "context_id":"legacy-context",
            "token":"legacy-binding-token"}
        row, = rows
        assert row["native_session"] == handle
        assert (row["run_id"], row["run_status"], row["last_input_id"], row["revision"]) == (
            "legacy-run", "done", "legacy-input", 7)
        assert row["last_turn_id"] == ""
        assert row["work_item_id"] == ""
        assert row["workspace_route"] == {}
        assert row["requirements"] == ProviderRequirements(task_kind="general",
            workspace_access="write", workspace_ownership="caller", ownership="managed",
            resume="attach")
        with ledger._transaction() as db:
            columns = {item["name"] for item in db.execute("PRAGMA table_info(cooperative_contexts)")}
            source = db.execute("""SELECT root_id,source_scope,utterance_id,authority_mode,
                transcript_hash,lifecycle FROM control_admissions WHERE source_scope=? AND utterance_id=?""",
                ("chat:conversation-A", "legacy-input")).fetchone()
        assert {"requirements", "last_turn_id", "work_item_id",
            "workspace_route"}.issubset(columns)
        assert dict(source) == {"root_id":"legacy-root", "source_scope":"chat:conversation-A",
            "utterance_id":"legacy-input", "authority_mode":"legacy",
            "transcript_hash":admission_transcript_hash(source_text), "lifecycle":"current"}
    finally:
        ledger.close()


async def test_checkpoint_cannot_replace_an_established_native_identity(host_factory):
    host = host_factory()
    try:
        await host.send("检查目录。", "first")
        await host.loop.wait()
        child = host.loop.children[host.loop.bound_context_id]
        altered = replace(child, native_session=ProviderSessionHandle(
            provider=child.provider, session_id="different-native", scope="interaction"))
        with pytest.raises(ControlLedgerConflict, match="checkpoint changed"):
            host.loop._state.checkpoint(altered)
        _, rows = host.loop._state.load()
        assert rows[0]["native_session"] == child.native_session
        assert rows[0]["revision"] == child.revision
    finally:
        await host.close()


async def test_host_close_of_context_survives_reconstruction(host_factory):
    first = host_factory()
    try:
        await first.send("检查目录。", "first")
        await first.loop.wait()
        child_id = first.loop.bound_context_id
        assert (await first.loop._apply({"op":"close", "recipient":child_id}, ""))["state"] == "closed"
    finally:
        await first.close()
    resumed = host_factory(allow_allocate=False)
    try:
        assert resumed.loop.get_context(child_id).closed
        with pytest.raises(LoopConflict, match="closed"):
            await resumed.send("现在检查原目录。", "continued")
        assert not resumed.adapter.requests and len(resumed.loop.children) == 1
    finally:
        await resumed.close()


async def test_retained_session_dialogue_is_available_to_first_handoff(host_factory):
    assert sm.append_session_message("conversation-A", role="user", content="我正在整理购物清单。", turn_id="discussion")
    assert sm.append_session_message("conversation-A", role="assistant", content="可以接着检查目录。", turn_id="discussion")
    host = host_factory()
    try:
        await host.send("检查目录。", "first")
        await host.loop.wait()
        assert [(row["source"], row["text"]) for row in host.queries[0]["history"]] == [
            ("user", "我正在整理购物清单。"), ("kurisu", "可以接着检查目录。")]
        assert "我正在整理购物清单。" in host.adapter.requests[0].metadata["source_user_context"]
        assert "可以接着检查目录。" in host.adapter.requests[0].metadata["source_user_context"]
    finally:
        await host.close()


async def test_runtime_identity_is_durable_before_adapter_scheduling(host_factory):
    host = host_factory()
    observed = []
    def check_created(request, run_id, phase):
        if phase == "created":
            _, rows = host.loop._state.load()
            assert rows[0]["run_id"] == run_id and rows[0]["run_status"] == "queued"
            assert rows[0]["last_input_id"] == request.metadata["source_utterance_id"] == "first"
            assert rows[0]["last_turn_id"] == request.metadata["turn_id"] == "first"
            assert not host.adapter.requests
            observed.append(run_id)
        return {"accepted":True}
    host.runtime.set_start_admission_validator(check_created)
    try:
        receipt = await host.send("检查目录。", "first")
        await host.loop.wait()
        assert observed == [receipt["run_id"]]
    finally:
        await host.close()


async def test_next_turn_settles_old_start_before_its_delayed_monitor(
        host_factory, monkeypatch):
    host = host_factory()
    monitor_entered, release_monitor = asyncio.Event(), asyncio.Event()
    original_observe = host.loop._observe
    delayed_run_ids = []

    async def delayed_observe(child, run_id, **kwargs):
        if not delayed_run_ids:
            delayed_run_ids.append(run_id)
            monitor_entered.set()
            await release_monitor.wait()
        return await original_observe(child, run_id, **kwargs)

    monkeypatch.setattr(host.loop, "_observe", delayed_observe)
    try:
        host.adapter.release.clear()
        first = await host.send("第一轮。", "first")
        first_run_id = first["run_id"]
        assert delayed_run_ids == [first_run_id]
        await asyncio.wait_for(host.adapter.started.wait(), 2)
        host.adapter.release.set()
        await host.runtime.get_run(first_run_id).task_handle
        await asyncio.wait_for(monitor_entered.wait(), 2)

        second = await host.send("第二轮。", "second")
        assert second["state"] == "started" and second["run_id"] != first_run_id
        with host.ledger._lock:
            first_effect = next(dict(row) for row in host.ledger._db.execute(
                "SELECT * FROM control_effect_outbox")
                if json.loads(row["payload_json"])["source_utterance_id"] == "first")
        assert first_effect["state"] == "terminal"
        assert host.ledger.get_receipt(first_effect["effect_id"])["external_id"] == (
            first_run_id)
        release_monitor.set()
        await host.loop.wait()
        with host.ledger._lock:
            effects = [dict(row) for row in host.ledger._db.execute(
                "SELECT * FROM control_effect_outbox")]
        assert len(effects) == 2 and all(row["state"] == "terminal" for row in effects)
    finally:
        release_monitor.set()
        host.adapter.release.set()
        await host.close()


async def restored_pending(host_factory, tmp_path):
    first = host_factory()
    try:
        await first.send("检查目录。", "first")
        await first.loop.wait()
        first.adapter.started.clear()
        first.adapter.release.clear()
        pending = await first.send("现在检查原目录。", "pending")
        await first.adapter.started.wait()
        backup(first.ledger, tmp_path/"pending.sqlite3")
    finally:
        await first.close()
    return host_factory(database=tmp_path/"pending.sqlite3", allow_allocate=False), pending["run_id"]


async def test_exact_terminal_read_unblocks_original_context_without_resending(host_factory, tmp_path):
    host, run_id = await restored_pending(host_factory, tmp_path)
    child = host.loop.children[host.loop.bound_context_id]
    host.adapter.observation = ProviderSubmissionReconciliationResult(state="matched_terminal",
        execution=ProviderNativeExecutionHandle(provider=child.provider, execution_id="observed-turn"),
        terminal_result=ProviderRunResult(status="done", result="已核验原执行结束。", session=child.native_session))
    try:
        history = list(host.loop.history)
        receipt = (await host.ingress.recover())[child.child_id]
        assert receipt["state"] == "matched_terminal" and receipt["promoted"] is True
        inspection, = host.adapter.inspections
        assert inspection.run_id == run_id and inspection.session == child.native_session
        assert not host.adapter.requests and not host.runtime.list_runs() and not host.queries
        assert host.loop.history == history
        _, rows = host.loop._state.load()
        assert rows[0]["run_id"] == run_id and rows[0]["run_status"] == "done"
        effect = host.ledger.get_effect(rows[0]["run_effect_id"])
        assert effect["state"] == "terminal" and effect["external_id"] == run_id
        assert host.ledger.get_receipt(effect["effect_id"])["details"] == {
            "status":"done"}
        assert (await host.send("现在检查原目录。", "continued"))["state"] == "started"
        await host.loop.wait()
        assert host.adapter.requests[0].session == inspection.session
        assert host.adapter.requests[0].cwd == child.workspace
    finally:
        await host.close()


async def test_orphaned_result_keeps_start_receipt_open_until_exact_terminal_recovery(
        host_factory, monkeypatch, tmp_path):
    first = host_factory()
    orphaned_database = tmp_path/"orphaned-result.sqlite3"

    async def orphaned_run(request, run_id, emit):
        handle = request.session or ProviderSessionHandle(
            provider=first.adapter.provider_id,
            session_id="native-" + run_id, scope="interaction")
        await emit(ProviderEvent(provider=first.adapter.provider_id,
            run_id=run_id, type="session.opened", session=handle))
        return ProviderRunResult(status="orphaned",
            error="submission outcome is unknown", session=handle)

    monkeypatch.setattr(first.adapter, "run", orphaned_run)
    try:
        started = await first.send("检查目录。", "orphaned-source")
        await first.loop.wait()
        child = first.loop.children[started["child_id"]]
        assert child.run_status == "orphaned"
        effect = first.ledger.get_effect(child.run_effect_id)
        assert effect["state"] == "running" and effect["external_id"] == started["run_id"]
        assert first.ledger.get_receipt(effect["effect_id"]) is None
        child_id, run_id, session = child.child_id, child.run_id, child.native_session
        backup(first.ledger, orphaned_database)
    finally:
        await first.close()

    second = host_factory(database=orphaned_database, allow_allocate=False)
    second.adapter.observation = ProviderSubmissionReconciliationResult(
        state="matched_terminal",
        execution=ProviderNativeExecutionHandle(provider=second.adapter.provider_id,
            execution_id="native-terminal"),
        terminal_result=ProviderRunResult(status="done",
            result="exact terminal result", session=session))
    try:
        recovery = (await second.ingress.recover())[child_id]
        assert recovery["state"] == "matched_terminal" and recovery["promoted"] is True
        restored = second.loop.children[child_id]
        assert restored.run_id == run_id and restored.run_status == "done"
        effect = second.ledger.get_effect(restored.run_effect_id)
        assert effect["state"] == "terminal"
        assert second.ledger.get_receipt(effect["effect_id"])["outcome"] == "succeeded"
    finally:
        await second.close()


@pytest.mark.parametrize("state", ["matched_active", "not_observed", "ambiguous", "unavailable"])
async def test_nonterminal_observation_never_reopens_execution(host_factory, tmp_path, state):
    host, _ = await restored_pending(host_factory, tmp_path)
    child = host.loop.children[host.loop.bound_context_id]
    host.adapter.observation = ProviderSubmissionReconciliationResult(state=state,
        execution=ProviderNativeExecutionHandle(provider=child.provider, execution_id="active-turn")
            if state == "matched_active" else None)
    try:
        before = host.loop._state.load()
        receipt = await host.loop.reconcile_restored_context(child.child_id)
        assert receipt["state"] == state and receipt["promoted"] is False
        assert host.loop._state.load() == before
        assert (await host.send("现在检查原目录。", "continued"))["state"] == "unknown"
        assert not host.adapter.requests and len(host.adapter.inspections) == 1
    finally:
        await host.close()


@pytest.mark.parametrize("recovery_state", ["matched_terminal", "unavailable"])
async def test_recovery_expires_permission_without_a_runtime_callback_owner(
        host_factory, tmp_path, recovery_state):
    host, run_id = await restored_pending(host_factory, tmp_path)
    child = host.loop.children[host.loop.bound_context_id]
    permissions = WorkLedgerStore(host.ledger.path)
    request = permissions.create_cooperative_permission_request(
        session_id="conversation-A", context_id=child.child_id,
        provider_run_id=run_id, capability="shell.execute",
        action="execute_command", options=["deny"],
        idempotency_key="provider:recovery-test:" + run_id + ":permission",
        metadata={"provider":"recovery-test",
            "provider_request_id":"native-permission"})
    if recovery_state == "matched_terminal":
        host.adapter.observation = ProviderSubmissionReconciliationResult(
            state="matched_terminal",
            execution=ProviderNativeExecutionHandle(provider=child.provider,
                execution_id="native-terminal"),
            terminal_result=ProviderRunResult(status="cancelled",
                session=child.native_session))
    try:
        recovery = await host.ingress.recover()
        expired = host.ingress.expire_unactionable_permissions(
            permissions, recovery=recovery)
        assert expired == [{"session_id":"conversation-A",
            "context_id":child.child_id, "run_id":run_id,
            "permission_request_id":request.request_id, "state":"expired",
            "reason":"runtime_owner_unavailable_after_recovery"}]
        stored = permissions.get_permission_request(request.request_id)
        assert stored is not None and stored.status == "expired"
        assert stored.metadata["recovery_state"] == recovery_state
        assert not stored.work_item_id and not stored.attempt_id
        assert permissions.list_work_items() == [] and permissions.list_projects() == []
        assert host.ingress.expire_unactionable_permissions(
            permissions, recovery=recovery) == []
    finally:
        permissions.close()
        await host.close()


async def test_recovery_permission_cleanup_includes_an_older_context_run(
        host_factory, tmp_path):
    first = host_factory()
    permissions = WorkLedgerStore(first.ledger.path)
    recovered_database = tmp_path/"older-permission.sqlite3"
    try:
        first.adapter.release.clear()
        old = await first.send("第一轮。", "old-run")
        await asyncio.wait_for(first.adapter.started.wait(), 2)
        child = first.loop.children[old["child_id"]]
        pending = permissions.create_cooperative_permission_request(
            session_id="conversation-A", context_id=child.child_id,
            provider_run_id=old["run_id"], capability="shell.execute",
            action="execute_command", options=["deny"],
            idempotency_key="provider:recovery-test:" + old["run_id"] + ":old",
            metadata={"provider":"recovery-test",
                "provider_request_id":"old-native-permission"})
        first.adapter.release.set()
        await first.loop.wait()

        first.adapter.started.clear()
        first.adapter.release.clear()
        current = await first.send("第二轮。", "current-run")
        await asyncio.wait_for(first.adapter.started.wait(), 2)
        assert current["run_id"] != old["run_id"]
        backup(first.ledger, recovered_database)
    finally:
        first.adapter.release.set()
        permissions.close()
        await first.close()

    second = host_factory(database=recovered_database, allow_allocate=False)
    reopened_permissions = WorkLedgerStore(recovered_database)
    try:
        recovery = await second.ingress.recover()
        expired = second.ingress.expire_unactionable_permissions(
            reopened_permissions, recovery=recovery)
        assert expired == [{"session_id":"conversation-A",
            "context_id":second.loop.bound_context_id, "run_id":old["run_id"],
            "permission_request_id":pending.request_id, "state":"expired",
            "reason":"runtime_owner_unavailable_after_recovery"}]
        stored = reopened_permissions.get_permission_request(pending.request_id)
        assert stored is not None and stored.status == "expired"
        assert stored.metadata["provider_run_status"] == "superseded"
        assert stored.metadata["recovery_state"] == "superseded_run"
    finally:
        reopened_permissions.close()
        await second.close()


@pytest.mark.parametrize("changed", ["native_identity", "checkpoint"])
async def test_reconciliation_cannot_overwrite_another_identity_or_newer_state(host_factory, tmp_path, changed):
    host, _ = await restored_pending(host_factory, tmp_path)
    child = host.loop.children[host.loop.bound_context_id]
    prior_status = child.run_status
    async def inspect(request):
        handle = child.native_session
        if changed == "native_identity":
            handle = replace(handle, session_id="other-native")
        else:
            # Another Host writer advances the same durable context while this
            # caller still has its older snapshot; the late read cannot win.
            host.loop._state.checkpoint(replace(child, output="newer observation"))
        return ProviderSubmissionReconciliationResult(state="matched_terminal",
            execution=ProviderNativeExecutionHandle(provider=child.provider, execution_id="observed-turn"),
            terminal_result=ProviderRunResult(status="done", result="late result", session=handle))
    host.adapter.reconcile_submission = inspect
    try:
        receipt = await host.loop.reconcile_restored_context(child.child_id)
        assert receipt["state"] == ("unavailable" if changed == "native_identity" else "stale")
        assert receipt["promoted"] is False and not host.adapter.requests
        _, rows = host.loop._state.load()
        assert rows[0]["run_status"] == prior_status and rows[0]["native_session"] == child.native_session
    finally:
        await host.close()


async def test_reconciliation_timeout_preserves_uncertainty(host_factory, tmp_path):
    host, _ = await restored_pending(host_factory, tmp_path)
    child = host.loop.children[host.loop.bound_context_id]
    async def inspect(request):
        await asyncio.Event().wait()
    host.adapter.reconcile_submission = inspect
    try:
        before = host.loop._state.load()
        receipt = await host.loop.reconcile_restored_context(child.child_id, timeout_seconds=0.01)
        assert receipt["state"] == "unavailable" and receipt["promoted"] is False
        assert host.loop._state.load() == before and not host.adapter.requests
    finally:
        await host.close()


async def test_runtime_cannot_replay_an_already_consumed_dispatch_slot(host_factory):
    host = host_factory()
    try:
        await host.send("检查目录。", "first")
        await host.loop.wait()
        before = host.loop._state.load()
        child = host.loop.children[host.loop.bound_context_id]
        request = replace(host.adapter.requests[0], session=child.native_session)
        with pytest.raises(ProviderStartAdmissionRejected, match="accepted Provider effect"):
            await host.runtime.start(request)
        assert host.loop._state.load() == before and len(host.adapter.requests) == 1
    finally:
        await host.close()


async def test_context_assembly_cannot_replace_an_existing_intake_owner(tmp_path):
    runtime = ProviderRuntime()
    original = lambda request, run_id: request
    runtime.set_request_preparer(original)
    ledger = ControlLedgerStore(tmp_path/"host.sqlite3")
    loop = CooperativeProviderLoop(runtime, AsyncMock(), lambda *_:tmp_path, provider="unused", context_requirements={})
    try:
        with pytest.raises(LoopConflict, match="another intake owner"):
            loop.attach_state(CooperativeContextStore(ledger, "session"))
        assert runtime._request_preparer is original and loop._state is None
    finally:
        await loop.close()
        ledger.close()


async def test_shutdown_drains_owned_query_without_promoting_after_close(host_factory, tmp_path):
    host, _ = await restored_pending(host_factory, tmp_path)
    child = host.loop.children[host.loop.bound_context_id]
    entered, release = asyncio.Event(), asyncio.Event()
    async def inspect(request):
        entered.set()
        await release.wait()
        return ProviderSubmissionReconciliationResult(state="matched_terminal",
            execution=ProviderNativeExecutionHandle(provider=child.provider, execution_id="observed-turn"),
            terminal_result=ProviderRunResult(status="done", session=child.native_session))
    host.adapter.reconcile_submission = inspect
    try:
        before = host.loop._state.load()
        caller = asyncio.create_task(host.loop.reconcile_restored_context(child.child_id))
        await entered.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        closing = asyncio.create_task(host.loop.close())
        await asyncio.sleep(0)
        assert host.loop._closed and not closing.done()
        release.set()
        await closing
        assert host.loop._state.load() == before
        assert not host.loop._monitors and not host.adapter.requests
    finally:
        release.set()
        await host.close()


async def test_shared_runtime_owner_closes_adapter_before_loop_observation_drain(tmp_path):
    runtime, adapter = ProviderRuntime(), CloseReleasedNativeFixture()
    runtime.register(adapter)
    async def query(messages):
        frame = json.loads(messages[-1]["content"])
        return (json.dumps({"say":"始めるわ。", "action":{"op":"send"}})
            if frame["source_kind"] == "user" else "始めるわ。")
    def allocate(_label, context_id):
        path = tmp_path/context_id
        path.mkdir()
        return path
    loop = CooperativeProviderLoop(runtime, query, allocate,
        context_requirements={adapter.provider_id:ProviderRequirements(workspace_access="write",
            workspace_ownership="caller", resume="attach")},
        provider=adapter.provider_id, publish=lambda _event:True, owns_runtime=False)
    first = await loop.submit("开始。", input_id="source-1", turn_id="turn-1")
    await asyncio.wait_for(adapter.started.wait(), 1)

    await asyncio.wait_for(loop.begin_close(), 1)
    stop = next(row for row in loop.trace if row["kind"] == "shutdown_stop")
    assert stop["state"] == "unknown" and stop["run_id"] == first["run_id"]
    assert not loop.children[first["child_id"]].closed
    assert any(not task.done() for task in loop._monitors)

    await asyncio.wait_for(runtime.close(), 1)
    assert adapter.closed.is_set()
    await asyncio.wait_for(loop.finish_close(), 1)
    assert not loop._monitors and loop._close_finished


async def test_host_can_associate_existing_same_workspace_work_without_creating_attempt(host_factory, tmp_path):
    host = host_factory()
    work = None
    try:
        first = await host.send("检查目录。", "source-1")
        await host.loop.wait()
        child = host.loop.children[first["child_id"]]
        work = WorkLedgerStore(host.ledger.path)
        project = work.create_or_get_project(Path(child.workspace))
        item = work.create_work_item(project.project_id, title="Existing delivery",
            goal="Associate the existing receiving context.")
        next_item = work.create_work_item(project.project_id, title="Later delivery",
            goal="Use the same long-lived receiving context for another task.")
        other_root = tmp_path/"unrelated-workspace"
        other_root.mkdir()
        other_project = work.create_or_get_project(other_root)
        other = work.create_work_item(other_project.project_id, title="Unrelated delivery")

        with pytest.raises(ControlLedgerConflict, match="different workspaces"):
            host.loop.bind_work_item(other.work_item_id)
        original_token = host.loop._binding.token
        host.loop.bind_work_item(item.work_item_id)
        first_token = host.loop._binding.token
        host.loop.bind_work_item(next_item.work_item_id)
        assert original_token != first_token != host.loop._binding.token
        assert child.work_item_id == next_item.work_item_id
        assert host.loop.snapshot()[0]["work_item_id"] == next_item.work_item_id

        second = await host.send("继续检查。", "source-2")
        await host.loop.wait()
        request = host.adapter.requests[-1]
        assert second["state"] == "started"
        assert request.metadata["cooperative_work_item_id"] == next_item.work_item_id
        assert "work" not in request.metadata
        assert work.list_attempts(item.work_item_id) == []
        assert work.list_attempts(next_item.work_item_id) == []
        _, stored = host.loop._state.load()
        assert stored[0]["work_item_id"] == next_item.work_item_id
    finally:
        if work is not None:
            work.close()
        await host.close()


async def test_work_rebinding_retires_an_unaccepted_old_decision(host_factory):
    host = host_factory()
    work = None
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        await host.send("检查目录。", "source-1")
        await host.loop.wait()
        child = host.loop.children[host.loop.bound_context_id]
        work = WorkLedgerStore(host.ledger.path)
        project = work.create_or_get_project(Path(child.workspace))
        first = work.create_work_item(project.project_id, title="First task")
        second = work.create_work_item(project.project_id, title="Second task")
        host.loop.bind_work_item(first.work_item_id)
        original_query = host.loop.query

        async def paused(messages):
            entered.set()
            await release.wait()
            return await original_query(messages)

        host.loop.query = paused
        pending = asyncio.create_task(host.send("继续检查。", "source-2"))
        await entered.wait()
        host.loop.bind_work_item(second.work_item_id)
        release.set()
        with pytest.raises(LoopConflict, match="context binding changed"):
            await pending
        assert child.work_item_id == second.work_item_id
        assert len(host.adapter.requests) == 1
        assert work.list_attempts(first.work_item_id) == []
        assert work.list_attempts(second.work_item_id) == []
    finally:
        release.set()
        if work is not None:
            work.close()
        await host.close()


async def test_attention_scope_choice_switches_workspace_a_b_a_without_work(
        host_factory):
    host = host_factory()
    attention = AttentionRequestCoordinator()
    manager_view = SimpleNamespace(attention=attention)
    original_query = host.loop.query

    async def query(messages):
        frame = json.loads(messages[-1]["content"])
        if (frame["source_kind"] == "user"
                and frame["current"]["text"] in {"换工作区。", "切回之前。"}):
            return json.dumps({"say":"请选择。", "action":{"op":"scope_change"}},
                ensure_ascii=False)
        return await original_query(messages)

    host.loop.query = query
    host.ingress.scope_request = lambda ingress, turn_id, receipt: (
        CooperativeChatManager.request_scope_change(
            manager_view, ingress, turn_id, receipt
        )
    )
    try:
        first = await host.send("检查 A。", "a-first")
        await host.loop.wait()
        original = host.loop.children[first["child_id"]]
        original_session = original.native_session
        scope = await host.send("换工作区。", "scope-new")
        assert scope["state"] == "scope_change_required"
        request, = attention.list_pending("conversation-A")
        assert scope["attention_request_id"] == request["id"]
        new_option = next(option for option in request["options"]
            if option.get("metadata", {}).get("relation") == "new")
        created = await attention.resolve(session_id="conversation-A",
            request_id=request["id"], option_id=new_option["id"])
        assert created["ok"] is True
        assert host.queries[-1]["source_kind"] == "host_receipt"
        assert host.queries[-1]["current"]["state"] == "scope_bound"
        assert "id" not in host.queries[-1]["current"]["context"]
        assert "label" not in host.queries[-1]["current"]["context"]
        assert "last_run" not in host.queries[-1]["current"]["context"]
        assert any(row.get("source") == "host_receipt"
            and row.get("state") == "scope_bound" for row in host.loop.history)
        replacement_id = host.loop.bound_context_id
        replacement = host.loop.children[replacement_id]
        assert replacement_id != original.child_id
        assert replacement.workspace != original.workspace
        assert original.native_session == original_session

        second = await host.send("检查 B。", "b-first")
        await host.loop.wait()
        assert second["child_id"] == replacement_id
        assert host.adapter.requests[-1].cwd == replacement.workspace

        scope = await host.send("切回之前。", "scope-return")
        assert scope["state"] == "scope_change_required"
        request, = attention.list_pending("conversation-A")
        old_option = next(option for option in request["options"]
            if option.get("metadata", {}).get("relation") == "existing"
            and Path(original.workspace).name in option["label"])
        returned = await attention.resolve(session_id="conversation-A",
            request_id=request["id"], option_id=old_option["id"])
        assert returned["ok"] is True
        assert host.loop.bound_context_id == original.child_id
        third = await host.send("回到 A。", "a-return")
        await host.loop.wait()
        assert third["child_id"] == original.child_id
        assert host.adapter.requests[-1].cwd == original.workspace
        assert host.adapter.requests[-1].session == original_session
        assert len(host.loop.children) == 2
    finally:
        attention.reset_for_tests()
        await host.close()


async def test_attention_scope_choice_switches_provider_a_b_a_without_work(
        host_factory):
    host = host_factory()
    attention = AttentionRequestCoordinator()
    manager_view = SimpleNamespace(attention=attention)
    other = InteractiveNativeFixture()
    other.provider_id = "other-provider"
    other.manifest = replace(other.manifest, provider_id=other.provider_id,
        display_name="Other provider")
    host.runtime.register(other)
    host.loop.context_requirements[other.provider_id] = ProviderRequirements(
        workspace_access="write", workspace_ownership="caller", resume="attach")
    try:
        first = await host.send("检查 A。", "provider-a")
        await host.loop.wait()
        original = host.loop.children[first["child_id"]]
        original_session = original.native_session

        request = await CooperativeChatManager.request_scope_change(
            manager_view, host.ingress, "provider-switch", {"target":"Other provider"}
        )
        assert request["prompt"].startswith("Requested target: Other provider.")
        other_option = next(option for option in request["options"]
            if option["label"] == "New other-provider context")
        switched = await attention.resolve(session_id="conversation-A",
            request_id=request["id"], option_id=other_option["id"])
        assert switched["ok"] is True
        assert host.queries[-1]["source_kind"] == "host_receipt"
        assert host.queries[-1]["current"]["state"] == "scope_bound"
        assert host.queries[-1]["current"]["context"]["provider"] == other.provider_id
        assert "id" not in host.queries[-1]["current"]["context"]
        other_context = host.loop.children[host.loop.bound_context_id]
        assert other_context.provider == other.provider_id
        second = await host.send("检查 B。", "provider-b")
        await host.loop.wait()
        assert second["child_id"] == other_context.child_id
        assert len(other.requests) == 1
        assert other.requests[0].provider == other.provider_id
        assert "work" not in other.requests[0].metadata

        request = await CooperativeChatManager.request_scope_change(
            manager_view, host.ingress, "provider-return", {}
        )
        old_option = next(option for option in request["options"]
            if option.get("metadata", {}).get("relation") == "existing"
            and Path(original.workspace).name in option["label"])
        returned = await attention.resolve(session_id="conversation-A",
            request_id=request["id"], option_id=old_option["id"])
        assert returned["ok"] is True
        third = await host.send("回到 A。", "provider-a-return")
        await host.loop.wait()
        assert third["child_id"] == original.child_id
        assert host.adapter.requests[-1].session == original_session
        assert {child.provider for child in host.loop.children.values()} == {
            host.adapter.provider_id, other.provider_id}
    finally:
        attention.reset_for_tests()
        await host.close()


async def test_additional_provider_run_preserves_default_binding_and_reports_retained_origin(
        host_factory):
    host = host_factory()
    other = InteractiveNativeFixture()
    other.provider_id = "other-provider"
    other.manifest = replace(other.manifest, provider_id=other.provider_id,
        display_name="Other provider")
    host.runtime.register(other)
    host.loop.context_requirements[other.provider_id] = ProviderRequirements(
        workspace_access="write", workspace_ownership="caller", resume="attach")
    original_query = host.loop.query

    async def query(messages):
        frame = json.loads(messages[-1]["content"])
        if (frame["source_kind"] == "user"
                and frame["current"]["text"] == "另外让 Other Provider 检查。"):
            return json.dumps({"say":"另外交给它。",
                "action":{"op":"delegate", "provider":"other-provider"}},
                ensure_ascii=False)
        if (frame["source_kind"] == "user"
                and frame["current"]["text"] == "回复 Other Provider。"):
            return json.dumps({"say":"把回复交给它。",
                "action":{"op":"send_to", "provider":"other-provider"}},
                ensure_ascii=False)
        return await original_query(messages)

    host.loop.query = query
    try:
        host.adapter.release.clear()
        primary = await host.send("保持 Codex 运行。", "delegate-primary")
        await asyncio.wait_for(host.adapter.started.wait(), 2)
        binding_before = (host.loop.bound_context_id, host.loop._binding.token)
        delegated = await host.send("另外让 Other Provider 检查。", "delegate-other")
        await asyncio.wait_for(other.started.wait(), 2)
        other_record = host.runtime.get_run(delegated["run_id"])
        await other_record.task_handle
        for _ in range(20):
            if any(row.get("run_id") == delegated["run_id"]
                    for row in host.loop.history):
                break
            await asyncio.sleep(0.01)
        assert (host.loop.bound_context_id, host.loop._binding.token) == binding_before
        assert delegated["child_id"] != primary["child_id"]
        target = host.loop.children[delegated["child_id"]]
        assert target.provider == other.provider_id
        assert other.requests[0].provider == other.provider_id
        with host.ledger._lock:
            effect = dict(host.ledger._db.execute("""SELECT * FROM control_effect_outbox
                WHERE root_id=(SELECT root_id FROM control_admissions
                    WHERE utterance_id='delegate-other')""").fetchone())
        payload = json.loads(effect["payload_json"])
        assert payload["context_id"] == delegated["child_id"]
        assert payload["source_binding_context_id"] == primary["child_id"]
        event = next(row for row in host.loop.history
            if row.get("run_id") == delegated["run_id"])
        assert event["binding_relation_at_observation"] == "retained"
        assert primary["child_id"] == host.loop.bound_context_id
        host.adapter.release.set()
        await host.loop.wait()
        delegated_session = target.native_session
        reply = await host.send("回复 Other Provider。", "delegate-reply")
        await host.loop.wait()
        assert reply["child_id"] == target.child_id
        assert len(host.loop.children) == 2
        assert other.requests[-1].session == delegated_session
        assert (host.loop.bound_context_id, host.loop._binding.token) == binding_before
        continued = await host.send("继续默认上下文。", "delegate-default")
        await host.loop.wait()
        assert continued["child_id"] == primary["child_id"]
        assert host.adapter.requests[-1].session == (
            host.loop.children[primary["child_id"]].native_session)
    finally:
        host.adapter.release.set()
        other.release.set()
        await host.close()


async def test_additional_provider_can_start_from_empty_default_binding(host_factory):
    host = host_factory()
    other = InteractiveNativeFixture()
    other.provider_id = "other-provider"
    other.manifest = replace(other.manifest, provider_id=other.provider_id,
        display_name="Other provider")
    host.runtime.register(other)
    host.loop.context_requirements[other.provider_id] = ProviderRequirements(
        workspace_access="write", workspace_ownership="caller", resume="attach")

    async def query(messages):
        frame = json.loads(messages[-1]["content"])
        return json.dumps({"say":"追加で頼む。",
            "action":{"op":"delegate", "provider":"other-provider"}
            if frame["source_kind"] == "user" else None}, ensure_ascii=False)

    host.loop.query = query
    try:
        original_token = host.loop._binding.token
        delegated = await host.send("另外让 Other Provider 检查。", "delegate-empty")
        await host.loop.wait()
        assert host.loop.bound_context_id == ""
        assert host.loop._binding.token == original_token
        with host.ledger._lock:
            effect = dict(host.ledger._db.execute(
                "SELECT * FROM control_effect_outbox").fetchone())
        payload = json.loads(effect["payload_json"])
        assert payload["source_binding_context_id"] == ""
        assert payload["context_id"] == delegated["child_id"]
        assert len(host.loop.children) == 1 and len(other.requests) == 1
    finally:
        await host.close()


async def test_addressed_retained_provider_requires_one_exact_context(host_factory):
    host = host_factory()
    attention = AttentionRequestCoordinator()
    manager_view = SimpleNamespace(attention=attention)
    other = InteractiveNativeFixture()
    other.provider_id = "other-provider"
    other.manifest = replace(other.manifest, provider_id=other.provider_id,
        display_name="Other provider")
    host.runtime.register(other)
    host.loop.context_requirements[other.provider_id] = ProviderRequirements(
        workspace_access="write", workspace_ownership="caller", resume="attach")
    first = host.loop._create_context("Other one", other.provider_id)
    second = host.loop._create_context("Other two", other.provider_id)

    async def query(messages):
        frame = json.loads(messages[-1]["content"])
        return json.dumps({"say":"どちらか確認が必要。",
            "action":{"op":"send_to", "provider":"other-provider"}
            if frame["source_kind"] == "user" else None}, ensure_ascii=False)

    host.loop.query = query
    host.ingress.address_request = lambda ingress, turn_id, receipt, admission: (
        CooperativeChatManager.request_addressed_context(
            manager_view, ingress, turn_id, receipt, admission
        )
    )
    try:
        receipt = await host.send("它继续。", "ambiguous-retained")
        assert receipt["state"] == "address_selection_required"
        assert receipt["reason"] == "addressed_context_ambiguous"
        request, = attention.list_pending("conversation-A")
        assert request["title"] == "Choose the reply context"
        selected_option = next(option for option in request["options"]
            if Path(second.workspace).name in option["label"])
        selected = await attention.resolve(session_id="conversation-A",
            request_id=request["id"], option_id=selected_option["id"])
        assert selected["ok"] is True
        await host.loop.wait()
        actual = host.ingress.receipts["ambiguous-retained"]
        assert actual["state"] == "started"
        assert actual["child_id"] == second.child_id
        assert host.loop.bound_context_id == ""
        assert {first.child_id, second.child_id} == set(host.loop.children)
        assert len(other.requests) == 1
    finally:
        attention.reset_for_tests()
        await host.close()


async def test_ambiguous_scope_change_preserves_all_retained_choices_until_selection(
        host_factory):
    host = host_factory()
    attention = AttentionRequestCoordinator()
    manager_view = SimpleNamespace(attention=attention)
    try:
        first = await host.send("检查 A。", "scope-ambiguous-a")
        await host.loop.wait()
        original = host.loop.children[first["child_id"]]
        second = host.loop._create_context("上下文 B", host.adapter.provider_id)
        third = host.loop._create_context("上下文 C", host.adapter.provider_id)
        host.loop.bind_context(second.child_id)

        request = await CooperativeChatManager.request_scope_change(
            manager_view, host.ingress, "scope-ambiguous", {"target":""}
        )
        retained = [option for option in request["options"]
            if option.get("metadata", {}).get("relation") in {"current", "existing"}]
        assert len(retained) == 3
        assert {Path(original.workspace).name, Path(second.workspace).name,
            Path(third.workspace).name} <= {
                option["label"].removeprefix("Stay in ").removeprefix("Switch to ")
                for option in retained}

        selected = next(option for option in retained
            if Path(third.workspace).name in option["label"])
        resolved = await attention.resolve(session_id="conversation-A",
            request_id=request["id"], option_id=selected["id"])
        assert resolved["ok"] is True
        assert host.loop.bound_context_id == third.child_id
        assert len(host.adapter.requests) == 1
    finally:
        attention.reset_for_tests()
        await host.close()


async def test_scope_selection_survives_pending_and_bound_expression_failures(
        host_factory):
    host = host_factory()
    attention = AttentionRequestCoordinator()
    manager_view = SimpleNamespace(attention=attention)
    try:
        first = await host.send("检查 A。", "scope-expression-a")
        await host.loop.wait()

        async def failing_expression_query(messages):
            frame = json.loads(messages[-1]["content"])
            if frame["source_kind"] == "user":
                return json.dumps({"say":"请选择。",
                    "action":{"op":"scope_change", "target":"new workspace"}},
                    ensure_ascii=False)
            raise RuntimeError("expression unavailable")

        host.loop.query = failing_expression_query
        host.ingress.scope_request = lambda ingress, turn_id, receipt: (
            CooperativeChatManager.request_scope_change(
                manager_view, ingress, turn_id, receipt
            )
        )
        scope = await host.send("换到新工作区。", "scope-expression-change")
        assert scope["state"] == "scope_change_required"
        request, = attention.list_pending("conversation-A")
        new_option = next(option for option in request["options"]
            if option.get("metadata", {}).get("relation") == "new")
        resolved = await attention.resolve(session_id="conversation-A",
            request_id=request["id"], option_id=new_option["id"])
        assert resolved["ok"] is True
        assert host.loop.bound_context_id != first["child_id"]
        failures = [row for row in host.loop.trace
            if row.get("kind") == "presentation_failed"]
        assert len(failures) == 2
    finally:
        attention.reset_for_tests()
        await host.close()


async def test_scope_choice_opens_trusted_project_read_only_without_work_or_scratch(
        host_factory, tmp_path):
    host = host_factory()
    attention = AttentionRequestCoordinator()
    work = WorkLedgerStore(host.ledger.path)
    project_path = tmp_path/"trusted-project"
    project_path.mkdir()
    project = work.create_or_get_project(str(project_path), name="Trusted Project")
    destination = WorkDestinationService(work,
        registry_check=lambda path: Path(path).resolve() == project_path.resolve(),
        scratch_root_provider=lambda: tmp_path/"unused-scratch")
    manager_view = SimpleNamespace(attention=attention, destination=destination)
    try:
        first = await host.send("检查 scratch。", "project-scope-first")
        await host.loop.wait()
        scratch_directories = {path.resolve() for path in tmp_path.iterdir()
            if path.is_dir() and path != project_path}

        request = await CooperativeChatManager.request_scope_change(
            manager_view, host.ingress, "project-scope", {"target":"Trusted Project"}
        )
        option = next(row for row in request["options"]
            if row["label"] == "Read-only project Trusted Project")
        resolved = await attention.resolve(session_id="conversation-A",
            request_id=request["id"], option_id=option["id"])
        assert resolved["ok"] is True
        child = host.loop.children[host.loop.bound_context_id]
        assert child.child_id != first["child_id"]
        assert Path(child.workspace) == project_path.resolve()
        assert child.requirements.workspace_access == "read"
        assert child.workspace_route == {"status":"resolved",
            "source":"cooperative_project_selection",
            "projectId":project.project_id, "workItemId":"",
            "cwd":str(project_path.resolve())}
        assert {path.resolve() for path in tmp_path.iterdir()
            if path.is_dir() and path != project_path} == scratch_directories
        assert child.work_item_id == "" and work.list_work_items() == []
        assert work.list_writer_leases() == []

        read = await host.send("只读检查项目。", "project-scope-read")
        await host.loop.wait()
        assert read["child_id"] == child.child_id
        assert host.adapter.requests[-1].cwd == str(project_path.resolve())
        assert host.adapter.requests[-1].requirements.workspace_access == "read"
        assert work.list_work_items() == [] and work.list_writer_leases() == []
        with host.ledger._transaction() as db:
            stored = db.execute("""SELECT workspace_route FROM cooperative_contexts
                WHERE session_id=? AND context_id=?""",
                ("conversation-A", child.child_id)).fetchone()
        assert json.loads(stored["workspace_route"]) == child.workspace_route
    finally:
        attention.reset_for_tests()
        await host.close()
        work.close()


async def test_project_scope_choice_revalidates_trust_before_binding(
        host_factory, tmp_path):
    host = host_factory()
    attention = AttentionRequestCoordinator()
    work = WorkLedgerStore(host.ledger.path)
    project_path = tmp_path/"revoked-project"
    project_path.mkdir()
    project = work.create_or_get_project(str(project_path), name="Revoked Project")
    trusted = {"value":True}
    destination = WorkDestinationService(work,
        registry_check=lambda _path: trusted["value"],
        scratch_root_provider=lambda: tmp_path/"unused-scratch")
    manager_view = SimpleNamespace(attention=attention, destination=destination)
    try:
        first = await host.send("检查原上下文。", "revoked-project-first")
        await host.loop.wait()
        before_contexts = set(host.loop.children)
        before_runs = len(host.adapter.requests)
        request = await CooperativeChatManager.request_scope_change(
            manager_view, host.ingress, "revoked-project-scope",
            {"target":"Revoked Project"})
        option = next(row for row in request["options"]
            if row["label"] == "Read-only project Revoked Project")
        trusted["value"] = False
        resolved = await attention.resolve(session_id="conversation-A",
            request_id=request["id"], option_id=option["id"])
        assert resolved["ok"] is False
        assert host.loop.bound_context_id == first["child_id"]
        assert set(host.loop.children) == before_contexts
        assert len(host.adapter.requests) == before_runs
        assert work.get_project(project.project_id) is not None
    finally:
        attention.reset_for_tests()
        await host.close()
        work.close()


async def test_handler_close_failure_still_closes_owned_loop_and_runtime(host_factory, monkeypatch):
    host = host_factory()
    runtime_close = AsyncMock(wraps=host.runtime.close)
    monkeypatch.setattr(host.runtime, "close", runtime_close)
    monkeypatch.setattr(host.ingress.handler, "close",
        AsyncMock(side_effect=OSError("durable fence unavailable")))
    try:
        with pytest.raises(OSError, match="durable fence unavailable"):
            await host.ingress.close()
        assert host.loop._closed
        runtime_close.assert_awaited_once()
    finally:
        if not host.loop._closed:
            await host.loop.close()
        host.ledger.close()


async def test_abort_can_find_started_run_while_role_display_is_waiting(
        host_factory):
    host = host_factory()
    display_entered, release_display = asyncio.Event(), asyncio.Event()

    async def slow_display(_event):
        display_entered.set()
        await release_display.wait()
        return True

    host.loop.publish = slow_display
    host.adapter.release.clear()
    sending = asyncio.create_task(host.send("检查目录。", "display-wait-turn"))
    try:
        await asyncio.wait_for(display_entered.wait(), 2)
        receipt = host.ingress.receipts["display-wait-turn"]
        assert receipt["state"] == "started" and receipt["run_id"]
        manager_view = SimpleNamespace(ingresses={"conversation-A":host.ingress})
        stopped = await CooperativeChatManager.abort_turn(
            manager_view, "display-wait-turn", "conversation-A"
        )
        assert stopped["state"] == "stopped"
        assert host.runtime.get_run(receipt["run_id"]).status == "cancelled"
        assert not sending.done()
        release_display.set()
        assert (await sending)["run_id"] == receipt["run_id"]
        await host.loop.wait()
    finally:
        release_display.set()
        host.adapter.release.set()
        await host.close()


async def test_abort_receipt_cannot_cancel_a_successor_run(host_factory):
    host = host_factory()
    try:
        first = await host.send("第一轮。", "first-turn")
        await host.loop.wait()
        host.adapter.started.clear()
        host.adapter.release.clear()
        second = await host.send("第二轮。", "second-turn")
        await asyncio.wait_for(host.adapter.started.wait(), 2)
        manager_view = SimpleNamespace(ingresses={"conversation-A":host.ingress})
        stale = await CooperativeChatManager.abort_turn(
            manager_view, "first-turn", "conversation-A"
        )
        assert stale == {"state":"stale", "reason":"cooperative_run_changed",
            "child_id":first["child_id"], "run_id":first["run_id"],
            "current_run_id":second["run_id"]}
        assert host.runtime.get_run(second["run_id"]).status == "running"
    finally:
        host.adapter.release.set()
        await host.close()


async def test_production_manager_keeps_one_handler_and_shared_runtime_across_sessions(tmp_path, monkeypatch):
    # This test installs a fresh foreground owner; other modules may have left
    # observations on the process singleton without owning this test's Handler.
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path/"sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    emit = AsyncMock()
    monkeypatch.setattr("server.handlers.chat_handler.bus.emit", emit)
    sm.create_session("conversation-A")
    sm.create_session("conversation-B", activate=False)
    database = tmp_path/"host.sqlite3"
    ledger = ControlLedgerStore(database)
    permission_store = WorkLedgerStore(database)
    handler = ChatHandler()
    runtime, adapter = ProviderRuntime(), InteractiveNativeFixture()
    runtime.register(adapter)
    requirements = ProviderRequirements(workspace_access="write",
        workspace_ownership="caller", resume="attach")

    query = _current_task_query()

    def allocate(label, context_id):
        path = tmp_path/"children"/context_id
        path.mkdir(parents=True)
        return path

    published = []
    manager = CooperativeChatManager(handler, ledger=ledger,
        fence_scope="cooperative:production-test", provider="recovery-test",
        runtime=runtime, context_requirements={"recovery-test":requirements},
        allocate=allocate, query=query,
        publish_factory=lambda session_id:lambda event:published.append((session_id, event)) is None,
        permission_policy="deny", permission_store=permission_store)
    def prepare_request(request, run_id, intake_authority=None):
        if request.metadata.get("cooperative_context_id"):
            return manager.prepare_runtime_request(request, run_id, intake_authority)
        return request

    runtime.set_request_preparer(prepare_request)
    runtime.set_native_session_checkpoint(manager.checkpoint_native_session)
    manager.install()
    assert handler._stream_llm_query == manager.run
    assert handler._interaction_branch_router is None and handler._control_ledger is ledger
    assert handler._control_authority_mode == "turn_decision"
    assert sm._activation_guard == handler.invalidate_session_context

    async def send(session_id, text, key):
        result = await handler._handle_send({"text":text, "turn_id":key,
            "utterance_id":key, "session_id":session_id})
        assert result["status"] == "ok"
        await handler._stream_task
        ingress = manager.ingresses[session_id]
        await ingress.loop.wait()
        return ingress.receipts[key]

    try:
        first = await send("conversation-A", "检查 A。", "a-1")
        second = await send("conversation-B", "检查 B。", "b-1")
        third = await send("conversation-A", "继续检查 A。", "a-2")
        assert [first["state"], second["state"], third["state"]] == ["started"] * 3
        assert set(manager.ingresses) == {"conversation-A", "conversation-B"}
        a_ingress, b_ingress = (manager.ingresses[key]
            for key in ("conversation-A", "conversation-B"))
        assert a_ingress.loop.runtime is b_ingress.loop.runtime is runtime
        assert len(adapter.requests) == 3
        assert [request.metadata["turn_id"] for request in adapter.requests] == ["a-1", "b-1", "a-2"]
        assert adapter.requests[0].session is None
        assert adapter.requests[1].session is None
        assert adapter.requests[2].session in adapter.handles.values()
        assert adapter.requests[2].session != adapter.requests[1].session
        assert a_ingress.loop.bound_context_id != b_ingress.loop.bound_context_id
        outside = tmp_path/"ordinary-workspace"
        outside.mkdir()
        ordinary = await runtime.start(ProviderRunRequest(provider="recovery-test",
            task="ordinary Work path", cwd=str(outside), requirements=requirements))
        await ordinary.task_handle
        assert ordinary.status == "done" and len(adapter.requests) == 4
        adapter.release.clear()
        active = await handler._handle_send({"text":"继续运行。", "turn_id":"a-3-turn",
            "utterance_id":"a-3-source", "session_id":"conversation-A"})
        assert active["status"] == "ok"
        await handler._stream_task
        a_ingress = manager.ingresses["conversation-A"]
        active_receipt = a_ingress.receipts["a-3-source"]
        assert a_ingress.utterance_by_turn["a-3-turn"] == "a-3-source"
        assert active_receipt["utterance_id"] == "a-3-source"
        assert active_receipt["turn_id"] == "a-3-turn"
        assert active_receipt.get("run_id"), active_receipt
        record = runtime.get_run(active_receipt["run_id"])
        scratch_lease = permission_store.get_cooperative_writer_lease(
            a_ingress.loop.children[a_ingress.loop.bound_context_id].run_effect_id)
        assert scratch_lease is None
        assert adapter.requests[-1].requirements.workspace_access == "read"
        assert record.metadata["turn_id"] == "a-3-turn"
        assert record.metadata["source_utterance_id"] == "a-3-source"
        assert any(session_id == "conversation-A" and event["cause"] == "a-3-turn"
            for session_id, event in published)
        assert any(call.args[0] == Method.CHAT_COMPLETE
            and call.args[1]["turn_id"] == "a-3-turn" for call in emit.await_args_list)
        history, _ = sm._read_session_history("conversation-A")
        assert next(row for row in reversed(history.dialog)
            if row["role"] == "user")["turn_id"] == "a-3-turn"
        permission = {"provider":"recovery-test", "run_id":record.run_id,
            "type":"permission.requested", "metadata":dict(record.metadata),
            "payload":{"permissionRequest":{"request_id":"native-permission-a-3",
                "capability":"shell.execute", "action":"execute_command",
                "options":["allow_once", "deny"]}}}
        spoofed = {**permission, "metadata":{**permission["metadata"],
            "cooperative_context_id":"other-context"}}
        await manager._handle_provider_event("provider.event", spoofed)
        assert not adapter.permission_responses
        await manager._handle_provider_event("provider.event", permission)
        response_run, response = adapter.permission_responses[0]
        assert response_run == record.run_id
        assert response.request_id == "native-permission-a-3"
        assert response.allow is False and response.automatic is True
        assert response.reason == "cooperative_read_only_context"
        assert manager.permission_receipts[-1]["accepted"] is True
        stored_permissions = permission_store.list_cooperative_permission_requests(
            "conversation-A", context_id=a_ingress.loop.bound_context_id,
            provider_run_id=record.run_id)
        assert len(stored_permissions) == 1
        stored_permission = stored_permissions[0]
        assert stored_permission.owner_kind == "cooperative_run"
        assert stored_permission.status == "denied"
        assert not stored_permission.work_item_id and not stored_permission.attempt_id
        assert stored_permission.metadata["provider_request_id"] == (
            "native-permission-a-3")
        assert stored_permission.metadata["provider_options"] == [
            "allow_once", "deny"]
        await manager._handle_provider_event("provider.event", permission)
        assert len(adapter.permission_responses) == 1
        assert manager.permission_receipts[-1]["replayed"] is True
        stopped = await handler._handle_abort({"turn_id":"a-3-turn"})
        assert stopped["status"] == "aborted"
        assert stopped["execution_stop"]["state"] == "stopped"
        assert runtime.get_run(active_receipt["run_id"]).status == "cancelled"
        await manager.ingresses["conversation-A"].loop.wait()
        assert permission_store.get_cooperative_writer_lease(
            a_ingress.loop.children[a_ingress.loop.bound_context_id].run_effect_id
        ) is None
        with ledger._lock:
            effects = [dict(row) for row in ledger._db.execute(
                "SELECT * FROM control_effect_outbox ORDER BY effect_id")]
        assert len(effects) == 4
        assert all(row["kind"] == "provider" and row["state"] == "terminal"
            for row in effects)
        a3_effect = next(row for row in effects
            if json.loads(row["payload_json"])["source_utterance_id"] == "a-3-source")
        assert json.loads(a3_effect["payload_json"])["turn_id"] == "a-3-turn"
        assert ledger.get_receipt(a3_effect["effect_id"])["external_id"] == (
            active_receipt["run_id"])
    finally:
        adapter.release.set()
        await handler.close()
        await manager.close()
        await runtime.close()
        permission_store.close()
        ledger.close()
    assert sm._activation_guard is None


async def test_production_manager_uses_explicit_session_project_as_read_only_first_context(
        tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path/"sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    emitted = AsyncMock()
    monkeypatch.setattr("server.handlers.chat_handler.bus.emit", emitted)
    sm.create_session("project-session")
    database = tmp_path/"project-host.sqlite3"
    ledger = ControlLedgerStore(database)
    work = WorkLedgerStore(database)
    project_path = tmp_path/"selected-project"
    project_path.mkdir()
    project = work.create_or_get_project(str(project_path), name="Selected Project")
    other_project_path = tmp_path/"other-selected-project"
    other_project_path.mkdir()
    other_project = work.create_or_get_project(str(other_project_path),
        name="Other Selected Project")
    trusted_paths = {project_path.resolve(), other_project_path.resolve()}
    destination = WorkDestinationService(work,
        registry_check=lambda path: Path(path).resolve() in trusted_paths,
        scratch_root_provider=lambda: tmp_path/"unused-scratch")
    destination.bind_session_context("project-session", project.project_id,
        source="explicit_test_selection")
    handler = ChatHandler()
    runtime, adapter = ProviderRuntime(), InteractiveNativeFixture()
    runtime.register(adapter)
    requirements = ProviderRequirements(workspace_access="write",
        workspace_ownership="caller", resume="attach")
    frames = []

    query = _current_task_query(frames=frames)

    def allocate(_label, _context_id):
        raise AssertionError("an explicit Project destination must not allocate scratch")

    manager = CooperativeChatManager(handler, ledger=ledger,
        fence_scope="cooperative:project-test", provider="recovery-test",
        runtime=runtime, context_requirements={"recovery-test":requirements},
        allocate=allocate, query=query, publish_factory=lambda _session:lambda _event:True,
        destination=destination, permission_policy="deny", permission_store=work)
    runtime.set_request_preparer(lambda request, run_id, intake_authority=None:
        manager.prepare_runtime_request(request, run_id, intake_authority))
    runtime.set_native_session_checkpoint(manager.checkpoint_native_session)
    manager.install()
    try:
        accepted = await handler._handle_send({"text":"只读检查当前项目。",
            "turn_id":"project-turn", "utterance_id":"project-source",
            "session_id":"project-session"})
        assert accepted["status"] == "ok"
        await handler._stream_task
        ingress = manager.ingresses["project-session"]
        await ingress.loop.wait()
        child = ingress.loop.children[ingress.loop.bound_context_id]
        assert frames[0]["context"]["workspace"] is None
        initial = frames[0]["context"]["initial_destination"]
        assert initial["workspace"] == str(project_path.resolve())
        assert initial["requirements"]["workspace_access"] == "read"
        assert frames[0]["context"]["requirements"]["workspace_access"] == "read"
        assert child.workspace == str(project_path.resolve())
        assert child.requirements.workspace_access == "read"
        assert child.workspace_route["projectId"] == project.project_id
        assert child.workspace_route["source"] == "cooperative_session_project"
        request, = adapter.requests
        assert request.cwd == str(project_path.resolve())
        assert request.requirements.workspace_access == "read"
        assert request.metadata["workspace_routing_source"] == (
            "cooperative_session_project")
        assert child.work_item_id == ""
        assert work.list_work_items() == [] and work.list_writer_leases() == []
        assert not (tmp_path/"unused-scratch").exists()

        destination.bind_session_context("project-session", other_project.project_id,
            source="later_explicit_selection")
        continued = await handler._handle_send({"text":"继续只读检查。",
            "turn_id":"project-turn-2", "utterance_id":"project-source-2",
            "session_id":"project-session"})
        assert continued["status"] == "ok"
        await handler._stream_task
        await ingress.loop.wait()
        assert len(adapter.requests) == 2
        assert adapter.requests[-1].cwd == str(project_path.resolve())
        assert adapter.requests[-1].session == child.native_session

        trusted_paths.remove(project_path.resolve())
        rejected = await handler._handle_send({"text":"再检查一次。",
            "turn_id":"project-turn-3", "utterance_id":"project-source-3",
            "session_id":"project-session"})
        assert rejected["status"] == "ok"
        await handler._stream_task
        receipt = ingress.receipts["project-source-3"]
        assert receipt["state"] == "rejected"
        assert receipt["reason"] == "workspace_destination_unavailable"
        assert len(adapter.requests) == 2
        assert ingress.loop.bound_context_id == child.child_id
    finally:
        adapter.release.set()
        await handler.close()
        await manager.close()
        await runtime.close()
        work.close()
        ledger.close()
    assert sm._activation_guard is None


async def test_initial_destination_uses_explicit_work_and_draft_but_not_latest_work(
        tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path/"sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    scratch_root = tmp_path/"scratch"
    scratch_root.mkdir()
    monkeypatch.setattr("config.settings.WORK_SCRATCH_ROOT", str(scratch_root))
    for session_id in ("work-session", "draft-session", "latest-session"):
        sm.create_session(session_id, activate=session_id == "work-session")

    database = tmp_path/"destination-host.sqlite3"
    ledger = ControlLedgerStore(database)
    work = WorkLedgerStore(database)
    project_path = tmp_path/"destination-project"
    worktree = tmp_path/"selected-worktree"
    draft_path = scratch_root/"selected-draft"
    for path in (project_path, worktree, draft_path):
        path.mkdir()
    project = work.create_or_get_project(project_path, name="Destination Project")
    scratch = work.create_or_get_project(scratch_root, name="scratch",
        metadata={"scratch":True})
    selected_work = work.create_work_item(project.project_id,
        title="Selected Work", workspace_path=str(worktree))
    selected_draft = work.create_work_item(scratch.project_id,
        title="Selected Draft", workspace_path=str(draft_path))
    destination = WorkDestinationService(work,
        registry_check=lambda path: Path(path).resolve() == project_path.resolve(),
        scratch_root_provider=lambda: scratch_root)
    destination.bind_session_context("work-session", project.project_id,
        work_item_id=selected_work.work_item_id, source="explicit_work")
    destination.bind_session_context("draft-session", "",
        work_item_id=selected_draft.work_item_id, source="explicit_draft")
    destination.bind_session_context("latest-session", project.project_id,
        source="explicit_project")
    work.set_session_active_work_item("latest-session", selected_work.work_item_id,
        metadata={"source":"work_intake"})

    handler = ChatHandler()
    runtime, adapter = ProviderRuntime(), InteractiveNativeFixture()
    runtime.register(adapter)
    attention = AttentionRequestCoordinator()
    requirements = ProviderRequirements(workspace_access="write",
        workspace_ownership="caller", resume="attach")

    def reject_scratch_allocation(*_args):
        raise AssertionError("explicit destination must not allocate scratch")

    manager = CooperativeChatManager(handler, ledger=ledger,
        fence_scope="cooperative:destination-test", provider="recovery-test",
        runtime=runtime, context_requirements={"recovery-test":requirements},
        allocate=reject_scratch_allocation,
        query=AsyncMock(), destination=destination, attention=attention,
        permission_store=work)
    try:
        work_ingress = await manager._ingress_for("work-session")
        draft_ingress = await manager._ingress_for("draft-session")
        latest_ingress = await manager._ingress_for("latest-session")
        work_route = work_ingress.loop.initial_destination(
            "recovery-test", requirements)
        draft_route = draft_ingress.loop.initial_destination(
            "recovery-test", requirements)
        latest_route = latest_ingress.loop.initial_destination(
            "recovery-test", requirements)
        assert Path(work_route["workspace"]) == worktree.resolve()
        assert work_route["workspace_route"]["workItemId"] == selected_work.work_item_id
        assert work_route["workspace_route"]["destinationKind"] == "work_item"
        assert Path(draft_route["workspace"]) == draft_path.resolve()
        assert draft_route["workspace_route"]["workItemId"] == selected_draft.work_item_id
        assert draft_route["workspace_route"]["destinationKind"] == "draft"
        assert Path(latest_route["workspace"]) == project_path.resolve()
        assert latest_route["workspace_route"]["source"] == (
            "cooperative_session_project")
        assert not latest_route["workspace_route"]["workItemId"]
        assert all(route["requirements"].workspace_access == "read"
            for route in (work_route, draft_route, latest_route))
        scope = await manager.request_scope_change(latest_ingress, "latest-scope",
            {"target":"selected work"})
        assert not [option for option in scope["options"]
            if "Selected Work" in option["label"]]
        draft_scope = await manager.request_scope_change(draft_ingress,
            "draft-scope", {"target":"Selected Draft"})
        assert {option["label"] for option in draft_scope["options"]
            if "Selected Draft" in option["label"]} == {
                "Read-only selected draft Selected Draft",
            }
        readable_draft = next(option for option in draft_scope["options"]
            if option["label"] == "Read-only selected draft Selected Draft")
        sm.load_session("draft-session")
        selected = await attention.resolve(session_id="draft-session",
            request_id=draft_scope["id"], option_id=readable_draft["id"])
        assert selected["ok"] is True
        draft_child = draft_ingress.loop.children[
            draft_ingress.loop.bound_context_id]
        assert Path(draft_child.workspace) == draft_path.resolve()
        assert draft_child.requirements.workspace_access == "read"
        assert draft_child.workspace_route["source"] == (
            "cooperative_session_work_item")
        assert draft_child.workspace_route["destinationKind"] == "draft"
        assert draft_child.work_item_id == ""
        assert work.list_attempts(selected_work.work_item_id) == []
        assert work.list_attempts(selected_draft.work_item_id) == []
        assert work.list_writer_leases() == []
    finally:
        attention.reset_for_tests()
        await manager.close()
        await runtime.close()
        work.close()
        ledger.close()


async def test_legacy_writable_context_uses_the_shared_cross_owner_writer_lease(
        tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path/"sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    monkeypatch.setattr("server.handlers.chat_handler.bus.emit", AsyncMock())
    sm.create_session("writer-session")
    database = tmp_path/"writer-host.sqlite3"
    ledger = ControlLedgerStore(database)
    work = WorkLedgerStore(database)
    project_path = tmp_path/"writer-project"
    project_path.mkdir()
    project = work.create_or_get_project(project_path, name="Writer Project")
    work_item = work.create_work_item(project.project_id,
        title="Competing Work", workspace_path=project_path)
    work_attempt = work.create_attempt(work_item.work_item_id,
        provider="recovery-test", task="Competing writer")
    destination = WorkDestinationService(work,
        registry_check=lambda path: Path(path).resolve() == project_path.resolve(),
        scratch_root_provider=lambda: tmp_path/"scratch")
    attention = AttentionRequestCoordinator()
    handler = ChatHandler()
    runtime, adapter = ProviderRuntime(), InteractiveNativeFixture()
    runtime.register(adapter)
    adapter.release.clear()
    requirements = ProviderRequirements(workspace_access="write",
        workspace_ownership="caller", resume="attach")

    query = _current_task_query()

    def allocate(label, context_id):
        path = tmp_path/"scratch"/context_id
        path.mkdir(parents=True)
        return path

    manager = CooperativeChatManager(handler, ledger=ledger,
        fence_scope="cooperative:writer-test", provider="recovery-test",
        runtime=runtime, context_requirements={"recovery-test":requirements},
        allocate=allocate, query=query, publish_factory=lambda _session:lambda _event:True,
        destination=destination, attention=attention,
        permission_policy="deny", permission_store=work)
    runtime.set_request_preparer(lambda request, run_id, intake_authority=None:
        manager.prepare_runtime_request(request, run_id, intake_authority))
    runtime.set_native_session_checkpoint(manager.checkpoint_native_session)
    manager.install()
    try:
        ingress = await manager._ingress_for("writer-session")
        scope = await manager.request_scope_change(ingress, "writer-scope",
            {"target":"Writer Project"})
        assert not any(row["label"].startswith("Writable") for row in scope["options"])
        # Exercise the retained pre-convergence contract directly. The installed
        # scope UI no longer creates a writable conversation.
        ingress.loop.context_requirements["recovery-test"] = requirements
        child = ingress.loop._create_context("Legacy Writer Project", "recovery-test",
            workspace=str(project_path.resolve()), workspace_route={"status":"resolved",
                "source":"cooperative_project_write_selection", "projectId":project.project_id,
                "workItemId":"", "cwd":str(project_path.resolve())})
        ingress.loop.bind_context(child.child_id)
        assert child.requirements.workspace_access == "write"
        assert child.workspace_route["source"] == (
            "cooperative_project_write_selection")

        accepted = await handler._handle_send({"text":"写入 context.txt。",
            "turn_id":"writer-turn", "utterance_id":"writer-source",
            "session_id":"writer-session"})
        assert accepted["status"] == "ok"
        await handler._stream_task
        await asyncio.wait_for(adapter.started.wait(), 2)
        receipt = ingress.receipts["writer-source"]
        lease = work.get_cooperative_writer_lease(child.run_effect_id)
        assert lease is not None and lease.status == "active"
        assert lease.provider_run_id == receipt["run_id"]
        assert lease.owner_kind == "cooperative_run"
        assert not lease.work_item_id and not lease.attempt_id
        with pytest.raises(ProviderStartAdmissionRejected,
                match="accepted Provider effect authority"):
            manager.prepare_runtime_request(adapter.requests[0],
                "foreign-run", None)
        unchanged_lease = work.get_cooperative_writer_lease(child.run_effect_id)
        assert unchanged_lease is not None
        assert unchanged_lease.status == "active"
        assert unchanged_lease.provider_run_id == receipt["run_id"]
        with pytest.raises(WorkLedgerConflict, match="active writer"):
            work.acquire_writer_lease(work_item.work_item_id,
                work_attempt.attempt_id, workspace_path=project_path)

        def fail_atomic_release(cls, cursor, provider_effect_id, **kwargs):
            del cls, cursor, provider_effect_id, kwargs
            raise OSError("lease store temporarily unavailable")

        with monkeypatch.context() as atomic_failure:
            atomic_failure.setattr(WorkLedgerStore,
                "release_cooperative_writer_lease_in_transaction",
                classmethod(fail_atomic_release))
            adapter.release.set()
            with pytest.raises(OSError, match="lease store temporarily unavailable"):
                await ingress.loop.wait()
        lease = work.get_cooperative_writer_lease(child.run_effect_id)
        assert lease is not None and lease.status == "active"
        effect = ledger.get_effect(child.run_effect_id)
        assert effect["state"] == "running"
        ingress.loop._settle_terminal_run(child, runtime.get_run(receipt["run_id"]))
        lease = work.get_cooperative_writer_lease(child.run_effect_id)
        assert lease is not None and lease.status == "released"
        with work._transaction() as cursor:
            cursor.execute("""UPDATE workspace_leases SET status='active',
                released_at=NULL WHERE lease_id=?""", (lease.lease_id,))
        manager._release_terminal_writer_leases("writer-session", ingress.loop)
        assert work.get_cooperative_writer_lease(
            child.run_effect_id).status == "released"

        before_runs = len(adapter.requests)
        with monkeypatch.context() as intake_failure:
            def reject_workspace_binding(request, manifest):
                del request, manifest
                raise OSError("workspace validation failed")

            intake_failure.setattr(provider_runtime_module,
                "prepare_workspace_binding", reject_workspace_binding)
            rejected_start = await handler._handle_send({"text":"再次写入。",
                "turn_id":"writer-intake-fail-turn",
                "utterance_id":"writer-intake-fail-source",
                "session_id":"writer-session"})
            assert rejected_start["status"] == "ok"
            await handler._stream_task
        failed_receipt = ingress.receipts["writer-intake-fail-source"]
        assert failed_receipt["state"] == "rejected"
        assert failed_receipt["reason"] == (
            "accepted_effect_pre_execution_failed:OSError")
        assert len(adapter.requests) == before_runs
        with ledger._lock:
            effects = [dict(row) for row in ledger._db.execute(
                "SELECT * FROM control_effect_outbox")]
        intake_effect = next(row for row in effects
            if json.loads(row["payload_json"])["source_utterance_id"]
            == "writer-intake-fail-source")
        assert intake_effect["state"] == "terminal"
        intake_lease = work.get_cooperative_writer_lease(intake_effect["effect_id"])
        assert intake_lease is not None and intake_lease.status == "released"
        assert intake_lease.provider_run_id == intake_effect["external_id"]
        assert runtime.get_run(intake_effect["external_id"]) is None

        work_lease = work.acquire_writer_lease(work_item.work_item_id,
            work_attempt.attempt_id, workspace_path=project_path)
        assert work_lease.status == "active"
        before_runs = len(adapter.requests)
        conflict = await handler._handle_send({"text":"再次写入。",
            "turn_id":"writer-conflict-turn",
            "utterance_id":"writer-conflict-source",
            "session_id":"writer-session"})
        assert conflict["status"] == "ok"
        await handler._stream_task
        conflict_receipt = ingress.receipts["writer-conflict-source"]
        assert conflict_receipt["state"] == "rejected"
        assert conflict_receipt["reason"] == "writer_lease_conflict"
        assert len(adapter.requests) == before_runs
        assert work.get_writer_lease(work_attempt.attempt_id).status == "active"
        active_leases = work.list_writer_leases(active_only=True)
        assert len(active_leases) == 1
        assert active_leases[0].owner_kind == "work_attempt"
        with ledger._lock:
            effects = [dict(row) for row in ledger._db.execute(
                "SELECT * FROM control_effect_outbox")]
        conflict_effect = next(row for row in effects
            if json.loads(row["payload_json"])["source_utterance_id"]
            == "writer-conflict-source")
        assert conflict_effect["state"] == "terminal"
        assert ledger.get_receipt(conflict_effect["effect_id"])["outcome"] == "failed"
        work.release_writer_lease(work_attempt.attempt_id)

        source = "生成一份正式报告。"
        admission = capture_turn_admission(utterance_id="work-v6-source",
            turn_id="work-v6-turn", session_id="writer-session",
            transcript=source, chat_epoch=99, authority_mode="turn_decision")
        control = WorkControl(ledger, work,
            cooperative_context_resolver=ingress.loop._state.work_recipient)
        control.admit(admission, fence_scope="work-v6-test")
        payload = WorkCooperativeContextPayloadV6(
            provider=child.provider, task=source,
            title=ProviderEventIngestor.task_title(source),
            project_id=project.project_id, session_id="writer-session",
            utterance_id=admission.utterance_id, turn_id=admission.turn_id,
            source_user_text=source, source_user_context="",
            source_context_scope=admission.dialogue_source_scope,
            source_proof=CurrentTurnSourceSpanV1.capture(admission, source,
                start=0, end=len(source)), requirements=child.requirements,
            cooperative_context_id=child.child_id,
            cooperative_binding_token=ingress.loop._binding.token,
            cooperative_context_revision=child.revision)
        sealed = control.seal(admission, payload)
        request = control.provider_request(sealed["effect_id"])
        assert request.cwd == child.workspace and request.session is None
        attachment = control.addressed_context(ProviderRunIntakeAuthority(
            sealed["effect_id"], kind="control_work_effect"))
        assert attachment is not None and attachment.session == child.native_session
        assert attachment.audit["cooperative_context_id"] == child.child_id
        # This is a Work request attaching to a context, not a native-loop run
        # that would suppress WorkObserver and install its own result narrator.
        assert not request.metadata.get("cooperative_context_id")
        assert WorkCooperativeContextPayloadV6.from_payload(
            payload.to_payload()) == payload
    finally:
        adapter.release.set()
        attention.reset_for_tests()
        await handler.close()
        await manager.close()
        await runtime.close()
        work.close()
        ledger.close()


async def test_cooperative_manager_reuses_focused_auip_read_step_and_leave_without_provider(
        tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path/"sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    sm.create_session("auip-session")
    ledger = ControlLedgerStore(tmp_path/"auip-host.sqlite3")
    handler = ChatHandler()
    runtime, adapter = ProviderRuntime(), InteractiveNativeFixture()
    runtime.register(adapter)
    requirements = ProviderRequirements(workspace_access="write",
        workspace_ownership="caller", resume="attach")
    expressions = []

    async def query(messages):
        frame = json.loads(messages[-1]["content"])
        expressions.append(frame)
        if frame["source_kind"] == "host_receipt" and frame["current"].get(
                "state") == "auip_step_pending":
            assert "今その手順を実行すること" in messages[0]["content"]
            assert "後回しや次の手順にしてはいけません" in messages[0]["content"]
        if frame["current"].get("state") == "auip_read":
            assert frame["current"]["question"] == "读取应用状态。"
            assert frame["current"]["facts"] == "应用当前状态是 active。"
            return "アプリは利用できる状態よ。"
        return "アプリの操作を受け付けたわ。"

    def allocate(_label, context_id):
        path = tmp_path/"children"/context_id
        path.mkdir(parents=True)
        return path

    class Decision:
        status = "ok"
        app_session_id = "app-focused"
        work_relation = "subsumed"

        def __init__(self, action, *, read_facets=(), instruction=""):
            self.action = action
            self.read_facets = read_facets
            self.instruction = instruction

        def control_attrs(self):
            return {"action":self.action,
                "_host_app_session_id":self.app_session_id,
                **({"instruction":self.instruction} if self.instruction else {})}

    class Decider:
        def capture(self, *, user_text, **_kwargs):
            if user_text == "读取应用状态。":
                return Decision("none", read_facets=("state",))
            if user_text == "让应用执行一步。":
                return Decision("step", instruction=user_text)
            if user_text == "关闭这个应用。":
                return Decision("leave")
            return None

        def render_read_only_answer(self, decision, *, language):
            assert decision.read_facets == ("state",) and language == "ja"
            return "应用当前状态是 active。"

    routed = []

    async def route(attrs, **facts):
        routed.append((dict(attrs), dict(facts)))
        return {"ok":True, "action":attrs["action"]}

    manager = CooperativeChatManager(handler, ledger=ledger,
        fence_scope="cooperative:auip-test", provider="recovery-test",
        runtime=runtime, context_requirements={"recovery-test":requirements},
        allocate=allocate, query=query,
        publish_factory=lambda _session:lambda _event:True)
    manager.configure_auip(Decider(), route)
    manager.install()

    async def send(text, key):
        accepted = await handler._handle_send({"text":text, "turn_id":key,
            "utterance_id":key, "session_id":"auip-session"})
        assert accepted["status"] == "ok"
        await asyncio.wait_for(handler._stream_task, 2)
        return manager.ingresses["auip-session"].receipts[key]

    try:
        read = await send("读取应用状态。", "auip-read")
        step = await send("让应用执行一步。", "auip-step")
        replay = await handler._handle_send({"text":"让应用执行一步。",
            "turn_id":"auip-step", "utterance_id":"auip-step",
            "session_id":"auip-session"})
        assert replay["status"] == "replayed"
        assert len(routed) == 1
        leave = await send("关闭这个应用。", "auip-leave")
        assert read["state"] == "auip_read"
        assert read["display_text"] == "アプリは利用できる状態よ。"
        assert [step["state"], leave["state"]] == ["auip_applied"] * 2
        assert [call[0]["action"] for call in routed] == ["step", "leave"]
        assert all(call[0]["_host_app_session_id"] == "app-focused"
            for call in routed)
        assert all(call[1]["session_id"] == "auip-session" for call in routed)
        assert len(expressions) == 3
        ingress = manager.ingresses["auip-session"]
        assert ingress.loop.children == {} and adapter.requests == []
        with ledger._lock:
            assert ledger._db.execute(
                "SELECT count(*) FROM control_effect_outbox").fetchone()[0] == 0
        history, _ = sm._read_session_history("auip-session")
        assert [row["content"] for row in history.dialog if row["role"] == "user"] == [
            "读取应用状态。", "让应用执行一步。", "关闭这个应用。"]
    finally:
        await handler.close()
        await manager.close()
        await runtime.close()
        ledger.close()


async def test_recipient_hint_without_work_ledger_cannot_authorize_result_handoff(
        tmp_path, monkeypatch):
    """A later natural turn binds the current Work; it does not create another run."""

    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path/"sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    session_id = "auip-after-work-session"
    sm.create_session(session_id)
    ledger = ControlLedgerStore(tmp_path/"auip-after-work-host.sqlite3")
    handler = ChatHandler()
    runtime, adapter = ProviderRuntime(), InteractiveNativeFixture()
    runtime.register(adapter)
    requirements = ProviderRequirements(workspace_access="write",
        workspace_ownership="caller", resume="attach")
    decision_calls = []
    routed = []

    async def query(messages):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] == "host_receipt":
            assert frame["current"]["state"] == "auip_rejected"
            return "対象を確認できないわ。"
        return json.dumps({"say":"这条请求没有接到应用交接。", "action":None},
            ensure_ascii=False)

    def decision_for(attempt_ids):
        from server.auip_control_decision import AuipControlDecision

        return AuipControlDecision(status="ok", action="launch", timing="after_work",
            mode="collaborate", active_work_attempt_ids=attempt_ids)

    class Decider:
        def capture(self, *, user_text, **facts):
            if facts.get("active_required"):
                return None
            decision_calls.append((user_text, dict(facts)))
            if user_text == "完成后打开这个结果，我们一起试一下。":
                return decision_for(("work-attempt-current",))
            if user_text == "完成后打开另一个结果。":
                return decision_for(("work-attempt-other",))
            if user_text == "绑定变化时不要打开。":
                manager._work_dispatches["session:" + session_id] = (
                    SimpleNamespace(effect_id="work-effect-successor",
                        binding={"work_item_id":"work-item-successor",
                            "attempt_id":"work-attempt-successor",
                            "provider_run_id":record.run_id}))
                return decision_for(("work-attempt-current",))
            return None

    async def route(attrs, **facts):
        routed.append((dict(attrs), dict(facts)))
        return {"ok":True, "deferred":True, "turn_id":facts["turn_id"]}

    def allocate(_label, context_id):
        path = tmp_path/"children"/context_id
        path.mkdir(parents=True)
        return path

    manager = CooperativeChatManager(handler, ledger=ledger,
        fence_scope="cooperative:auip-after-work-test",
        provider="recovery-test", runtime=runtime,
        context_requirements={"recovery-test":requirements},
        allocate=allocate, query=query,
        publish_factory=lambda _session:lambda _event:True)
    manager.configure_auip(Decider(), route,
        entry_context=lambda _session:"作業の成果を開く入口です。")
    manager.install()
    adapter.release.clear()
    record = await runtime.start(ProviderRunRequest(
        provider="recovery-test", task="Build a counter application",
        cwd=str(tmp_path), requirements=requirements,
        metadata={"source":"control_work_effect"}))
    await asyncio.wait_for(adapter.started.wait(), 2)
    manager._work_dispatches["session:" + session_id] = SimpleNamespace(
        effect_id="work-effect-current",
        binding={"work_item_id":"work-item-current",
            "attempt_id":"work-attempt-current",
            "provider_run_id":record.run_id})

    async def send(text, key):
        accepted = await handler._handle_send({"text":text, "turn_id":key,
            "utterance_id":key, "session_id":session_id})
        assert accepted["status"] == "ok"
        await asyncio.wait_for(handler._stream_task, 2)
        return manager.ingresses[session_id].receipts[key]

    try:
        receipt = await send("完成后打开这个结果，我们一起试一下。",
            "auip-after-work")
        assert receipt["state"] == "auip_rejected"
        assert receipt["outcome"]["error"] == "work_target_owner_unavailable"
        assert routed == []
        assert len(adapter.requests) == 1
        first_call = decision_calls[0][1]
        assert not first_call.get("active_required")

        replay = await handler._handle_send({
            "text":"完成后打开这个结果，我们一起试一下。",
            "turn_id":"auip-after-work", "utterance_id":"auip-after-work",
            "session_id":session_id})
        assert replay["status"] == "replayed"
        assert routed == [] and len(decision_calls) == 1

        wrong = await send("完成后打开另一个结果。", "auip-after-work-wrong")
        assert wrong["state"] == "auip_rejected"
        assert routed == [] and len(adapter.requests) == 1
        assert not decision_calls[-1][1].get("active_required")

        changed = await handler._handle_send({
            "text":"绑定变化时不要打开。", "turn_id":"auip-after-work-changed",
            "utterance_id":"auip-after-work-changed", "session_id":session_id})
        assert changed["status"] == "ok"
        await asyncio.wait_for(handler._stream_task, 2)
        assert manager.ingresses[session_id].receipts["auip-after-work-changed"]["state"] == "auip_rejected"
        assert routed == [] and len(adapter.requests) == 1
    finally:
        adapter.release.set()
        await handler.close()
        await manager.close()
        await runtime.close()
        ledger.close()


async def test_cooperative_manager_uses_exact_browser_lease_without_continue_to_new_fallback(
        tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path/"sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    sm.create_session("browser-session")
    ledger = ControlLedgerStore(tmp_path/"browser-host.sqlite3")
    handler = ChatHandler()
    runtime, adapter = ProviderRuntime(), InteractiveNativeFixture()
    runtime.register(adapter)
    requirements = ProviderRequirements(workspace_access="write",
        workspace_ownership="caller", resume="attach")
    provider_calls = []

    async def provider_run(params):
        provider_calls.append(dict(params))
        return {"run":{"run_id":"browser-run-2", "provider":"browser",
            "status":"running"}}

    browser = InteractionBranchCoordinator(provider_run=provider_run,
        root=tmp_path/"branches")
    branch = InteractionBranchState(branch_id="branch-exact",
        parent_session_id="browser-session", provider="browser", status="idle",
        goal="Inspect counter", browser_session_id="browser-native",
        title="Counter", url="http://example.test/counter",
        page_summary="Counter value is 0", expires_at=10**12)
    browser._active_by_session["browser-session"] = branch
    browser.configure()
    frames = []

    async def query(messages):
        frame = json.loads(messages[-1]["content"])
        frames.append(frame)
        action = None
        if frame["source_kind"] == "user":
            text = frame["current"]["text"]
            if text == "点一次加一按钮。":
                action = {"op":"browser", "intent":"continue"}
            elif text == "结束这次网页操作。":
                action = {"op":"browser", "intent":"close"}
            elif text == "继续点刚才那个。":
                action = {"op":"browser", "intent":"continue"}
            elif text == "打开 https://example.test/new。":
                action = {"op":"browser", "intent":"open",
                    "target":"https://example.test/new"}
        return json.dumps({"say":"好的。", "action":action}, ensure_ascii=False)

    def allocate(_label, context_id):
        path = tmp_path/"children"/context_id
        path.mkdir(parents=True)
        return path

    manager = CooperativeChatManager(handler, ledger=ledger,
        fence_scope="cooperative:browser-test", provider="recovery-test",
        runtime=runtime, context_requirements={"recovery-test":requirements},
        allocate=allocate, query=query,
        publish_factory=lambda _session:lambda _event:True)
    manager.configure_browser(browser)
    manager.install()

    async def send(text, key):
        accepted = await handler._handle_send({"text":text, "turn_id":key,
            "utterance_id":key, "session_id":"browser-session"})
        assert accepted["status"] == "ok"
        await asyncio.wait_for(handler._stream_task, 2)
        return manager.ingresses["browser-session"].receipts[key]

    try:
        continued = await send("点一次加一按钮。", "browser-continue")
        assert continued["state"] == "browser_accepted"
        assert len(provider_calls) == 1
        assert provider_calls[0]["metadata"]["interaction_branch_id"] == (
            "branch-exact")
        assert provider_calls[0]["metadata"]["browser_session_id"] == (
            "browser-native")
        assert provider_calls[0]["metadata"]["branch_user_message"] == (
            "点一次加一按钮。")
        await browser._on_provider_result(Method.PROVIDER_RESULT,
            {"provider":"browser", "run_id":"browser-run-2", "status":"done",
             "task":"点一次加一按钮。", "result":"Counter value is 1",
             "metadata":{"session_id":"browser-session",
                 "interaction_branch_id":"branch-exact",
                 "browser":{"browser_session_id":"browser-native",
                     "current_url":"http://example.test/counter", "title":"Counter"},
                 "provider_branch":{"branch_id":"branch-exact", "actions":[],
                     "final_report":"Counter value is 1"}}})
        thanks = await send("谢谢。", "browser-thanks")
        assert thanks["state"] == "no_action"
        closed = await send("结束这次网页操作。", "browser-close")
        assert closed["state"] == "browser_closed"
        assert browser.active_branch_for_session("browser-session") is None
        after = await send("继续点刚才那个。", "browser-after-close")
        assert after["state"] == "rejected"
        assert after["reason"] == "browser_context_unavailable"
        assert len(provider_calls) == 1
        after_frame = next(frame for frame in reversed(frames)
            if frame.get("source_kind") == "user"
            and frame.get("current", {}).get("text") == "继续点刚才那个。")
        assert after_frame["context"].get("browser_branch") is None
        assert manager.ingresses["browser-session"].loop.children == {}
        opened = await send("打开 https://example.test/new。", "browser-new-entry")
        assert opened["state"] == "browser_accepted"
        assert len(provider_calls) == 2
        assert provider_calls[-1]["metadata"]["browser_action"] == "open"
        assert provider_calls[-1]["metadata"]["url"] == "https://example.test/new"
        assert provider_calls[-1]["metadata"]["interaction_branch_routing_scope"][
            "state"] == "absent"
        with ledger._lock:
            assert ledger._db.execute(
                "SELECT count(*) FROM control_effect_outbox").fetchone()[0] == 0
    finally:
        await handler.close()
        await manager.close()
        await runtime.close()
        bus.off(Method.PROVIDER_RESULT, browser._on_provider_result)
        bus.off(Method.PROVIDER_EVENT, browser._on_provider_event)
        browser._subscribed = False
        import server.interaction_branch as interaction_branch_module
        if interaction_branch_module._current_coordinator is browser:
            interaction_branch_module._current_coordinator = None
        ledger.close()


async def test_cooperative_domains_coexist_without_stealing_work_stop_or_unknown_facts(
        tmp_path, monkeypatch):
    """Work, Browser and AUIP keep independent owners in one admitted Session."""

    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path/"sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    monkeypatch.setattr("server.handlers.chat_handler.bus.emit", AsyncMock())
    session_id = "coexisting-domain-session"
    sm.create_session(session_id)
    ledger = ControlLedgerStore(tmp_path/"coexisting-domain-host.sqlite3")
    handler = ChatHandler()
    runtime, adapter = ProviderRuntime(), InteractiveNativeFixture()
    runtime.register(adapter)
    requirements = ProviderRequirements(workspace_access="write",
        workspace_ownership="caller", resume="attach")

    browser_runs = []
    browser_cancels = []

    async def browser_run(params):
        browser_runs.append(dict(params))
        return {"run":{"run_id":"browser-run-coexisting",
            "provider":"browser", "status":"running"}}

    async def browser_cancel(run_id, **_kwargs):
        browser_cancels.append(run_id)
        return {"cancelled":False, "reason":"browser_transport_unknown",
            "run":{"run_id":run_id, "provider":"browser", "status":"running"}}

    browser = InteractionBranchCoordinator(provider_run=browser_run,
        provider_cancel=browser_cancel, root=tmp_path/"branches")
    branch = InteractionBranchState(branch_id="branch-coexisting",
        parent_session_id=session_id, provider="browser", status="idle",
        goal="Inspect counter", browser_session_id="browser-native-coexisting",
        title="Counter", url="http://example.test/counter",
        page_summary="Counter value is 0", expires_at=10**12)
    browser._active_by_session[session_id] = branch
    browser.configure()

    class AuipReadDecision:
        status = "ok"
        app_session_id = "app-coexisting"
        work_relation = "subsumed"
        action = "none"
        read_facets = ("state",)

    class AuipDecider:
        def capture(self, *, user_text, **_kwargs):
            return AuipReadDecision() if user_text == "读取应用状态。" else None

        def render_read_only_answer(self, decision, *, language):
            assert decision.app_session_id == "app-coexisting"
            assert language == "ja"
            return "应用当前状态是 active。"

    auip_routes = []

    async def route_auip(attrs, **facts):
        auip_routes.append((dict(attrs), dict(facts)))
        return {"ok":True}

    frames = []

    async def query(messages):
        if "typed reference-set resolver" in messages[0]["content"]:
            return json.dumps({"references":[]})
        frame = json.loads(messages[-1]["content"])
        frames.append(frame)
        action = None
        say = "好的。"
        if frame["source_kind"] != "user":
            if frame["current"].get("state") == "auip_read":
                assert frame["current"]["facts"] == "应用当前状态是 active。"
                return "アプリは利用できる状態よ。"
            return "受け付けた内容を確認したわ。"
        if frame["source_kind"] == "user":
            text = frame["current"]["text"]
            if text == "操作当前网页。":
                action = {"op":"browser", "intent":"continue"}
            elif text == "关闭当前网页。":
                action = {"op":"browser", "intent":"close"}
            elif text == "停止这份报告。":
                action = {"op":"interrupt"}
            elif text == "停止报告并操作网页。":
                # Fix the semantic model's no-action result here; the domain
                # assertions below verify it cannot borrow another operation.
                say = "这是两个独立操作；本轮都没有启动，请分别提出。"
        return json.dumps({"say":say, "action":action}, ensure_ascii=False)

    def allocate(_label, context_id):
        path = tmp_path/"children"/context_id
        path.mkdir(parents=True)
        return path

    manager = CooperativeChatManager(handler, ledger=ledger,
        fence_scope="cooperative:coexisting-domain-test",
        provider="recovery-test", runtime=runtime,
        context_requirements={"recovery-test":requirements},
        allocate=allocate, query=query,
        publish_factory=lambda _session:lambda _event:True)
    manager.configure_auip(AuipDecider(), route_auip)
    manager.configure_browser(browser)
    manager.install()
    work_record = None

    async def send(text, key):
        accepted = await handler._handle_send({"text":text, "turn_id":key,
            "utterance_id":key, "session_id":session_id})
        assert accepted["status"] == "ok"
        await asyncio.wait_for(handler._stream_task, 2)
        return manager.ingresses[session_id].receipts[key]

    try:
        ingress = await manager._ingress_for(session_id)
        workspace = tmp_path/"work-context"
        workspace.mkdir()
        child = ingress.loop._create_context("Work context", "recovery-test",
            requirements=replace(requirements, workspace_access="read"), workspace=str(workspace))
        ingress.loop.bind_context(child.child_id)

        adapter.release.clear()
        work_record = await runtime.start(ProviderRunRequest(
            provider="recovery-test", task="Active report Work",
            cwd=str(workspace), requirements=requirements,
            metadata={"source":"control_work_effect"}))
        await asyncio.wait_for(adapter.started.wait(), 2)
        assert work_record.status == "running"
        manager._work_dispatches[child.child_id] = SimpleNamespace(
            effect_id="work-effect-coexisting",
            binding={"work_item_id":"work-item-coexisting",
                "attempt_id":"work-attempt-coexisting",
                "provider_run_id":work_record.run_id})

        ordinary = await send("谢谢，先保持现状。", "coexisting-ordinary")
        compound = await send("停止报告并操作网页。", "coexisting-compound")
        assert ordinary["state"] == compound["state"] == "no_action"
        assert runtime.get_run(work_record.run_id).status == "running"
        assert browser.active_branch_for_session(session_id) is branch
        assert browser_runs == [] and auip_routes == []

        first_read = await send("读取应用状态。", "coexisting-auip-read-1")
        assert first_read["state"] == "auip_read"
        assert first_read["app_session_id"] == "app-coexisting"
        assert first_read["display_text"] == "アプリは利用できる状態よ。"
        assert browser_runs == [] and auip_routes == []
        assert runtime.get_run(work_record.run_id).status == "running"

        operated = await send("操作当前网页。", "coexisting-browser-continue")
        assert operated["state"] == "browser_accepted"
        assert operated["run_id"] == "browser-run-coexisting"
        assert len(browser_runs) == 1
        assert browser_runs[0]["metadata"]["interaction_branch_id"] == (
            "branch-coexisting")
        assert runtime.get_run(work_record.run_id).status == "running"

        uncertain_close = await send(
            "关闭当前网页。", "coexisting-browser-close")
        assert uncertain_close["state"] == "browser_unknown"
        assert "browser_transport_unknown" in uncertain_close["reason"]
        assert browser_cancels == ["browser-run-coexisting"]
        assert browser.active_branch_for_session(session_id) is None
        pending, = browser.termination_pending_for_session(session_id)
        assert pending.run_id == "browser-run-coexisting"
        assert runtime.get_run(work_record.run_id).status == "running"

        second_read = await send("读取应用状态。", "coexisting-auip-read-2")
        assert second_read["state"] == "auip_read"
        assert browser.termination_pending_for_session(session_id)[0] == pending
        assert runtime.get_run(work_record.run_id).status == "running"

        stopped = await send("停止这份报告。", "coexisting-work-stop")
        # This fixture injected a dispatch hint without an accepted Work record.
        # That hint alone is no longer task cancellation authority.
        assert stopped["state"] == "rejected"
        assert stopped["reason"] == "task_stop_target_none"
        assert runtime.get_run(work_record.run_id).status == "running"
        assert browser.termination_pending_for_session(session_id)[0] == pending
        assert manager.ingresses[session_id].receipts[
            "coexisting-browser-close"] == uncertain_close
        assert len(browser_runs) == 1 and auip_routes == []
        with ledger._lock:
            assert ledger._db.execute(
                "SELECT count(*) FROM control_effect_outbox").fetchone()[0] == 0
        user_frames = {frame["current"]["text"]:frame for frame in frames
            if frame.get("source_kind") == "user"}
        assert user_frames["操作当前网页。"]["context"][
            "active_work"]["run_id"] == work_record.run_id
        assert user_frames["停止这份报告。"]["context"].get(
            "browser_branch") is None
    finally:
        adapter.release.set()
        await handler.close()
        await manager.close()
        await runtime.close()
        bus.off(Method.PROVIDER_RESULT, browser._on_provider_result)
        bus.off(Method.PROVIDER_EVENT, browser._on_provider_event)
        browser._subscribed = False
        import server.interaction_branch as interaction_branch_module
        if interaction_branch_module._current_coordinator is browser:
            interaction_branch_module._current_coordinator = None
        ledger.close()


async def test_first_unbound_work_uses_draft_intake_without_provider_prewarm(
        tmp_path, monkeypatch):
    """A natural first deliverable uses Work as owner without fabricating context."""

    monkeypatch.setattr("server.work_ledger_coordinator.cwd_in_project_registry",
        lambda _path:True)
    scratch = tmp_path/"drafts"
    monkeypatch.setattr("config.settings.WORK_SCRATCH_ROOT", str(scratch))
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path/"sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    session_id = "unbound-work-session"
    sm.create_session(session_id)
    database = tmp_path/"unbound-work-host.sqlite3"
    ledger = ControlLedgerStore(database)
    work = WorkLedgerStore(database)
    def scratch_root_provider():
        scratch.mkdir(parents=True, exist_ok=True)
        return scratch
    destination = WorkDestinationService(work,
        registry_check=lambda _path:True,
        scratch_root_provider=scratch_root_provider)
    attention = AttentionRequestCoordinator()
    handler = ChatHandler()
    runtime, adapter = ProviderRuntime(), CooperativeWorkFixture()
    runtime.register(adapter)
    requirements = ProviderRequirements(workspace_access="write",
        workspace_ownership="caller", resume="attach")
    frames = []

    async def query(messages):
        if "typed reference-set resolver" in messages[0]["content"]:
            content = messages[-1]["content"]
            marker = "\n\n[Complete typed candidates;"
            assert content.startswith("[Current user message]\n") and marker in content
            phrase = content.removeprefix("[Current user message]\n").split(marker, 1)[0]
            expected = {
                "停止第二份报告。":second["work_item_id"],
                "修改 report-1.md，追加 amended。":first["work_item_id"],
                "修改 report-1.md，追加 batch":first["work_item_id"],
                "顺便告诉我 report-2.md 对应任务现在什么状态。":second["work_item_id"],
                "修改 missing.md":"",
            }
            assert phrase in expected, f"unexpected typed-reference phrase: {phrase!r}"
            work_item_id = expected[phrase]
            tokens = ["work_item:" + work_item_id] if work_item_id else []
            return json.dumps({"references":tokens})
        frame = json.loads(messages[-1]["content"])
        frames.append(frame)
        action = None
        if frame["source_kind"] == "user":
            text = frame["current"]["text"]
            if text in {"生成第一份正式报告。", "生成第二份正式报告。",
                    "生成第三份正式报告。"}:
                action = {"op":"work", "intent":"execute"}
            elif text == "修改 report-1.md，追加 amended。":
                action = {"op":"work", "intent":"amend",
                    "target":"report-1.md"}
            elif text == "停止第二份报告。":
                action = {"op":"interrupt"}
            elif text == "只使用 CPU。":
                action = {"op":"send"}
            elif text == "这份应用用了什么库？":
                action = {"op":"send"}
            elif text == "再生成一份独立报告。":
                action = {"op":"work", "intent":"execute"}
            elif text == "生成个人介绍网页并交付到桌面。":
                action = {"op":"work", "intent":"execute",
                    "external_export_target":"desktop"}
            elif text == (
                    "修改 report-1.md，追加 batch；"
                    "顺便告诉我 report-2.md 对应任务现在什么状态。"):
                action = {"op":"batch", "actions":[
                    {"op":"work", "intent":"amend", "target":"report-1.md",
                        "source":"修改 report-1.md，追加 batch"},
                    {"op":"report", "target":"report-2.md",
                        "source":"顺便告诉我 report-2.md 对应任务现在什么状态。"}]}
            elif text == (
                    "修改 missing.md；"
                    "顺便告诉我 report-2.md 对应任务现在什么状态。"):
                action = {"op":"batch", "actions":[
                    {"op":"work", "intent":"amend", "target":"missing.md",
                        "source":"修改 missing.md"},
                    {"op":"report", "target":"report-2.md",
                        "source":"顺便告诉我 report-2.md 对应任务现在什么状态。"}]}
            elif text == (
                    "生成第三份正式报告；"
                    "顺便告诉我 report-2.md 对应任务现在什么状态。"):
                action = {"op":"batch", "actions":[
                    {"op":"work", "intent":"execute",
                        "source":"生成第三份正式报告"},
                    {"op":"report", "target":"report-2.md",
                        "source":"顺便告诉我 report-2.md 对应任务现在什么状态。"}]}
            elif text == (
                    "生成第四份正式应用；"
                    "完成后打开它，我们一起试一下。"):
                action = {"op":"batch", "actions":[
                    {"op":"work", "intent":"execute",
                        "source":"生成第四份正式应用"},
                    {"op":"auip_after_work", "mode":"collaborate",
                        "source":"完成后打开它，我们一起试一下。"}]}
            elif text == (
                    "生成第五份正式应用；"
                    "完成后打开它让我看。"):
                action = {"op":"batch", "actions":[
                    {"op":"work", "intent":"execute",
                        "source":"生成第五份正式应用"},
                    {"op":"auip_after_work", "mode":"observe",
                        "source":"完成后打开它让我看。"}]}
        return json.dumps({"say":"好的。", "action":action}, ensure_ascii=False)

    allocations = []
    def allocate(_label, context_id):
        path = tmp_path/"children"/context_id
        path.mkdir(parents=True)
        allocations.append(path)
        return path

    manager = CooperativeChatManager(handler, ledger=ledger,
        fence_scope="cooperative:unbound-work-test", provider="recovery-test",
        runtime=runtime, context_requirements={"recovery-test":requirements},
        allocate=allocate, query=query,
        publish_factory=lambda _session:lambda _event:True,
        destination=destination, attention=attention,
        permission_policy="deny", permission_store=work)
    coordinator = WorkLedgerCoordinator(work, provider_start=runtime.start,
        provider_cancel=runtime.cancel,
        current_session_id=lambda:sm.get_current_session_id())
    work_handler = WorkLedgerHandler(coordinator,
        provider_input=runtime.append_input)
    control = WorkControl(ledger, work,
        cooperative_context_resolver=manager.resolve_work_recipient)
    coordinator.configure_work_control(control)
    report_calls = []
    async def report_request(task, attrs):
        report_calls.append((task, dict(attrs),
            runtime.get_run(next(iter(manager._work_dispatches.values())).binding[
                "provider_run_id"]).status))
        return "[report] answered from canonical ledger identity"
    manager.configure_work(control,
        WorkEffectExecutor(control, runtime, coordinator),
        input_request=work_handler.submit_input,
        report_request=report_request)
    auip_reservations = []
    cancelled_reservations = []

    class NoActiveAuipDecision:
        def capture(self, **_kwargs):
            return None

    async def reserve_auip(attrs, **facts):
        auip_reservations.append({"attrs":dict(attrs), "facts":dict(facts),
            "adapter_request_count":len(adapter.requests)})
        return {"ok":True, "deferred":True, "turn_id":facts["turn_id"]}

    def cancel_auip(*, session_id, turn_id):
        cancelled_reservations.append((session_id, turn_id))
        return True

    manager.configure_auip(NoActiveAuipDecision(), reserve_auip,
        cancel_deferred=cancel_auip)

    def prepare_request(request, run_id, intake_authority=None):
        if getattr(intake_authority, "kind", "") == "cooperative_provider_effect":
            return manager.prepare_runtime_request(request, run_id, intake_authority)
        return coordinator.prepare_request(request, run_id, intake_authority)

    runtime.set_request_preparer(prepare_request)
    runtime.set_native_session_checkpoint(manager.checkpoint_native_session)
    coordinator.configure()
    manager.install()

    async def send(text, key):
        accepted = await handler._handle_send({"text":text, "turn_id":key,
            "utterance_id":key, "session_id":session_id})
        assert accepted["status"] == "ok"
        await asyncio.wait_for(handler._stream_task, 2)
        return manager.ingresses[session_id].receipts[key]

    async def finish_work():
        tasks = tuple(manager._work_tasks)
        if tasks:
            await asyncio.gather(*tasks)
            await asyncio.sleep(0)

    try:
        adapter.release.clear()
        first = await send("生成第一份正式报告。", "unbound-work-1")
        await asyncio.wait_for(adapter.started.wait(), 2)
        assert first["state"] == "work_started" and first["child_id"] == ""
        assert manager.ingresses[session_id].loop.children == {}
        assert allocations == []
        assert len(adapter.requests) == 1
        first_item = work.get_work_item(first["work_item_id"])
        assert first_item is not None
        first_path = Path(first_item.workspace_path)
        assert first_path.parent == scratch.resolve()
        assert first_path != scratch.resolve() and (first_path/".git").is_dir()
        first_session = adapter.handles[first["run_id"]]
        adapter.release.set()
        await asyncio.wait_for(runtime.get_run(first["run_id"]).task_handle, 2)
        await finish_work()
        assert work.get_attempt(first["attempt_id"]).execution_status == "succeeded"
        assert any(artifact.title == "report-1.md" for artifact in
            work.list_artifacts(first["work_item_id"],
                attempt_id=first["attempt_id"]))

        adapter.started.clear()
        adapter.release.clear()
        second = await send("生成第二份正式报告。", "unbound-work-2")
        await asyncio.wait_for(adapter.started.wait(), 2)
        second_item = work.get_work_item(second["work_item_id"])
        assert second["state"] == "work_started" and second["child_id"] == ""
        assert second_item is not None
        assert second_item.work_item_id != first_item.work_item_id
        assert second_item.workspace_path != first_item.workspace_path
        assert Path(second_item.workspace_path).parent == scratch.resolve()
        assert len(adapter.requests) == 2

        # Independent unbound Work is covered by test_cooperative_parallel_work.
        # Keep this recovery journey's conversational subject on the second report.

        appended = await send("只使用 CPU。", "unbound-work-input")
        assert appended["state"] == "work_input_accepted"
        assert appended["work_item_id"] == second["work_item_id"]
        await work_handler.drain_inputs()
        assert adapter.inputs == [(second["run_id"], "只使用 CPU。")]
        input_frame = next(frame for frame in frames
            if frame.get("current", {}).get("text") == "只使用 CPU。")
        assert input_frame["context"]["id"] is None
        assert input_frame["context"]["active_work"]["run_id"] == second["run_id"]

        manager._work_dispatches.pop(
            manager._work_owner_key(session_id, ""))
        actual_get_run = runtime.get_run
        with monkeypatch.context() as lost_runtime:
            lost_runtime.setattr(runtime, "get_run", lambda run_id:
                None if run_id == second["run_id"] else actual_get_run(run_id))
            unknown_stop = await send(
                "停止第二份报告。", "unbound-work-stop-unknown")
        assert unknown_stop["state"] == "unknown"
        assert unknown_stop["reason"] == "work_runtime_owner_unavailable"
        assert actual_get_run(second["run_id"]).status == "running"

        stopped = await send("停止第二份报告。", "unbound-work-stop")
        assert stopped["state"] == "stopped"
        assert stopped["work_item_id"] == second["work_item_id"]
        assert runtime.get_run(second["run_id"]).status == "cancelled"
        await finish_work()
        assert work.get_attempt(second["attempt_id"]).execution_status == "cancelled"

        adapter.started.clear()
        adapter.release.clear()
        amended = await send("修改 report-1.md，追加 amended。",
            "unbound-work-amend")
        assert amended["state"] == "work_started"
        await asyncio.wait_for(adapter.started.wait(), 2)
        assert amended["work_item_id"] == first["work_item_id"]
        assert Path(adapter.requests[-1].cwd) == first_path
        assert adapter.requests[-1].session == first_session
        assert len(work.list_work_items()) == 2
        assert len(work.list_operations(first["work_item_id"])) == 2
        assert len(work.list_operations(second["work_item_id"])) == 1
        assert manager.ingresses[session_id].loop.children == {}
        adapter.release.set()
        await asyncio.wait_for(runtime.get_run(amended["run_id"]).task_handle, 2)
        await finish_work()
        assert work.get_attempt(amended["attempt_id"]).execution_status == "succeeded"
        assert len(adapter.requests) == 3

        batch_text = ("修改 report-1.md，追加 batch；"
            "顺便告诉我 report-2.md 对应任务现在什么状态。")
        adapter.started.clear()
        adapter.release.clear()
        batch = await send(batch_text, "unbound-work-report-batch")
        await asyncio.wait_for(adapter.started.wait(), 2)
        assert batch["state"] == "work_report_batch_started"
        assert batch["work"]["state"] == "work_started"
        assert batch["work"]["work_item_id"] == first["work_item_id"]
        assert batch["report_work_item_id"] == second["work_item_id"]
        assert len(report_calls) == 1
        assert report_calls[0][:2] == (
            "顺便告诉我 report-2.md 对应任务现在什么状态。",
            {"intent":"report", "subject":"work_item",
                "workspace_ref":second["work_item_id"],
                "lookup_session_id":session_id,
                "_host_nonblocking_report":True})
        assert report_calls[0][2] in {"queued", "running"}
        assert len(work.list_work_items()) == 2
        assert len(work.list_operations(first["work_item_id"])) == 3
        assert len(work.list_operations(second["work_item_id"])) == 1
        admission = ledger.find_admission(
            f"chat:{session_id}", "unbound-work-report-batch")
        plan = json.loads(admission["plan_json"])
        batch_evidence = plan["evidence"]["cooperative_batch"]
        assert batch_evidence["kind"] == "work_and_report"
        assert [row["op"] for row in batch_evidence["actions"]] == [
            "work", "report"]
        assert [row["work_item_id"] for row in batch_evidence["actions"]] == [
            first["work_item_id"], second["work_item_id"]]
        replay = await handler._handle_send({"text":batch_text,
            "turn_id":"unbound-work-report-batch",
            "utterance_id":"unbound-work-report-batch",
            "session_id":session_id})
        assert replay["status"] == "replayed"
        assert len(report_calls) == 1 and len(adapter.requests) == 4
        assert len([row for row in manager.ingresses[session_id].loop.history
            if row.get("source") == "kurisu"
            and row.get("cause") == "unbound-work-report-batch"]) == 1
        batch_effect = ledger.get_effect(batch["work"]["effect_id"])
        batch_payload = json.loads(batch_effect["payload_json"])
        assert batch_payload["version"] == 4
        assert batch_payload["task"] == "修改 report-1.md，追加 batch"
        assert batch_payload["source_user_text"] == batch_text
        assert adapter.requests[-1].metadata["source_user_text"] == batch_text
        assert adapter.requests[-1].metadata["source_user_operation_text"] == (
            "修改 report-1.md，追加 batch")
        adapter.release.set()
        await asyncio.wait_for(runtime.get_run(
            batch["work"]["run_id"]).task_handle, 2)
        await finish_work()
        assert work.get_attempt(
            batch["work"]["attempt_id"]).execution_status == "succeeded"

        missing_batch = ("修改 missing.md；"
            "顺便告诉我 report-2.md 对应任务现在什么状态。")
        before_missing = (len(adapter.requests), len(report_calls),
            len(work.list_operations(first["work_item_id"])))
        rejected_batch = await send(missing_batch,
            "unbound-work-report-batch-missing")
        assert rejected_batch["state"] == "rejected"
        assert rejected_batch["reason"].startswith(
            "work_report_batch_work_target_")
        assert (len(adapter.requests), len(report_calls),
            len(work.list_operations(first["work_item_id"]))) == before_missing

        execute_batch_text = ("生成第三份正式报告；"
            "顺便告诉我 report-2.md 对应任务现在什么状态。")
        adapter.started.clear()
        adapter.release.clear()
        execute_batch = await send(execute_batch_text,
            "unbound-execute-report-batch")
        await asyncio.wait_for(adapter.started.wait(), 2)
        assert execute_batch["state"] == "work_report_batch_started"
        third_work = execute_batch["work"]["work_item_id"]
        assert third_work not in {first["work_item_id"], second["work_item_id"]}
        third_item = work.get_work_item(third_work)
        assert third_item is not None
        assert Path(third_item.workspace_path).parent == scratch.resolve()
        assert third_item.workspace_path not in {
            first_item.workspace_path, second_item.workspace_path}
        assert execute_batch["report_work_item_id"] == second["work_item_id"]
        assert len(report_calls) == 2
        assert report_calls[-1][1]["workspace_ref"] == second["work_item_id"]
        assert report_calls[-1][2] in {"queued", "running"}
        execute_effect = ledger.get_effect(
            execute_batch["work"]["effect_id"])
        execute_payload = json.loads(execute_effect["payload_json"])
        assert execute_payload["version"] == 3
        assert execute_payload["task"] == "生成第三份正式报告"
        assert execute_payload["source_user_text"] == execute_batch_text
        assert adapter.requests[-1].metadata["source_user_text"] == (
            execute_batch_text)
        assert adapter.requests[-1].metadata["source_user_operation_text"] == (
            "生成第三份正式报告")
        adapter.release.set()
        await asyncio.wait_for(runtime.get_run(
            execute_batch["work"]["run_id"]).task_handle, 2)
        await finish_work()
        assert work.get_attempt(
            execute_batch["work"]["attempt_id"]).execution_status == "succeeded"
        assert len(work.list_work_items()) == 3

        same_turn_text = ("生成第四份正式应用；"
            "完成后打开它，我们一起试一下。")
        before_same_turn_runs = len(adapter.requests)
        before_same_turn_frames = len(frames)
        adapter.started.clear()
        adapter.release.clear()
        same_turn = await send(same_turn_text,
            "unbound-work-auip-same-turn")
        await asyncio.wait_for(adapter.started.wait(), 2)
        assert same_turn["state"] == "work_auip_batch_started"
        assert same_turn["work"]["state"] == "work_started"
        assert same_turn["auip"]["deferred"] is True
        assert len(frames) == before_same_turn_frames + 1
        assert len([row for row in manager.ingresses[session_id].loop.history
            if row.get("source") == "kurisu"
            and row.get("cause") == "unbound-work-auip-same-turn"]) == 1
        assert len(auip_reservations) == 1
        reservation = auip_reservations[0]
        assert reservation["adapter_request_count"] == before_same_turn_runs
        assert reservation["attrs"] == {"action":"launch",
            "target":"delivery", "mode":"collaborate", "after":"work",
            "_host_work_binding":"turn"}
        assert reservation["facts"]["turn_id"] == (
            "unbound-work-auip-same-turn")
        same_turn_admission = ledger.find_admission(
            f"chat:{session_id}", "unbound-work-auip-same-turn")
        same_turn_plan = json.loads(same_turn_admission["plan_json"])
        assert len(same_turn_plan["effects"]) == 1
        same_turn_evidence = same_turn_plan["evidence"]["cooperative_batch"]
        assert same_turn_evidence["kind"] == "work_then_auip_after_work"
        assert [row["op"] for row in same_turn_evidence["actions"]] == [
            "work", "auip_after_work"]
        same_turn_effect = ledger.get_effect(
            same_turn["work"]["effect_id"])
        same_turn_payload = json.loads(same_turn_effect["payload_json"])
        assert same_turn_payload["task"] == "生成第四份正式应用"
        assert same_turn_payload["source_user_text"] == same_turn_text
        assert adapter.requests[-1].metadata["source_user_operation_text"] == (
            "生成第四份正式应用")
        replay = await handler._handle_send({"text":same_turn_text,
            "turn_id":"unbound-work-auip-same-turn",
            "utterance_id":"unbound-work-auip-same-turn",
            "session_id":session_id})
        assert replay["status"] == "replayed"
        assert len(auip_reservations) == 1
        assert len(adapter.requests) == before_same_turn_runs + 1
        assert cancelled_reservations == []

        refused_text = "生成第五份正式应用；完成后打开它让我看。"
        original_router = manager.auip_router
        with monkeypatch.context() as unavailable_destination:
            async def reserve_then_lose_destination(attrs, **facts):
                result = await original_router(attrs, **facts)
                unavailable_destination.setattr(manager.destination, "resolve_workspace_route",
                    lambda *_args, **_kwargs:{"status":"unavailable"})
                return result
            unavailable_destination.setattr(manager, "auip_router", reserve_then_lose_destination)
            refused = await send(refused_text, "unbound-work-auip-unavailable")
        assert refused["state"] == "rejected"
        assert refused["reason"] == "work_destination_unavailable"
        assert len(auip_reservations) == 2
        assert auip_reservations[-1]["attrs"]["mode"] == "observe"
        assert cancelled_reservations == [
            (session_id, "unbound-work-auip-unavailable")]
        assert len(adapter.requests) == before_same_turn_runs + 1
        adapter.release.set()
        await asyncio.wait_for(runtime.get_run(
            same_turn["work"]["run_id"]).task_handle, 2)
        await finish_work()
        assert work.get_attempt(
            same_turn["work"]["attempt_id"]).execution_status == "succeeded"
        assert len(work.list_work_items()) == 4

        adapter.started.clear()
        before_conversation = (len(work.list_work_items()),
            len(work.list_attempts(same_turn["work"]["work_item_id"])))
        ordinary = await send("这份应用用了什么库？", "unbound-context-start")
        await asyncio.wait_for(adapter.started.wait(), 2)
        await manager.ingresses[session_id].loop.wait()
        child = manager.ingresses[session_id].loop.children[
            ordinary["child_id"]]
        assert ordinary["state"] == "started"
        assert len(allocations) == 0
        assert child.native_session is not None
        assert child.work_item_id == same_turn["work"]["work_item_id"]
        last_work = work.get_work_item(child.work_item_id)
        assert Path(child.workspace) == Path(last_work.workspace_path)
        assert adapter.requests[-1].session == adapter.handles[same_turn["work"]["run_id"]]
        assert adapter.requests[-1].requirements.workspace_access == "read"
        assert adapter.requests[-1].metadata["cooperative_work_item_id"] == last_work.work_item_id
        assert (len(work.list_work_items()), len(work.list_attempts(last_work.work_item_id))) == before_conversation
        assert work.get_project_by_path(child.workspace) is None
        assert manager.work_for_recipient(session_id, child.child_id)["work_item_id"] == last_work.work_item_id
        assert manager.ingresses[session_id].loop.bound_context_id == child.child_id

        adapter.started.clear()
        adapter.release.clear()
        desktop = await send("生成个人介绍网页并交付到桌面。",
            "projectless-context-desktop-work")
        await asyncio.wait_for(adapter.started.wait(), 2)
        assert desktop["state"] == "work_started"
        assert desktop["child_id"] == ""
        assert desktop["work_item_id"] != first["work_item_id"]
        desktop_item = work.get_work_item(desktop["work_item_id"])
        assert desktop_item is not None
        assert Path(desktop_item.workspace_path).parent == scratch.resolve()
        assert Path(desktop_item.workspace_path) != Path(child.workspace)
        desktop_effect = ledger.get_effect(desktop["effect_id"])
        desktop_payload = json.loads(desktop_effect["payload_json"])
        assert desktop_payload["version"] == 3
        assert desktop_payload["external_export_target"] == "desktop"
        assert manager.ingresses[session_id].loop.bound_context_id == child.child_id
        adapter.release.set()
        await asyncio.wait_for(runtime.get_run(desktop["run_id"]).task_handle, 2)
        await finish_work()
    finally:
        adapter.release.set()
        attention.reset_for_tests()
        await handler.close()
        await manager.close()
        await runtime.close()
        await work_handler.drain_inputs()
        coordinator.close()
        work.close()
        ledger.close()


async def test_missing_execution_provider_keeps_cooperative_role_only_chat_available(
        tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path/"sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    session_id = "provider-unavailable-session"
    sm.create_session(session_id)
    database = tmp_path/"provider-unavailable.sqlite3"
    ledger = ControlLedgerStore(database)
    work = WorkLedgerStore(database)
    handler = ChatHandler()
    runtime = ProviderRuntime()
    requirements = ProviderRequirements(workspace_access="write",
        workspace_ownership="caller", resume="attach")
    frames = []

    async def query(messages):
        frame = json.loads(messages[-1]["content"])
        frames.append(frame)
        if frame["source_kind"] != "user":
            return json.dumps({"say":"Codex 当前不可用，这次没有执行。",
                "action":None}, ensure_ascii=False)
        assert frame["context"]["provider"] == "missing-provider"
        assert frame["context"]["available"] is False
        text = frame["current"]["text"]
        action = ({"op":"send"} if text == "请让 Codex 检查目录。"
            else {"op":"work", "intent":"execute"}
            if text == "请生成一份正式报告。" else None)
        return json.dumps({"say":"普通回答。", "action":action},
            ensure_ascii=False)

    def forbid_allocate(*_args):
        raise AssertionError("unavailable Provider must not allocate a context")

    manager = CooperativeChatManager(handler, ledger=ledger,
        fence_scope="cooperative:provider-unavailable-test",
        provider="missing-provider", runtime=runtime,
        context_requirements={"missing-provider":requirements},
        allocate=forbid_allocate,
        query=query, publish_factory=lambda _session:lambda _event:True)
    manager.install()

    async def send(text, key):
        accepted = await handler._handle_send({"text":text, "turn_id":key,
            "utterance_id":key, "session_id":session_id})
        assert accepted["status"] == "ok"
        await asyncio.wait_for(handler._stream_task, 2)
        return manager.ingresses[session_id].receipts[key]

    try:
        ordinary = await send("二加二是多少？", "provider-missing-chat")
        requested = await send("请让 Codex 检查目录。", "provider-missing-send")
        work_request = await send(
            "请生成一份正式报告。", "provider-missing-work")
        assert ordinary["state"] == "no_action"
        assert requested == {"state":"rejected",
            "reason":"provider_unavailable", "provider":"missing-provider",
            "input_id":"provider-missing-send",
            "utterance_id":"provider-missing-send",
            "turn_id":"provider-missing-send"}
        assert work_request["state"] == "rejected"
        # This role-only fixture installs no Work owner. Work eligibility is
        # now checked there, after the role has expressed the semantic request.
        assert work_request["reason"] == "work_owner_unavailable"
        ingress = manager.ingresses[session_id]
        assert ingress.loop.children == {}
        assert runtime.list_runs() == []
        assert work.list_work_items() == []
        with ledger._lock:
            assert ledger._db.execute(
                "SELECT count(*) FROM control_effect_outbox").fetchone()[0] == 0
            assert ledger._db.execute(
                "SELECT count(*) FROM run_attempts").fetchone()[0] == 0
        assert len([frame for frame in frames
            if frame["source_kind"] == "host_receipt"]) == 2
    finally:
        await handler.close()
        await manager.close()
        await runtime.close()
        work.close()
        ledger.close()


async def test_cooperative_work_uses_existing_context_without_blocking_chat_and_keeps_stop_open(
        tmp_path, monkeypatch):
    monkeypatch.setattr("server.work_ledger_coordinator.cwd_in_project_registry",
        lambda _path:True)
    async def non_git_workspace(_cwd):
        return {"available":False, "source":"git",
            "reason":"not_a_git_workspace", "repo_root":""}
    monkeypatch.setattr("server.work_artifact_registry.capture_git_baseline",
        non_git_workspace)
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path/"sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    sm.create_session("work-session")
    database = tmp_path/"work-host.sqlite3"
    ledger = ControlLedgerStore(database)
    work = WorkLedgerStore(database)
    project_path = tmp_path/"work-project"
    project_path.mkdir()
    work.create_or_get_project(project_path, name="Work Project")
    destination = WorkDestinationService(work,
        registry_check=lambda path: Path(path).resolve() == project_path.resolve(),
        scratch_root_provider=lambda: tmp_path/"scratch")
    attention = AttentionRequestCoordinator()
    handler = ChatHandler()
    runtime, adapter = ProviderRuntime(), CooperativeWorkFixture()
    runtime.register(adapter)
    requirements = ProviderRequirements(workspace_access="write",
        workspace_ownership="caller", resume="attach")
    frames = []

    async def query(messages):
        if "typed reference-set resolver" in messages[0]["content"]:
            tokens = list(dict.fromkeys(re.findall(
                r"work_item:[A-Za-z0-9._:-]+", messages[-1]["content"])))
            if messages[-1]["content"].startswith("[Current user message]\n停止这个任务。\n"):
                tokens = ["work_item:" + second["work_item_id"]]
            elif messages[-1]["content"].startswith(
                    "[Current user message]\n再次修改 report-1.md，保留原 Work。\n"):
                # Both Work catalogs may expose this shared-workspace filename.
                # This scenario's semantic reply selects the original report;
                # the later "那份报告" case deliberately retains both candidates.
                tokens = ["work_item:" + first["work_item_id"]]
            return json.dumps({"references":tokens})
        frame = json.loads(messages[-1]["content"])
        frames.append(frame)
        action = None
        if frame["source_kind"] == "user":
            text = frame["current"]["text"]
            if text in {"生成第一份正式报告。", "生成第二份正式报告。",
                    "生成第三份正式报告。"}:
                action = {"op":"work", "intent":"execute"}
            elif "修改 report-1.md" in text:
                action = {"op":"work", "intent":"amend", "target":"report-1.md"}
            elif text == "修改那份报告。":
                action = {"op":"work", "intent":"amend", "target":"那份报告"}
            elif text == "停止这个任务。":
                action = {"op":"interrupt"}
            else:
                action = {"op":"send"}
        return json.dumps({"say":"好的。", "action":action}, ensure_ascii=False)

    def allocate(_label, context_id):
        path = tmp_path/"scratch"/context_id
        path.mkdir(parents=True)
        return path

    manager = CooperativeChatManager(handler, ledger=ledger,
        fence_scope="cooperative:work-test", provider="recovery-test",
        runtime=runtime, context_requirements={"recovery-test":requirements},
        allocate=allocate, query=query,
        publish_factory=lambda _session:lambda _event:True,
        destination=destination, attention=attention,
        permission_policy="deny", permission_store=work)
    coordinator = WorkLedgerCoordinator(work, provider_start=runtime.start,
        provider_cancel=runtime.cancel,
        current_session_id=lambda:sm.get_current_session_id())
    work_handler = WorkLedgerHandler(coordinator,
        provider_input=runtime.append_input)
    control = WorkControl(ledger, work,
        cooperative_context_resolver=manager.resolve_work_recipient)
    coordinator.configure_work_control(control)
    manager.configure_work(control, WorkEffectExecutor(control, runtime, coordinator),
        input_request=work_handler.submit_input)

    def prepare_request(request, run_id, intake_authority=None):
        if getattr(intake_authority, "kind", "") == "cooperative_provider_effect":
            return manager.prepare_runtime_request(request, run_id, intake_authority)
        return coordinator.prepare_request(request, run_id, intake_authority)

    runtime.set_request_preparer(prepare_request)
    runtime.set_native_session_checkpoint(manager.checkpoint_native_session)
    coordinator.configure()
    manager.install()

    async def send(text, key):
        accepted = await handler._handle_send({"text":text, "turn_id":key,
            "utterance_id":key, "session_id":"work-session"})
        assert accepted["status"] == "ok"
        await asyncio.wait_for(handler._stream_task, 2)
        return manager.ingresses["work-session"].receipts[key]

    async def finish_work():
        tasks = tuple(manager._work_tasks)
        if tasks:
            await asyncio.gather(*tasks)
            await asyncio.sleep(0)

    try:
        ingress = await manager._ingress_for("work-session")
        scope = await manager.request_scope_change(ingress, "work-scope",
            {"target":"Work Project"})
        option = next(row for row in scope["options"]
            if row["label"] == "Read-only project Work Project")
        selected = await attention.resolve(session_id="work-session",
            request_id=scope["id"], option_id=option["id"])
        assert selected["ok"] is True
        prepared = await send("先建立可续接上下文。", "context-source")
        assert prepared["state"] == "started"
        await ingress.loop.wait()
        child = ingress.loop.children[ingress.loop.bound_context_id]
        assert child.run_status == "done" and child.native_session is not None

        adapter.started.clear()
        adapter.release.clear()
        first = await send("生成第一份正式报告。", "work-source-1")
        assert first["state"] == "work_started", first
        assert not any(frame.get("source_kind") == "host_receipt"
            and frame.get("current", {}).get("state") == "work_started" for frame in frames)
        start_replies = [row for row in ingress.loop.history
            if row.get("source") == "kurisu" and row.get("cause") == "work-source-1"]
        assert len(start_replies) == 1 and start_replies[0]["text"] == "好的。"
        await asyncio.wait_for(adapter.started.wait(), 2)
        first_record = runtime.get_run(first["run_id"])
        assert first_record is not None and first_record.status == "running"
        assert adapter.requests[-1].session == child.native_session
        assert adapter.requests[-1].cwd == child.workspace == str(project_path.resolve())
        assert len(work.list_work_items()) == 1
        assert len(work.list_operations(first["work_item_id"])) == 1
        assert len(work.list_attempts(first["work_item_id"])) == 1
        assert work.get_writer_lease(first["attempt_id"]).status == "active"

        adapter.release.set()
        await asyncio.wait_for(first_record.task_handle, 2)
        await finish_work()
        first_attempt = work.get_attempt(first["attempt_id"])
        assert first_attempt is not None and first_attempt.execution_status == "succeeded"
        assert work.latest_completion(first["work_item_id"]) is not None
        first_artifacts = work.list_artifacts(first["work_item_id"],
            attempt_id=first["attempt_id"])
        assert any(artifact.title == "report-1.md" for artifact in first_artifacts)
        assert work.get_writer_lease(first["attempt_id"]).status == "released"
        assert not any(row.get("cause") == first["run_id"]
            for row in ingress.loop.history if row.get("source") == "kurisu")

        adapter.started.clear()
        adapter.release.clear()
        amended = await send("修改 report-1.md，追加一行 amended。",
            "work-amend-source-1")
        await asyncio.wait_for(adapter.started.wait(), 2)
        assert amended["state"] == "work_started"
        assert amended["work_item_id"] == first["work_item_id"]
        assert amended["attempt_id"] != first["attempt_id"]
        assert len([row for row in ingress.loop.history if row.get("source") == "kurisu"
            and row.get("cause") == "work-amend-source-1"]) == 1
        assert len(work.list_work_items()) == 1
        assert len(work.list_operations(first["work_item_id"])) == 2
        assert len(work.list_attempts(first["work_item_id"])) == 2
        adapter.release.set()
        amended_record = runtime.get_run(amended["run_id"])
        await asyncio.wait_for(amended_record.task_handle, 2)
        await finish_work()
        amended_artifact = next(artifact for artifact in work.list_artifacts(
            first["work_item_id"], attempt_id=amended["attempt_id"])
            if artifact.title == "report-1.md" and artifact.kind == "business.file")
        assert amended_artifact.status == "registered"
        assert amended_artifact.metadata["attribution"] == "lineage_amendment"

        adapter.started.clear()
        adapter.release.clear()
        second = await send("生成第二份正式报告。", "work-source-2")
        await asyncio.wait_for(adapter.started.wait(), 2)
        assert second["state"] == "work_started"
        assert second["work_item_id"] != first["work_item_id"]
        assert len(work.list_work_items()) == 2
        with ledger._lock:
            effects_before_inputs = ledger._db.execute(
                "SELECT COUNT(*) FROM control_effect_outbox").fetchone()[0]

        appended = await send("只使用 CPU。", "work-input-source")
        assert appended["state"] == "work_input_accepted"
        assert appended["work_item_id"] == second["work_item_id"]
        assert appended["run_id"] == second["run_id"]
        await work_handler.drain_inputs()
        provider_inputs = work.list_provider_inputs(second["work_item_id"])
        assert len(provider_inputs) == 1
        assert provider_inputs[0]["state"] == "delivered"
        assert adapter.inputs == [(second["run_id"], "只使用 CPU。")]
        active_frame = next(frame for frame in frames
            if frame.get("current", {}).get("text") == "只使用 CPU。")
        assert active_frame["context"]["active_work"]["run_id"] == second["run_id"]

        await coordinator.drain_provider_facts()
        manager._work_dispatches.pop(child.child_id)
        actual_get_run = runtime.get_run
        with monkeypatch.context() as lost_runtime:
            lost_runtime.setattr(runtime, "get_run", lambda run_id:
                None if run_id == second["run_id"] else actual_get_run(run_id))
            unknown_stop = await send("停止这个任务。", "work-stop-unknown")
        assert unknown_stop["state"] == "unknown"
        assert unknown_stop["reason"] == "work_runtime_owner_unavailable"
        assert actual_get_run(second["run_id"]).status == "running"

        stopped = await send("停止这个任务。", "work-stop-source")
        assert stopped["state"] == "stopped"
        assert stopped["run_id"] == second["run_id"]
        assert runtime.get_run(second["run_id"]).status == "cancelled"
        with ledger._lock:
            effects_after_stop = ledger._db.execute(
                "SELECT COUNT(*) FROM control_effect_outbox").fetchone()[0]
        assert effects_after_stop == effects_before_inputs
        await finish_work()
        second_attempt = work.get_attempt(second["attempt_id"])
        assert second_attempt is not None
        assert second_attempt.execution_status == "cancelled"
        assert work.latest_completion(second["work_item_id"]) is not None
        assert work.get_writer_lease(second["attempt_id"]).status == "released"

        adapter.started.clear()
        adapter.release.clear()
        returned = await send("再次修改 report-1.md，保留原 Work。",
            "work-amend-source-2")
        assert returned["state"] == "work_started", returned
        await asyncio.wait_for(adapter.started.wait(), 2)
        assert returned["work_item_id"] == first["work_item_id"]
        assert returned["work_item_id"] != second["work_item_id"]
        assert len(work.list_work_items()) == 2
        assert len(work.list_operations(first["work_item_id"])) == 3
        adapter.release.set()
        await asyncio.wait_for(runtime.get_run(returned["run_id"]).task_handle, 2)
        await finish_work()

        with ledger._lock:
            before_ambiguity = ledger._db.execute(
                "SELECT COUNT(*) FROM control_effect_outbox").fetchone()[0]
        adapter.started.clear()
        adapter.release.clear()
        ambiguous = await send("修改那份报告。", "work-amend-ambiguous")
        assert ambiguous["state"] == "work_amend_selection_required"
        request, = attention.list_pending("work-session")
        assert request["title"] == "Choose the deliverable to modify"
        assert len(request["options"]) == 2
        with ledger._lock:
            after_ambiguity = ledger._db.execute(
                "SELECT COUNT(*) FROM control_effect_outbox").fetchone()[0]
        assert after_ambiguity == before_ambiguity
        first_option = next(option for option in request["options"]
            if "第一份" in option["label"])
        resolved = await attention.resolve(session_id="work-session",
            request_id=request["id"], option_id=first_option["id"])
        assert resolved["ok"] is True
        selected_amend = resolved["outcome"]
        assert selected_amend.get("work_item_id") == first["work_item_id"], selected_amend
        await asyncio.wait_for(adapter.started.wait(), 2)
        adapter.release.set()
        await asyncio.wait_for(runtime.get_run(
            selected_amend["run_id"]).task_handle, 2)
        await finish_work()

        adapter.started.clear()
        adapter.release.clear()
        third = await send("生成第三份正式报告。", "work-source-3")
        await asyncio.wait_for(adapter.started.wait(), 2)
        aborted = await handler._handle_abort({"turn_id":"work-source-3"})
        assert aborted["status"] == "aborted"
        assert aborted["execution_stop"]["state"] == "stopped"
        assert aborted["execution_stop"]["run_id"] == third["run_id"]
        await finish_work()
        assert work.get_attempt(third["attempt_id"]).execution_status == "cancelled"
        with ledger._lock:
            before_accept_effects = ledger._db.execute(
                "SELECT count(*) FROM control_effect_outbox").fetchone()[0]
        accepted_work = await work_handler.handle(Method.WORK_ACCEPT,
            {"work_item_id":first["work_item_id"]})
        assert accepted_work["work"]
        assert work.get_work_item(first["work_item_id"]).state == "accepted"
        with ledger._lock:
            assert ledger._db.execute(
                "SELECT count(*) FROM control_effect_outbox").fetchone()[0] == (
                    before_accept_effects)
        assert ingress.loop.bound_context_id == child.child_id
    finally:
        adapter.release.set()
        attention.reset_for_tests()
        await handler.close()
        await manager.close()
        await runtime.close()
        await work_handler.drain_inputs()
        coordinator.close()
        work.close()
        ledger.close()


async def test_ask_policy_resolves_only_the_exact_active_cooperative_permission(
        tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path/"sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    emitted = AsyncMock()
    monkeypatch.setattr("server.handlers.chat_handler.bus.emit", emitted)
    sm.create_session("permission-session")
    database = tmp_path/"host.sqlite3"
    ledger = ControlLedgerStore(database)
    permissions = WorkLedgerStore(database)
    handler = ChatHandler()
    runtime, adapter = ProviderRuntime(), InteractiveNativeFixture()
    runtime.register(adapter)
    adapter.release.clear()
    requirements = ProviderRequirements(workspace_access="write",
        workspace_ownership="caller", resume="attach")

    query = _current_task_query()

    def allocate(_label, context_id):
        path = tmp_path/"children"/context_id
        path.mkdir(parents=True)
        return path

    manager = CooperativeChatManager(handler, ledger=ledger,
        fence_scope="cooperative:permission-test", provider="recovery-test",
        runtime=runtime, context_requirements={"recovery-test":requirements},
        allocate=allocate, query=query, publish_factory=lambda _session:lambda _event:True,
        permission_policy="ask", permission_store=permissions)

    def prepare_request(request, run_id, intake_authority=None):
        return manager.prepare_runtime_request(request, run_id, intake_authority)

    runtime.set_request_preparer(prepare_request)
    runtime.set_native_session_checkpoint(manager.checkpoint_native_session)
    manager.install()
    try:
        # Legacy active approvals retain their exact owner; new conversation
        # starts are covered separately by the read-only permission checks.
        legacy_ingress = await manager._ingress_for("permission-session")
        legacy_ingress.loop.context_requirements["recovery-test"] = requirements
        result = await handler._handle_send({"text":"执行需要批准的命令。",
            "turn_id":"permission-turn", "utterance_id":"permission-source",
            "session_id":"permission-session"})
        assert result["status"] == "ok"
        await handler._stream_task
        await asyncio.wait_for(adapter.started.wait(), 2)
        ingress = manager.ingresses["permission-session"]
        receipt = ingress.receipts["permission-source"]
        record = runtime.get_run(receipt["run_id"])
        request_event = {"provider":"recovery-test", "run_id":record.run_id,
            "type":"permission.requested", "metadata":dict(record.metadata),
            "payload":{"permissionRequest":{"request_id":"native-allow-once",
                "capability":"shell.execute", "action":"execute_command",
                "options":["allow_once", "deny"]}}}
        await manager._handle_provider_event(Method.PROVIDER_EVENT, request_event)
        pending = permissions.list_cooperative_permission_requests(
            "permission-session", provider_run_id=record.run_id, status="pending")
        assert len(pending) == 1 and pending[0].options == ["allow_once", "deny"]
        assert not adapter.permission_responses
        permission_canvases = [call.args[1] for call in emitted.await_args_list
            if call.args and call.args[0] == Method.WALLPAPER_CANVAS]
        assert len(permission_canvases) == 1
        card = permission_canvases[0]
        assert card["permissionVisible"] is True
        assert card["permissionRequest"] == {
            "id":pending[0].request_id, "ownerKind":"cooperative_run",
            "sessionId":"permission-session", "runId":record.run_id,
            "providerRequestId":"native-allow-once",
            "capability":"shell.execute", "action":"execute_command",
            "scope":[], "reason":"Explicit user approval is required.",
            "reversibility":"unknown", "status":"pending",
            "options":["allow_once", "deny"]}
        assert "workItemId" not in card["permissionRequest"]
        assert "attemptId" not in card["permissionRequest"]

        router = CanvasActionRouter(
            cooperative_permission_action=manager.resolve_permission)
        action = {"target":"permission", "action":"allow_once",
            "owner_kind":"cooperative_run",
            "permission_request_id":pending[0].request_id,
            "session_id":"permission-session", "run_id":record.run_id,
            "provider_request_id":"native-allow-once"}
        unknown_owner = await router.route({**action, "owner_kind":"unknown"})
        assert unknown_owner == {"ok":False,
            "error":"unsupported_permission_owner"}
        wrong_session = await router.route({**action,
            "session_id":"other-session"})
        assert wrong_session == {"ok":False,
            "error":"cooperative_permission_session_not_current"}
        wrong_request = await router.route({**action,
            "provider_request_id":"different-request"})
        assert wrong_request == {"ok":False,
            "error":"cooperative_permission_not_pending"}
        wrong_internal = await router.route({**action,
            "permission_request_id":"permission-from-another-owner"})
        assert wrong_internal == {"ok":False,
            "error":"cooperative_permission_request_mismatch"}
        assert permissions.get_permission_request(pending[0].request_id).status == "pending"

        wrong = await manager.resolve_permission({"session_id":"permission-session",
            "run_id":"different-run", "provider_request_id":"native-allow-once",
            "allow":True})
        assert wrong == {"ok":False, "error":"cooperative_permission_not_pending"}
        allowed = await router.route(action)
        assert allowed["ok"] is True
        assert allowed["permission"]["status"] == "allowed"
        response_run, response = adapter.permission_responses[0]
        assert response_run == record.run_id
        assert response.request_id == "native-allow-once" and response.allow is True
        stored = permissions.get_permission_request(pending[0].request_id)
        assert stored is not None and stored.status == "allowed"
        assert not stored.work_item_id and not stored.attempt_id
        assert permissions.list_work_items() == [] and permissions.list_projects() == []
        permission_canvases = [call.args[1] for call in emitted.await_args_list
            if call.args and call.args[0] == Method.WALLPAPER_CANVAS]
        assert permission_canvases[-1]["permissionVisible"] is False
        assert permission_canvases[-1]["permissionRequest"]["id"] == pending[0].request_id
        assert permission_canvases[-1].get("visible") is None
        duplicate = await manager.resolve_permission({"session_id":"permission-session",
            "run_id":record.run_id, "provider_request_id":"native-allow-once",
            "allow":True})
        assert duplicate == {"ok":False, "error":"cooperative_permission_not_pending"}

        expiring = {**request_event,
            "payload":{"permissionRequest":{"request_id":"native-expiring",
                "capability":"shell.execute", "action":"execute_command",
                "options":["allow_once", "deny"]}}}
        before_background = len([call for call in emitted.await_args_list
            if call.args and call.args[0] == Method.WALLPAPER_CANVAS])
        with monkeypatch.context() as background_session:
            background_session.setattr(sm, "get_current_session_id",
                lambda:"other-session")
            await manager._handle_provider_event(Method.PROVIDER_EVENT, expiring)
        assert len([call for call in emitted.await_args_list
            if call.args and call.args[0] == Method.WALLPAPER_CANVAS]) == before_background
        expiring_row = next(permission for permission in
            permissions.list_cooperative_permission_requests(
                "permission-session", provider_run_id=record.run_id, status="pending")
            if permission.metadata["provider_request_id"] == "native-expiring")
        await manager._handle_permission_session_changed(Method.SESSION_CHANGED,
            {"current_session_id":"permission-session"})
        foreground_card = [call.args[1] for call in emitted.await_args_list
            if call.args and call.args[0] == Method.WALLPAPER_CANVAS][-1]
        assert foreground_card["permissionVisible"] is True
        assert foreground_card["permissionRequest"]["id"] == expiring_row.request_id

        remaining_event = {**request_event,
            "payload":{"permissionRequest":{"request_id":"native-remaining",
                "capability":"shell.execute", "action":"execute_command",
                "options":["allow_once", "deny"]}}}
        await manager._handle_provider_event(Method.PROVIDER_EVENT, remaining_event)
        remaining_row = next(permission for permission in
            permissions.list_cooperative_permission_requests(
                "permission-session", provider_run_id=record.run_id, status="pending")
            if permission.metadata["provider_request_id"] == "native-remaining")
        newest_card = [call.args[1] for call in emitted.await_args_list
            if call.args and call.args[0] == Method.WALLPAPER_CANVAS][-1]
        assert newest_card["permissionRequest"]["id"] == remaining_row.request_id
        denied_remaining = await router.route({**action, "action":"deny",
            "permission_request_id":remaining_row.request_id,
            "provider_request_id":"native-remaining"})
        assert denied_remaining["ok"] is True
        resumed_card = [call.args[1] for call in emitted.await_args_list
            if call.args and call.args[0] == Method.WALLPAPER_CANVAS][-1]
        assert resumed_card["permissionVisible"] is True
        assert resumed_card["permissionRequest"]["id"] == expiring_row.request_id
        await manager._handle_provider_event(Method.PROVIDER_EVENT,
            {"provider":"recovery-test", "run_id":record.run_id,
             "type":"permission.expired", "metadata":dict(record.metadata),
             "payload":{"request_id":"native-expiring",
                 "reason":"approval_timeout"}})
        expired = permissions.get_permission_request(expiring_row.request_id)
        assert expired is not None and expired.status == "expired"
        assert expired.metadata["resolution"] == "provider_expired"
        expired_canvas = [call.args[1] for call in emitted.await_args_list
            if call.args and call.args[0] == Method.WALLPAPER_CANVAS][-1]
        assert expired_canvas["permissionVisible"] is False
        assert expired_canvas["permissionRequest"]["id"] == expiring_row.request_id
        assert expired_canvas["permissionRequest"]["status"] == "expired"

        read_path = tmp_path/"read-only-context"
        read_path.mkdir()
        read_child = ingress.loop._create_context("Read only", "recovery-test",
            requirements=replace(requirements, workspace_access="read"),
            workspace=str(read_path))
        ingress.loop.bind_context(read_child.child_id)
        read_result = await handler._handle_send({"text":"不要写入。",
            "turn_id":"read-permission-turn",
            "utterance_id":"read-permission-source",
            "session_id":"permission-session"})
        assert read_result["status"] == "ok"
        await handler._stream_task
        read_receipt = ingress.receipts["read-permission-source"]
        read_record = runtime.get_run(read_receipt["run_id"])
        read_event = {"provider":"recovery-test", "run_id":read_record.run_id,
            "type":"permission.requested", "metadata":dict(read_record.metadata),
            "payload":{"permissionRequest":{"request_id":"read-only-escalation",
                "capability":"shell.execute", "action":"execute_command",
                "options":["allow_once", "deny"]}}}
        await manager._handle_provider_event(Method.PROVIDER_EVENT, read_event)
        denied = permissions.list_cooperative_permission_requests(
            "permission-session", context_id=read_child.child_id,
            provider_run_id=read_record.run_id)
        assert len(denied) == 1
        assert denied[0].status == "denied" and denied[0].options == ["deny"]
        response_run, response = adapter.permission_responses[-1]
        assert response_run == read_record.run_id
        assert response.allow is False and response.automatic is True
        assert response.reason == "cooperative_read_only_context"
        refused = await manager.resolve_permission({"session_id":"permission-session",
            "run_id":read_record.run_id,
            "provider_request_id":"read-only-escalation", "allow":True})
        assert refused == {"ok":False, "error":"cooperative_permission_not_pending"}
    finally:
        adapter.release.set()
        await handler.close()
        await manager.close()
        await runtime.close()
        permissions.close()
        ledger.close()


async def test_accepted_dispatch_keeps_its_target_after_binding_change(host_factory):
    host = host_factory()
    entered, release = asyncio.Event(), asyncio.Event()
    async def reserve(request, run_id, phase):
        if phase == "reserve":
            entered.set()
            await release.wait()
        return {"accepted":True}
    host.runtime.set_start_admission_validator(reserve)
    try:
        pending = asyncio.create_task(host.send("检查目录。", "first"))
        await entered.wait()
        accepted_id = host.loop.bound_context_id
        replacement = host.loop._create_context("另一处工作区", host.adapter.provider_id)
        host.loop.bind_context(replacement.child_id)
        release.set()
        receipt = await pending
        await host.loop.wait()
        assert receipt["state"] == "started" and receipt["child_id"] == accepted_id
        assert host.loop.bound_context_id == replacement.child_id
        assert host.adapter.requests[0].cwd == host.loop.children[accepted_id].workspace
        assert not replacement.run_id
    finally:
        release.set()
        await host.close()


async def test_first_native_address_is_checkpointed_before_execution_and_can_reconcile(host_factory, tmp_path):
    first = host_factory()
    entered, release = asyncio.Event(), asyncio.Event()
    checkpoint = first.runtime._native_session_checkpoint
    async def pause_after_checkpoint(run_id, session):
        await checkpoint(run_id, session)
        entered.set()
        await release.wait()
    first.runtime.set_native_session_checkpoint(pause_after_checkpoint)
    try:
        receipt = await first.send("检查目录。", "first")
        await asyncio.wait_for(entered.wait(), 2)
        child = first.loop.children[receipt["child_id"]]
        _, rows = first.loop._state.load()
        handle = rows[0]["native_session"]
        assert handle is not None and rows[0]["run_id"] == receipt["run_id"]
        assert not (Path(child.workspace)/"context.txt").exists()
        backup(first.ledger, tmp_path/"before-execution.sqlite3")
        release.set()
        await first.loop.wait()
    finally:
        release.set()
        await first.close()
    # Restore the checkpoint made before execution, while the native side has
    # since finished. Its exact address is already durable on the first turn.
    resumed = host_factory(database=tmp_path/"before-execution.sqlite3", allow_allocate=False)
    resumed.adapter.observation = ProviderSubmissionReconciliationResult(state="matched_terminal",
        execution=ProviderNativeExecutionHandle(provider=handle.provider, execution_id="first-native-turn"),
        terminal_result=ProviderRunResult(status="done", result="目录已检查。", session=handle))
    try:
        result = (await resumed.ingress.recover())[child.child_id]
        assert result["promoted"] is True
        assert resumed.adapter.inspections[0].run_id == receipt["run_id"]
        assert resumed.adapter.inspections[0].session == handle
        assert not resumed.adapter.requests
        assert (await resumed.send("现在检查原目录。", "continued"))["state"] == "started"
        await resumed.loop.wait()
        assert resumed.adapter.requests[0].session == handle
        assert resumed.adapter.requests[0].cwd == child.workspace
    finally:
        await resumed.close()
