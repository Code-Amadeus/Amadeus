"""A task-addressed continuation must retain the selected task's stop identity."""
import asyncio
import json
from unittest.mock import AsyncMock
from dataclasses import replace

import pytest

from server.attention_request import AttentionRequestCoordinator
from server.cooperative_chat_ingress import CooperativeChatManager
from test_cooperative_context_recovery import host_factory as host_factory
from server.control_ledger import ControlLedgerConflict, ControlLedgerStore
from server.cooperative_provider_effect import CooperativeProviderEffectLedger
from test_cooperative_provider_effect import admitted, intent


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("finish_during_lookup", [False, True])
async def test_stop_original_task_stops_its_explicitly_resumed_execution(host_factory, restart, finish_during_lookup):
    host = host_factory()
    manager = object.__new__(CooperativeChatManager)
    manager.ledger, manager.runtime, manager.work_executor = host.ledger, host.runtime, None
    manager.attention = AttentionRequestCoordinator()
    host.ingress.work_request = manager.handle_work_action
    host.loop.task_contexts = lambda:manager.task_context_candidates(host.ingress)
    original_text = "帮我看看这台电脑的 Python 是哪个版本。"
    resume_text = "就按你说的继续看看吧。"
    stop_text = "Python 那个先别查了。"
    next_text = "接着帮我看看相关的环境变量吧。"
    original_token = ""
    selected_stop_tokens = []
    finishing = False

    async def query(messages, **_kwargs):
        nonlocal original_token, finishing
        try:
            frame = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            # Model meaning is fixed: the stop refers to the original task,
            # rather than guessing that the latest execution is its successor.
            assert original_token and original_token in messages[-1]["content"]
            if messages[-1]["content"].startswith("[Current user message]\n" + resume_text + "\n\n"):
                return json.dumps({"references":[original_token]})
            if finishing:
                host.adapter.release.set()
                await host.loop.wait()
                host.adapter.release.clear()
                finishing = False
                return json.dumps({"references":[original_token]})
            assert stop_text in messages[-1]["content"]
            selected_stop_tokens.append(original_token)
            return json.dumps({"references":[original_token]})
        if frame["source_kind"] != "user":
            return "好。"
        text = frame["current"]["text"]
        if text == original_text:
            action = {"op":"delegate", "provider":host.adapter.provider_id}
        elif text == resume_text:
            task, = [row["task"] for row in frame["retained_contexts"] if "task" in row]
            assert task["goal"] == original_text
            original_token = task["token"]
            action = {"op":"send_to", "target":original_token}
        elif text == next_text:
            finishing = True
            action = {"op":"send_to", "target":"Python 版本检查"}
        else:
            assert text == stop_text
            action = {"op":"interrupt", "target":original_token}
        return json.dumps({"action":action, "say":"好。"})

    manager.query = host.loop.query = query
    try:
        first = await host.send(original_text, "python-question")
        await host.loop.wait()
        assert host.runtime.get_run(first["run_id"]).status == "done"
        context = host.loop.get_context(first["child_id"])
        original_native, original_workspace = context.native_session, context.workspace
        original_binding = (host.loop.bound_context_id, host.loop._binding.token)
        if restart:
            await host.close()
            host = host_factory(allow_allocate=False)
            manager.ledger, manager.runtime = host.ledger, host.runtime
            host.ingress.work_request = manager.handle_work_action
            host.loop.task_contexts = lambda:manager.task_context_candidates(host.ingress)
            manager.query = host.loop.query = query
        host.adapter.release.clear()
        host.adapter.started.clear()
        host.runtime.cancel = AsyncMock(wraps=host.runtime.cancel)

        resumed = await host.send(resume_text, "python-follow-up")
        await asyncio.wait_for(host.adapter.started.wait(), 2)
        assert resumed["state"] == "started" and resumed["run_id"] != first["run_id"]
        assert resumed["child_id"] == first["child_id"]
        assert host.adapter.requests[-1].session == original_native
        assert host.adapter.requests[-1].cwd == original_workspace
        assert host.adapter.requests[-1].task == resume_text
        assert len(host.loop.context_catalog()) == 1
        effect = host.ledger.get_effect(host.loop.get_context(resumed["child_id"]).run_effect_id)
        assert json.loads(effect["payload_json"])["continuation_effect_id"] == original_token.removeprefix("execution:")
        assert host.runtime.get_run(resumed["run_id"]).status == "running"
        if finish_during_lookup:
            previous_run = resumed["run_id"]
            resumed = await host.send(next_text, "python-another-follow-up")
            assert resumed["state"] == "started" and resumed["run_id"] != previous_run
            assert host.runtime.get_run(previous_run).status == "done"
            assert host.adapter.requests[-1].session == original_native
            assert host.adapter.requests[-1].cwd == original_workspace

        stopped = await host.send(stop_text, "stop-python")
        # Continuation still reuses the known token. Cancellation resolves the
        # actual stop request, since the role may have named a different task.
        assert selected_stop_tokens == [original_token]
        assert (host.loop.bound_context_id, host.loop._binding.token) == original_binding
        assert len(host.adapter.requests) == (1 if restart else 2) + int(finish_during_lookup)
        assert stopped["state"] == "stopped" and stopped.get("run_id") == resumed["run_id"], (
            "The original task was explicitly selected for continuation, but its stop target "
            f"did not follow that accepted continuation: original={first['run_id']}, "
            f"resumed={resumed['run_id']}, stop_receipt={stopped}, "
            f"cancelled_runs={[call.args[0] for call in host.runtime.cancel.await_args_list]}"
        )
        assert [call.args[0] for call in host.runtime.cancel.await_args_list] == [resumed["run_id"]]
    finally:
        host.adapter.release.set()
        await host.close()


