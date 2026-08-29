from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from agent_host.provider_activity_journal import ProviderActivityJournal
from server.event_bus import bus
from server.handlers.provider_activity_handler import ProviderActivityHandler
from server.protocol import Method


def _event(
    run_id: str,
    sequence: int,
    *,
    session_id: str = "session-one",
    turn_id: str = "turn-one",
    event_type: str = "tool.call",
) -> dict:
    return {
        "provider": "provider-test",
        "run_id": run_id,
        "type": event_type,
        "payload": {
            "name": "shell",
            "item_id": f"item-{sequence}",
            "input": {"command": f"python verify.py token=secret-{sequence}"},
        },
        "metadata": {
            "session_id": session_id,
            "turn_id": turn_id,
            "work": {
                "work_item_id": f"work-{run_id}",
                "attempt_id": f"attempt-{run_id}",
            },
        },
        "task_id": f"work-{run_id}",
        "attempt_id": f"attempt-{run_id}",
        "sequence": sequence,
        "observed_at": 1000.0 + sequence,
    }


def _result(run_id: str, *, session_id: str = "session-one") -> dict:
    return {
        "provider": "provider-test",
        "run_id": run_id,
        "task": "Verify the project",
        "cwd": "C:/project",
        "status": "done",
        "created_at": 1000.0,
        "updated_at": 1010.0,
        "result": "Verified password=do-not-persist",
        "metadata": {
            "session_id": session_id,
            "turn_id": "turn-one",
            "work": {
                "work_item_id": f"work-{run_id}",
                "attempt_id": f"attempt-{run_id}",
            },
        },
    }


def test_journal_rehydrates_only_observable_host_bound_activity() -> None:
    with tempfile.TemporaryDirectory(prefix="amadeus-provider-activity-") as temp_dir:
        path = Path(temp_dir) / "activity.jsonl"
        journal = ProviderActivityJournal(path)

        hidden = _event("run-one", 1, event_type="assistant.delta")
        assert journal.record_event(hidden) is False
        missing_origin = _event("run-one", 1, turn_id="")
        assert journal.record_event(missing_origin) is False
        assert journal.record_event(_event("run-one", 1)) is True
        assert journal.record_result(_result("run-one")) is True
        journal.close()

        persisted = path.read_text(encoding="utf-8")
        assert "do-not-persist" not in persisted
        assert "secret-1" not in persisted
        assert "assistant.delta" not in persisted

        restored = ProviderActivityJournal(path)
        runs = restored.list_runs("session-one")
        assert len(runs) == 1
        assert runs[0]["run_id"] == "run-one"
        assert runs[0]["status"] == "done"
        assert runs[0]["task_id"] == "work-run-one"
        assert len(runs[0]["events"]) == 1
        command = runs[0]["events"][0]["payload"]["input"]["command"]
        assert command.endswith("token=•••")
        assert restored.list_runs("another-session") == []


def test_journal_compaction_bounds_runs_and_events() -> None:
    with tempfile.TemporaryDirectory(prefix="amadeus-provider-activity-bounds-") as temp_dir:
        path = Path(temp_dir) / "activity.jsonl"
        journal = ProviderActivityJournal(path, max_runs=2, max_events_per_run=2)
        for run_index in range(3):
            run_id = f"run-{run_index}"
            for sequence in range(1, 4):
                event = _event(run_id, sequence)
                event["observed_at"] += run_index * 100
                journal.record_event(event)
        journal.close()

        restored = ProviderActivityJournal(path, max_runs=2, max_events_per_run=2)
        runs = restored.list_runs("session-one")
        assert [run["run_id"] for run in runs] == ["run-1", "run-2"]
        assert all(len(run["events"]) == 2 for run in runs)
        assert all(
            [event["sequence"] for event in run["events"]] == [2, 3]
            for run in runs
        )


def test_read_api_returns_the_persisted_session_projection() -> None:
    async def scenario(path: Path) -> None:
        journal = ProviderActivityJournal(path)
        handler = ProviderActivityHandler(journal)
        try:
            await bus.emit(Method.PROVIDER_EVENT, _event("run-api", 1))
            response = await handler.handle(
                Method.PROVIDER_ACTIVITY_LIST,
                {"session_id": "session-one"},
            )
            assert response is not None
            assert [run["run_id"] for run in response["runs"]] == ["run-api"]
            assert await handler.handle(Method.PROVIDER_ACTIVITY_LIST, {}) == {"runs": []}
        finally:
            await handler.close()

    with tempfile.TemporaryDirectory(prefix="amadeus-provider-activity-api-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir) / "activity.jsonl"))


def _main() -> None:
    test_journal_rehydrates_only_observable_host_bound_activity()
    test_journal_compaction_bounds_runs_and_events()
    test_read_api_returns_the_persisted_session_projection()
    print("ok: Provider activity is bounded, local, restart-safe, and role-free")


if __name__ == "__main__":
    _main()
