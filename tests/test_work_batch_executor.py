"""A multi-effect admission reuses the existing Runtime and Work executor."""
import asyncio
from dataclasses import replace

from server.provider_event_ingestion import ProviderEventIngestor
from server.turn_admission import capture_turn_admission
from server.work_control import CurrentTurnSourceSpanV1
from test_work_effect_executor import _host, _payload


async def test_two_accepted_project_effects_use_existing_executor_and_replay(tmp_path):
    source = "在清单项目里做个页面，另外在计时项目里做个页面。"
    clauses = ("在清单项目里做个页面", "另外在计时项目里做个页面。")
    async with _host(tmp_path) as host:
        other_path = tmp_path / "other-project"
        other_path.mkdir()
        other = host.work.create_or_get_project(other_path, name="Other Project")
        admission = capture_turn_admission(utterance_id="batch-executor",
            turn_id="batch-executor", session_id="batch-executor-session",
            transcript=source, input_source="text", chat_epoch=1,
            pending=False, authority_mode="turn_decision")
        assert admission is not None
        host.control.admit(admission, fence_scope="batch-executor")
        payloads = []
        for project_id, clause in zip((host.project.project_id, other.project_id), clauses):
            start = source.index(clause)
            payloads.append(replace(_payload(project_id, host.adapter.provider_id),
                task=clause, title=ProviderEventIngestor.task_title(clause),
                session_id=admission.session_id, utterance_id=admission.utterance_id,
                turn_id=admission.turn_id, source_user_text=source,
                source_context_scope=admission.dialogue_source_scope,
                source_proof=CurrentTurnSourceSpanV1.capture(admission, source,
                    start=start, end=start+len(clause))))
        accepted = host.control.seal_many(admission, tuple(payloads))
        dispatched = await asyncio.gather(*(host.executor.dispatch(effect_id)
            for effect_id in accepted["effect_ids"]))
        results = await asyncio.gather(*(host.executor.finish(item) for item in dispatched))
        assert all(result["status"] == "terminal" for result in results)
        assert host.adapter.calls == 2
        assert len({item.binding["work_item_id"] for item in dispatched}) == 2
        assert len({item.binding["attempt_id"] for item in dispatched}) == 2
        requests = {row["run_id"]:row for row in host.adapter.requests}
        for dispatch, payload in zip(dispatched, payloads):
            row = requests[dispatch.binding["provider_run_id"]]
            assert row["request"].task == payload.task
            assert row["request"].metadata["source_user_text"] == source
            assert row["attempt_run_id"] == dispatch.binding["provider_run_id"]
            assert row["lease_status"] == "active"
            work = host.work.get_work_item(dispatch.binding["work_item_id"])
            assert work.project_id == payload.project_id
            assert work.origin_effect_id == dispatch.effect_id
        assert (host.workspace / "accepted-c2.txt").exists()
        assert (other_path / "accepted-c2.txt").exists()
        replayed = host.control.seal_many(admission, tuple(payloads))
        assert replayed["replayed"] and replayed["effect_ids"] == accepted["effect_ids"]
        replayed_results = await asyncio.gather(*(host.executor.execute(effect_id)
            for effect_id in accepted["effect_ids"]))
        assert all(result["replayed"] for result in replayed_results)
        assert host.adapter.calls == 2
        assert len(host.work.list_work_items()) == 2
        assert sum(len(host.work.list_attempts(item.work_item_id))
            for item in host.work.list_work_items()) == 2
