"""ProviderRuntime-to-WorkLedger integration smoke."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderEvent, ProviderRunRequest, ProviderRunResult
from agent_host.work_ledger_store import WorkLedgerStore
from server.handlers.provider_handler import ProviderHandler
from server.work_ledger_coordinator import WorkLedgerCoordinator


class FakeWriteAdapter:
    provider_id = "fake-write"

    async def run(self, request, run_id, emit):
        output = Path(str(request.cwd)) / "generated.txt"
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="tool.call",
                payload={"tool": "Write", "raw": {"input": {"file_path": str(output)}}},
            )
        )
        output.write_text("generated\n", encoding="utf-8")
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="tool.result",
                payload={"tool": "Write", "ok": True},
            )
        )
        return ProviderRunResult(status="done", result=f"Created {output}")

    async def cancel(self, run_id):
        return None


class CapturePromptAdapter:
    provider_id = "capture-prompt"

    def __init__(self) -> None:
        self.task = ""

    async def run(self, request, run_id, emit):
        self.task = request.task
        return ProviderRunResult(status="done", result="captured")

    async def cancel(self, run_id):
        return None


class BlockingResumeAdapter:
    provider_id = "blocking-resume"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.task = ""

    async def run(self, request, run_id, emit):
        self.task = request.task
        self.started.set()
        await self.release.wait()
        return ProviderRunResult(status="done", result="resumed")

    async def cancel(self, run_id):
        self.release.set()


class UnconfirmedCancelAdapter:
    provider_id = "unconfirmed-cancel"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, request, run_id, emit):
        self.started.set()
        await self.release.wait()
        return ProviderRunResult(status="done", result="finished after cancellation was not confirmed")

    async def cancel(self, run_id):
        return {
            "requested": True,
            "confirmed": False,
            "status": "running",
            "reason": "cancel_unconfirmed",
        }


def test_runtime_keeps_display_task_separate_from_provider_policy_prompt() -> None:
    async def run() -> None:
        runtime = ProviderRuntime()
        adapter = CapturePromptAdapter()
        runtime.register(adapter)

        def prepare(request: ProviderRunRequest) -> ProviderRunRequest:
            request.metadata["display_task"] = "Create chess_game.py on Desktop"
            request.task = "internal staged-export policy prompt"
            return request

        runtime.set_request_preparer(prepare)
        record = await runtime.start(
            ProviderRunRequest(provider="capture-prompt", task="original")
        )
        assert record.task_handle is not None
        await record.task_handle
        assert record.task == "Create chess_game.py on Desktop"
        assert adapter.task == "internal staged-export policy prompt"
        assert record.events[0]["payload"]["task"] == "Create chess_game.py on Desktop"

    asyncio.run(run())


def test_runtime_hook_binds_and_releases_a_durable_attempt() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_control_runtime_") as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            store = WorkLedgerStore(root / "ledger.sqlite3")
            coordinator = WorkLedgerCoordinator(store)
            coordinator.configure()
            runtime = ProviderRuntime()
            runtime.register(FakeWriteAdapter())
            runtime.set_request_preparer(coordinator.prepare_request)
            record = await runtime.start(
                ProviderRunRequest(
                    provider="fake-write",
                    task="Generate a text artifact",
                    cwd=str(project),
                    mode="agent",
                )
            )
            assert record.task_handle is not None
            await record.task_handle

            binding = record.metadata["work"]
            attempt = store.get_attempt(str(binding["attempt_id"]))
            assert attempt is not None
            assert attempt.provider_run_id == record.run_id
            assert attempt.execution_status == "succeeded"
            assert store.get_writer_lease(attempt.attempt_id).status == "released"  # type: ignore[union-attr]
            artifacts = store.list_artifacts(attempt.work_item_id, attempt_id=attempt.attempt_id)
            assert any(artifact.kind == "tool.output" and artifact.title == "generated.txt" for artifact in artifacts)
            assessment = store.latest_completion(attempt.work_item_id)
            assert assessment is not None and assessment.work_item_state == "review_ready"
            assert assessment.work_item_state != "accepted"
            runtime.set_request_preparer(None)
            coordinator.close()

    asyncio.run(run())


def test_runtime_resume_is_orphan_only_single_flight_and_adopts_missing_task() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="provider_runtime_resume_") as temp:
            runtime = ProviderRuntime()
            adapter = BlockingResumeAdapter()
            runtime.register(adapter)
            runtime.add_orphaned_run(
                provider=adapter.provider_id,
                run={
                    "run_id": "taskless-orphan",
                    "cwd": temp,
                    "mode": "agent",
                },
            )
            record = await runtime.resume(
                "taskless-orphan",
                ProviderRunRequest(
                    provider=adapter.provider_id,
                    task="Durable ledger task",
                    cwd=temp,
                    mode="agent",
                ),
            )
            assert record.task == "Durable ledger task"
            assert record.metadata["resume_task_authoritative"] is True
            assert record.status == "queued"

            try:
                await runtime.resume(
                        "taskless-orphan",
                    ProviderRunRequest(
                        provider=adapter.provider_id,
                        task="Durable ledger task",
                        cwd=temp,
                        mode="agent",
                    ),
                )
            except ValueError as exc:
                assert "not orphaned" in str(exc) or "active task" in str(exc)
            else:
                raise AssertionError("the same provider run must not resume twice")

            await asyncio.wait_for(adapter.started.wait(), timeout=2.0)
            assert adapter.task == "Durable ledger task"
            adapter.release.set()
            assert record.task_handle is not None
            await record.task_handle
            assert record.status == "done"

            runtime.add_orphaned_run(
                provider=adapter.provider_id,
                run={
                    "run_id": "authoritative-orphan",
                    "task": "Original task",
                    "cwd": temp,
                    "mode": "agent",
                },
            )
            try:
                await runtime.resume(
                    "authoritative-orphan",
                    ProviderRunRequest(
                        provider=adapter.provider_id,
                        task="Changed task",
                        cwd=temp,
                        mode="agent",
                    ),
                )
            except ValueError as exc:
                assert "original task" in str(exc)
            else:
                raise AssertionError("Resume must not replace an authoritative task")

    asyncio.run(run())


def test_runtime_keeps_unconfirmed_cancel_running() -> None:
    async def run() -> None:
        runtime = ProviderRuntime()
        adapter = UnconfirmedCancelAdapter()
        runtime.register(adapter)
        record = await runtime.start(
            ProviderRunRequest(provider=adapter.provider_id, task="Long task")
        )
        await asyncio.wait_for(adapter.started.wait(), timeout=2.0)

        outcome = await runtime.cancel(record.run_id)
        assert outcome["cancelled"] is False
        assert outcome["reason"] == "cancel_unconfirmed"
        assert record.status == "running"
        assert record.task_handle is not None and not record.task_handle.done()
        assert record.metadata["liveness"]["state"] == "cancel_pending"
        assert record.events[-1]["type"] == "run.status"
        assert record.events[-1]["payload"]["liveness"] == "cancel_pending"

        adapter.release.set()
        await record.task_handle
        assert record.status == "done"

    asyncio.run(run())


def test_generic_provider_run_cannot_reach_resume() -> None:
    async def run() -> None:
        handler = object.__new__(ProviderHandler)
        try:
            await handler._run(  # type: ignore[attr-defined]
                {
                    "provider": "fake",
                    "task": "Changed task",
                    "cwd": "C:/different-workspace",
                    "resume": "old-run",
                },
                allow_resume=False,
            )
        except ValueError as exc:
            assert "use work.resume" in str(exc)
        else:
            raise AssertionError("generic provider.run must not Resume")

    asyncio.run(run())


def _main() -> None:
    test_runtime_keeps_display_task_separate_from_provider_policy_prompt()
    test_runtime_hook_binds_and_releases_a_durable_attempt()
    test_runtime_resume_is_orphan_only_single_flight_and_adopts_missing_task()
    test_runtime_keeps_unconfirmed_cancel_running()
    test_generic_provider_run_cannot_reach_resume()
    print("ok: provider runtime is durably bound to WorkItem lifecycle")


if __name__ == "__main__":
    _main()