@pytest.mark.parametrize("mismatch", ["context", "provider", "session", "source", "missing"])
def test_continuation_link_requires_matching_admitted_provider_owner(tmp_path, mismatch):
    store = ControlLedgerStore(tmp_path / "lineage.sqlite3")
    try:
        effects = CooperativeProviderEffectLedger(store)
        original = admitted(store, source="original", turn="original-turn", epoch=1)
        old_intent = intent("start", source="original", turn="original-turn")
        accepted = effects.accept(original, old_intent)
        original_id = accepted["effect"]["effect_id"]
        assert "continuation_effect_id" not in old_intent.to_payload()
        session = "other-session" if mismatch == "session" else "session-a"
        followup = admitted(store, source="followup", turn="followup-turn", epoch=2, session=session)
        linked = replace(intent("start", source="followup", turn="followup-turn"),
            session_id=session, continuation_effect_id=original_id)
        if mismatch == "context":
            linked = replace(linked, context_id="other-context")
        elif mismatch == "provider":
            linked = replace(linked, provider="other-provider")
        elif mismatch == "missing":
            linked = replace(linked, continuation_effect_id="missing-effect")
        elif mismatch == "source":
            with store._lock:
                store._db.execute("UPDATE control_admissions SET utterance_id=? WHERE root_id=?",
                    ("different-source", original.root_id))
        with pytest.raises(ControlLedgerConflict, match="continuation"):
            effects.accept(followup, linked)
        assert store.get_admission(followup.root_id)["plan_id"] is None
    finally:
        store.close()


def test_explicit_lineage_and_legacy_payload_bytes_survive_reopen(tmp_path):
    path = tmp_path / "lineage.sqlite3"
    store = ControlLedgerStore(path)
    effects = CooperativeProviderEffectLedger(store)
    snapshots, effect_ids = {}, []
    try:
        for index in range(3):
            source, turn = f"source-{index}", f"turn-{index}"
            admission = admitted(store, source=source, turn=turn, epoch=index + 1)
            operation = replace(intent("start", source=source, turn=turn),
                continuation_effect_id=effect_ids[-1] if effect_ids else "")
            accepted = effects.accept(admission, operation)
            effect_id = accepted["effect"]["effect_id"]
            effect_ids.append(effect_id)
            snapshots[effect_id] = store.get_effect(effect_id)["payload_json"]
        assert "continuation_effect_id" not in json.loads(snapshots[effect_ids[0]])
    finally:
        store.close()
    reopened = ControlLedgerStore(path)
    try:
        restored = CooperativeProviderEffectLedger(reopened)
        assert restored.task_root(effect_ids[-1], session_id="session-a",
            context_id="context-a", provider="codex") == effect_ids[0]
        assert restored.accept(admission, operation)["replayed"] is True
        assert {key:reopened.get_effect(key)["payload_json"] for key in effect_ids} == snapshots
    finally:
        reopened.close()


async def test_other_active_task_in_same_context_cannot_receive_addressed_input(host_factory):
    host = host_factory()
    source_a, source_b = "帮我看看 Python 的版本。", "再看看这个目录里有什么。"
    try:
        first = await host.send(source_a, "python")
        await host.loop.wait()
        host.adapter.release.clear()
        host.adapter.started.clear()
        second = await host.send(source_b, "directory")
        await asyncio.wait_for(host.adapter.started.wait(), 2)
        assert first["child_id"] == second["child_id"]
        manager = object.__new__(CooperativeChatManager)
        manager.ledger, manager.runtime, manager.work_executor = host.ledger, host.runtime, None
        manager.attention = AttentionRequestCoordinator()
        host.ingress.work_request = manager.handle_work_action
        host.loop.task_contexts = lambda:manager.task_context_candidates(host.ingress)
        candidates, complete, _, targets = manager.task_context_candidates(host.ingress)
        assert complete and len(candidates) == 2
        first_token = next(candidate.token for candidate in candidates if candidate.delegated_goal == source_a)
        current_token = next(candidate.token for candidate in candidates if targets[candidate.token]["run_id"] == second["run_id"])

        async def query(messages, **_kwargs):
            try:
                frame = json.loads(messages[-1]["content"])
            except json.JSONDecodeError:
                assert "Python 那个再帮我看看吧。" in messages[-1]["content"]
                return json.dumps({"references":[first_token]})
            if frame["source_kind"] != "user":
                return "现在还不能接着查。"
            # The preview follows actual current execution, not the older task.
            assert frame["context"]["task"]["token"] == current_token
            return json.dumps({"action":{"op":"send_to", "target":first_token}, "say":"接着看 Python。"})

        manager.query = host.loop.query = query
        host.runtime.append_input = AsyncMock()
        result = await host.send("Python 那个再帮我看看吧。", "resume-python")
        assert result["state"] == "rejected" and result["reason"] == "addressed_task_busy"
        host.runtime.append_input.assert_not_awaited()
        assert host.runtime.get_run(second["run_id"]).status == "running"
        assert len(host.adapter.requests) == 2
        with host.ledger._lock:
            rows = host.ledger._db.execute("SELECT payload_json FROM control_effect_outbox WHERE kind='provider'").fetchall()
        assert [json.loads(row[0])["operation"] for row in rows] == ["start", "start"]
    finally:
        host.adapter.release.set()
        await host.close()
