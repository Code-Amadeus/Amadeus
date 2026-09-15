from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_host.provider_contract import (
    ProviderCapabilities,
    ProviderManifest,
    ProviderRequirements,
)
from agent_host.provider_runtime import (
    ProviderRuntime,
    ProviderStartAdmissionRejected,
)
from agent_host.provider_types import (
    PreparedProviderRun,
    ProviderRunIntakeAuthority,
    ProviderRunIntakeReceipt,
    ProviderRunRequest,
    ProviderRunResult,
    ProviderEvent,
)
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from server.control_ledger import ControlLedgerConflict, ControlLedgerStore
from server.turn_admission import capture_turn_admission
from server.work_control import (
    CurrentTurnSourceSpanV1,
    WorkControl,
    WorkEffectPayloadV3,
)
from server.work_effect_executor import WorkEffectExecutor
from server.work_ledger_coordinator import WorkLedgerCoordinator


_HOST_AUTHORITY_TEST_KEYS = {
    "effect_id",
    "origin_effect_id",
    "claim_token",
    "intake_authority",
    "intake_receipt",
    "source_proof",
}


class _RuntimeAdapter:
    def __init__(
        self,
        provider_id: str,
        store: WorkLedgerStore,
        *,
        result_status: str = "done",
        block: bool = False,
    ) -> None:
        self.provider_id = provider_id
        self.store = store
        self.result_status = result_status
        self.manifest = ProviderManifest(
            provider_id=provider_id,
            display_name="Generic C2 provider",
            capabilities=ProviderCapabilities(
                task_kinds=("workspace_mutation",),
                workspace_access="write",
                workspace_ownership="caller",
                durability="process",
            ),
        )
        self.calls = 0
        self.requests: list[dict] = []
        self.spoof_authority_metadata = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not block:
            self.release.set()

    async def run(self, request, run_id, _emit):
        self.calls += 1
        binding = dict(request.metadata["work"])
        attempt = self.store.get_attempt(binding["attempt_id"])
        lease = self.store.get_writer_lease(binding["attempt_id"])
        self.requests.append(
            {
                "run_id": run_id,
                "request": request,
                "attempt_run_id": attempt.provider_run_id if attempt else "",
                "lease_status": lease.status if lease else "",
                "metadata": dict(request.metadata),
            }
        )
        self.started.set()
        if self.spoof_authority_metadata:
            await _emit(
                ProviderEvent(
                    provider=self.provider_id,
                    run_id=run_id,
                    type="assistant.update",
                    payload={"text": "provider-authored update"},
                    metadata={key: "provider-spoof" for key in _HOST_AUTHORITY_TEST_KEYS},
                )
            )
        await self.release.wait()
        if self.result_status == "done":
            (Path(str(request.cwd)) / "accepted-c2.txt").write_text(
                "one accepted effect\n",
                encoding="utf-8",
            )
            result_metadata = {
                "source_user_text": "provider spoof",
                "source_user_context": "provider spoof",
                "source_utterance_id": "provider-spoof",
                "payload_continuity": "confirmed_prior_request",
            }
            if self.spoof_authority_metadata:
                result_metadata.update(
                    {key: "provider-spoof" for key in _HOST_AUTHORITY_TEST_KEYS}
                )
            return ProviderRunResult(
                status="done",
                result="completed",
                metadata=result_metadata,
            )
        if self.result_status == "cancelled":
            return ProviderRunResult(status="cancelled", error="cancelled")
        if self.result_status == "orphaned":
            return ProviderRunResult(status="orphaned", error="outcome unknown")
        return ProviderRunResult(status="error", error="provider failed")

    async def cancel(self, _run_id):
        self.release.set()
        return {"confirmed": True, "cancelled": True}


def _admission(
    *,
    suffix: str = "one",
    epoch: int = 1,
    mode: str = "turn_decision",
    text: str = "Build the accepted C2 artifact",
):
    admission = capture_turn_admission(
        utterance_id="utterance-" + suffix,
        turn_id="turn-" + suffix,
        session_id="session-c2",
        transcript=text,
        input_source="voice",
        chat_epoch=epoch,
        pending=False,
        authority_mode=mode,
    )
    assert admission is not None
    return admission


def _payload(
    project_id: str,
    provider: str,
    *,
    suffix: str = "one",
    source: str = "Build the accepted C2 artifact",
    task: str = "Build the accepted C2 artifact",
):
    admission = _admission(suffix=suffix, text=source)
    start = source.index(task)
    return WorkEffectPayloadV3(
        provider=provider,
        task=task,
        title=task,
        project_id=project_id,
        session_id="session-c2",
        utterance_id="utterance-" + suffix,
        turn_id="turn-" + suffix,
        source_user_text=source,
        source_user_context=(
            'User: "Please preserve the blue background."\n'
            "Assistant: I will keep that constraint in the assigned task."
        ),
        source_context_scope="chat:session-c2",
        source_proof=CurrentTurnSourceSpanV1.capture(
            admission,
            source,
            start=start,
            end=start + len(task),
        ),
        requirements=ProviderRequirements(
            task_kind="workspace_mutation",
            workspace_access="write",
            workspace_ownership="caller",
            ownership="managed",
        ),
    )


@asynccontextmanager
async def _host(
    tmp_path: Path,
    *,
    provider: str = "generic-alpha",
    result_status: str = "done",
    block: bool = False,
    clock=None,
    source: str = "Build the accepted C2 artifact",
    task: str = "Build the accepted C2 artifact",
):
    database = tmp_path / "shared.sqlite3"
    workspace = tmp_path / "project"
    workspace.mkdir(parents=True)
    work = WorkLedgerStore(database, clock=clock) if clock else WorkLedgerStore(database)
    project = work.create_or_get_project(workspace)
    control_store = (
        ControlLedgerStore(database, clock=clock)
        if clock
        else ControlLedgerStore(database)
    )
    control = WorkControl(control_store, work)
    admission = _admission(text=source)
    control.admit(admission, fence_scope="foreground-chat")
    effect_id = control.seal(
        admission,
        _payload(project.project_id, provider, source=source, task=task),
    )["effect_id"]
    coordinator = WorkLedgerCoordinator(work, work_control=control)
    runtime = ProviderRuntime()
    adapter = _RuntimeAdapter(
        provider,
        work,
        result_status=result_status,
        block=block,
    )
    runtime.register(adapter)
    runtime.set_request_preparer(coordinator.prepare_request)
    coordinator.configure()
    executor = WorkEffectExecutor(control, runtime, coordinator)
    with (
        patch("config.settings.WORK_WORKTREE_ISOLATION", False),
        patch("server.work_ledger_coordinator.cwd_in_project_registry", return_value=True),
    ):
        try:
            yield SimpleNamespace(
                database=database,
                workspace=workspace,
                project=project,
                work=work,
                control_store=control_store,
                control=control,
                coordinator=coordinator,
                runtime=runtime,
                adapter=adapter,
                executor=executor,
                effect_id=effect_id,
            )
        finally:
            adapter.release.set()
            await asyncio.gather(
                *(
                    record.task_handle
                    for record in runtime._runs.values()
                    if record.task_handle is not None
                ),
                return_exceptions=True,
            )
            await runtime.close()
            await coordinator.drain_provider_facts()
            runtime.set_request_preparer(None)
            coordinator.close()
            control_store.close()
            work.close()


@pytest.mark.parametrize("provider", ["generic-alpha", "remote-beta"])
async def test_accepted_effect_runs_once_with_runtime_identity_and_terminal_receipt(
    tmp_path,
    provider,
):
    async with _host(tmp_path, provider=provider) as host:
        result = await host.executor.execute(host.effect_id)
        assert result["status"] == "terminal" and not result["replayed"]
        assert host.adapter.calls == 1
        assert len(host.runtime.list_runs()) == 1
        captured = host.adapter.requests[0]
        binding = result["binding"]
        assert captured["run_id"] == captured["attempt_run_id"] == binding["provider_run_id"]
        assert captured["lease_status"] == "active"
        assert host.work.get_writer_lease(binding["attempt_id"]).status == "released"
        assert host.work.get_attempt(binding["attempt_id"]).execution_status == "succeeded"
        terminal = host.work.get_attempt(binding["attempt_id"]).metadata[
            "provider_terminal_pipeline"
        ]
        assert terminal["state"] == "completed"
        assert len(host.work.list_completions(binding["work_item_id"])) == 1
        receipt = host.control_store.get_receipt(host.effect_id)
        assert receipt["outcome"] == "succeeded"
        assert receipt["external_id"] == binding["provider_run_id"]
        assert receipt["details"]["work_terminal_receipt_sha256"] == terminal[
            "receipt_sha256"
        ]
        assert (host.workspace / "accepted-c2.txt").read_text(encoding="utf-8") == (
            "one accepted effect\n"
        )
        assert captured["metadata"]["source_user_text"] == (
            "Build the accepted C2 artifact"
        )
        assert "blue background" in captured["metadata"]["source_user_context"]
        assert captured["metadata"]["source_context_mode"] == "snapshot"
        runtime_record = host.runtime.get_run(binding["provider_run_id"])
        assert runtime_record is not None
        assert runtime_record.metadata["source_user_text"] == (
            "Build the accepted C2 artifact"
        )
        assert "blue background" in runtime_record.metadata["source_user_context"]
        assert runtime_record.metadata["source_utterance_id"] == "utterance-one"
        assert runtime_record.metadata["payload_continuity"] == "current_turn"
        forbidden = {
            key
            for key in captured["metadata"]
            if any(word in key for word in ("effect", "claim", "intake_authority"))
        }
        assert forbidden == set()
        assert not hasattr(captured["request"], "intake_authority")


@pytest.mark.parametrize("provider", ["generic-alpha", "remote-beta"])
async def test_unicode_subspan_runs_through_existing_runtime_and_receipt_path(
    tmp_path,
    provider,
):
    source = "先说明约束🙂，然后 Build the selected board artifact。"
    task = "Build the selected board artifact。"
    async with _host(
        tmp_path,
        provider=provider,
        source=source,
        task=task,
    ) as host:
        result = await host.executor.execute(host.effect_id)
        assert result["status"] == "terminal"
        assert host.adapter.calls == 1
        captured = host.adapter.requests[0]
        assert captured["request"].task == task
        assert captured["metadata"]["source_user_text"] == source
        assert host.control_store.get_receipt(host.effect_id)["outcome"] == "succeeded"
        binding = result["binding"]
        item = host.work.get_work_item(binding["work_item_id"])
        assert item is not None and item.goal == task


async def test_accepted_start_detaches_caller_request_before_durable_binding(tmp_path):
    async with _host(tmp_path) as host:
        request = host.control.provider_request(host.effect_id)
        expected_task = request.task
        expected_source = request.metadata["source_user_text"]
        expected_project = request.metadata["work"]["project_id"]
        created = asyncio.Event()
        release = asyncio.Event()

        async def validate(_snapshot, _run_id, phase):
            if phase == "created":
                created.set()
                await release.wait()
            return True

        host.runtime.set_start_admission_validator(validate)
        starter = asyncio.create_task(
            host.runtime.start_accepted(
                request,
                ProviderRunIntakeAuthority(host.effect_id),
            )
        )
        await asyncio.wait_for(created.wait(), timeout=2.0)
        request.task = "DIFFERENT TASK AFTER BIND"
        request.metadata["source_user_text"] = "different source after bind"
        request.metadata["work"]["project_id"] = "different-project-after-bind"
        release.set()
        record = await starter
        assert record.task_handle is not None
        await record.task_handle

        assert host.adapter.calls == 1
        captured = host.adapter.requests[0]
        assert captured["request"].task == expected_task
        assert captured["metadata"]["source_user_text"] == expected_source
        assert captured["metadata"]["work"]["project_id"] == expected_project
        binding = host.control.binding(host.effect_id)
        assert binding is not None
        item = host.work.get_work_item(binding["work_item_id"])
        assert item is not None and item.goal == expected_task


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("display_task", "unaccepted prior task"),
        ("external_export", {"target": "desktop", "filename": "unrequested.txt"}),
        ("host_outcome_requirement", {"facet": "auip.application"}),
    ],
)
async def test_accepted_start_rejects_unsealed_pre_intake_controls(
    tmp_path,
    key,
    value,
):
    async with _host(tmp_path) as host:
        request = host.control.provider_request(host.effect_id)
        request.metadata[key] = value
        with pytest.raises(WorkLedgerConflict, match="does not match"):
            await host.runtime.start_accepted(
                request,
                ProviderRunIntakeAuthority(host.effect_id),
            )
        assert host.control.binding(host.effect_id) is None
        assert host.control_store.get_effect(host.effect_id)["state"] == "pending"
        assert host.adapter.calls == 0
        assert host.work.list_work_items() == []
        assert not (host.workspace / "proposed_exports").exists()


async def test_accepted_start_requires_typed_authority_and_compatible_preparer(tmp_path):
    store = WorkLedgerStore(tmp_path / "work.sqlite3")
    runtime = ProviderRuntime()
    adapter = _RuntimeAdapter("generic-alpha", store)
    runtime.register(adapter)
    request = ProviderRunRequest(
        provider="generic-alpha",
        task="Build",
        cwd=str(tmp_path),
        requirements=ProviderRequirements(
            task_kind="workspace_mutation",
            workspace_access="write",
            workspace_ownership="caller",
        ),
    )
    try:
        with pytest.raises(TypeError, match="typed intake authority"):
            await runtime.start_accepted(request, object())  # type: ignore[arg-type]
        with pytest.raises(
            ProviderStartAdmissionRejected,
            match="accepted_effect_intake_owner_unavailable",
        ):
            await runtime.start_accepted(
                request,
                ProviderRunIntakeAuthority("effect-one"),
            )
        runtime.set_request_preparer(
            lambda prepared, _run_id, _authority: prepared
        )
        with pytest.raises(
            ProviderStartAdmissionRejected,
            match="accepted_effect_intake_receipt_missing",
        ):
            await runtime.start_accepted(
                request,
                ProviderRunIntakeAuthority("effect-one"),
            )
        runtime.set_request_preparer(
            lambda prepared, run_id, _authority: PreparedProviderRun(
                request=prepared,
                intake_receipt=ProviderRunIntakeReceipt(
                    effect_id="different-effect",
                    run_id=run_id,
                    work_item_id="work",
                    operation_id="operation",
                    attempt_id="attempt",
                ),
            )
        )
        with pytest.raises(
            ProviderStartAdmissionRejected,
            match="accepted_effect_intake_receipt_mismatch",
        ):
            await runtime.start_accepted(
                request,
                ProviderRunIntakeAuthority("effect-one"),
            )
        assert runtime.list_runs() == []
        assert adapter.calls == 0
    finally:
        await runtime.close()
        store.close()


async def test_runtime_scrubs_host_authority_metadata_after_accepted_preparation(tmp_path):
    async with _host(tmp_path) as host:
        canonical_prepare = host.coordinator.prepare_request

        def inject_authority_metadata(request, run_id, authority):
            prepared = canonical_prepare(request, run_id, authority)
            assert isinstance(prepared, PreparedProviderRun)
            prepared.request.metadata.update(
                {
                    "effect_id": "leaked-effect",
                    "origin_effect_id": "leaked-origin",
                    "claim_token": "leaked-claim",
                    "intake_authority": authority,
                    "intake_receipt": prepared.intake_receipt,
                }
            )
            prepared.request.metadata["work"] = {
                **dict(prepared.request.metadata["work"]),
                "effect_id": "nested-effect",
                "origin_effect_id": "nested-origin",
                "claim_token": "nested-claim",
            }
            return prepared

        host.runtime.set_request_preparer(inject_authority_metadata)
        host.adapter.spoof_authority_metadata = True
        result = await host.executor.execute(host.effect_id)

        assert result["status"] == "terminal"
        captured = host.adapter.requests[0]["metadata"]
        record = host.runtime.get_run(result["binding"]["provider_run_id"])
        assert record is not None
        for metadata in (captured, record.metadata):
            assert not (_HOST_AUTHORITY_TEST_KEYS & metadata.keys())
            assert not (_HOST_AUTHORITY_TEST_KEYS & metadata["work"].keys())
            assert metadata["source_user_text"] == "Build the accepted C2 artifact"
            assert "blue background" in metadata["source_user_context"]
        assert record.events
        for event in record.events:
            assert not (_HOST_AUTHORITY_TEST_KEYS & event["metadata"].keys())
        public = record.to_dict()
        assert not (_HOST_AUTHORITY_TEST_KEYS & public["metadata"].keys())
        for event in public["events"]:
            assert not (_HOST_AUTHORITY_TEST_KEYS & event["metadata"].keys())


async def test_explicit_new_mode_source_cannot_enter_ordinary_start(tmp_path):
    async with _host(tmp_path) as host:
        request = host.control.provider_request(host.effect_id)
        with pytest.raises(WorkLedgerConflict, match="accepted effect authority"):
            await host.runtime.start(request)
        assert host.control_store.get_effect(host.effect_id)["state"] == "pending"
        assert host.control.binding(host.effect_id) is None
        assert host.runtime.list_runs() == []
        assert host.adapter.calls == 0


@pytest.mark.parametrize(
    ("provider_status", "work_status", "control_outcome"),
    [
        ("error", "failed", "failed"),
        ("cancelled", "cancelled", "cancelled"),
    ],
)
async def test_terminal_failure_and_cancellation_use_the_same_receipt_path(
    tmp_path,
    provider_status,
    work_status,
    control_outcome,
):
    async with _host(tmp_path, result_status=provider_status) as host:
        result = await host.executor.execute(host.effect_id)
        binding = result["binding"]
        attempt = host.work.get_attempt(binding["attempt_id"])
        assert result["status"] == "terminal"
        assert attempt is not None and attempt.execution_status == work_status
        assert attempt.metadata["provider_terminal_pipeline"]["state"] == "completed"
        assert host.work.get_writer_lease(attempt.attempt_id).status == "released"
        assert len(host.work.list_completions(binding["work_item_id"])) == 1
        assert host.control_store.get_receipt(host.effect_id)["outcome"] == control_outcome
        assert host.adapter.calls == 1
        assert not (host.workspace / "accepted-c2.txt").exists()


async def test_terminal_replay_allocates_no_second_runtime_run(tmp_path):
    async with _host(tmp_path) as host:
        first = await host.executor.execute(host.effect_id)
        before = (
            host.adapter.calls,
            len(host.runtime.list_runs()),
            len(host.work.list_work_items()),
            dict(host.control_store.get_effect(host.effect_id)),
        )
        replay = await host.executor.execute(host.effect_id)
        assert not first["replayed"] and replay["replayed"]
        assert replay["binding"] == first["binding"]
        assert (
            host.adapter.calls,
            len(host.runtime.list_runs()),
            len(host.work.list_work_items()),
            host.control_store.get_effect(host.effect_id),
        ) == before


async def test_concurrent_executor_calls_share_one_runtime_and_adapter(tmp_path):
    async with _host(tmp_path, block=True) as host:
        first = asyncio.create_task(host.executor.execute(host.effect_id))
        await asyncio.wait_for(host.adapter.started.wait(), timeout=3)
        second = asyncio.create_task(host.executor.execute(host.effect_id))
        await asyncio.sleep(0)
        assert host.adapter.calls == 1
        assert len(host.runtime.list_runs()) == 1
        host.adapter.release.set()
        results = await asyncio.gather(first, second)
        assert {result["status"] for result in results} == {"terminal"}
        assert sorted(result["replayed"] for result in results) == [False, True]
        assert results[0]["binding"] == results[1]["binding"]
        assert len(host.work.list_work_items()) == 1


async def test_failure_after_atomic_intake_never_restarts_the_bound_effect(tmp_path):
    now = [100.0]
    clock = lambda: now[0]
    async with _host(tmp_path, clock=clock) as host:
        original = host.coordinator.prepare_request

        def fail_after_intake(request, run_id, authority):
            original(request, run_id, authority)
            raise RuntimeError("after atomic intake")

        host.runtime.set_request_preparer(fail_after_intake)
        with pytest.raises(RuntimeError, match="after atomic intake"):
            await host.executor.execute(host.effect_id)
        binding = host.control.binding(host.effect_id)
        assert binding is not None
        assert host.adapter.calls == 0
        assert host.runtime.list_runs() == []
        attempt = host.work.get_attempt(binding["attempt_id"])
        assert attempt is not None and attempt.execution_status == "queued"
        assert host.work.get_writer_lease(attempt.attempt_id).status == "active"
        replay = await host.executor.execute(host.effect_id)
        assert replay["status"] == "unknown" and replay["replayed"]
        assert host.adapter.calls == 0
        now[0] = 116.0
        host.control_store.expire_claims()
        assert host.control_store.get_effect(host.effect_id)["state"] == (
            "unknown_reconciling"
        )
        assert host.control_store.get_receipt(host.effect_id) is None


@pytest.mark.parametrize("failure", [WorkLedgerConflict, OSError, RuntimeError])
async def test_export_preparation_rejection_closes_effect_without_claiming_provider_execution(tmp_path, failure):
    async with _host(tmp_path) as host:
        with patch.object(host.coordinator.export_service, "prepare_plan",
                side_effect=failure("controlled preparation failure")) as prepare:
            dispatch = await host.executor.dispatch(host.effect_id)
            assert dispatch.status == "rejected" and not dispatch.replayed
            assert dispatch.record is None and host.adapter.calls == 0
            assert host.runtime.list_runs() == []
            attempt = host.work.get_attempt(dispatch.binding["attempt_id"])
            assert attempt.execution_status == "cancelled" and not attempt.started_at
            assert "controlled preparation failure" in attempt.error
            assert host.work.get_writer_lease(attempt.attempt_id).status == "released"
            receipt = dispatch.receipt
            assert receipt["outcome"] == "cancelled"
            assert receipt["details"]["authority"] == "work_intake_rejection"
            assert receipt["details"]["provider_started"] is False
            assert host.control_store.get_effect(host.effect_id)["state"] == "terminal"
            replay = await host.executor.execute(host.effect_id)
            assert replay["status"] == "terminal" and replay["replayed"]
            assert replay["receipt"] == receipt
            assert len(host.work.list_attempts(attempt.work_item_id)) == 1
            prepare.assert_called_once()


async def test_running_attempt_cannot_claim_preparation_rejection(tmp_path):
    async with _host(tmp_path, block=True) as host:
        dispatch = await host.executor.dispatch(host.effect_id)
        await host.adapter.started.wait()
        host.work.update_attempt(dispatch.binding["attempt_id"],
            metadata={"start_rejected":"forged_after_start"})
        with pytest.raises(ControlLedgerConflict, match="unstarted intake"):
            host.control.record_intake_rejection(host.effect_id)
        assert host.control_store.get_receipt(host.effect_id) is None
        host.adapter.release.set()
        await host.executor.finish(dispatch)


async def test_restart_closes_rejected_intake_gap_without_native_or_work_replay(tmp_path):
    async with _host(tmp_path) as host:
        with patch.object(host.coordinator.export_service, "prepare_plan",
                side_effect=WorkLedgerConflict("controlled preparation failure")):
            with pytest.raises(WorkLedgerConflict, match="controlled preparation failure"):
                await host.runtime.start_accepted(host.control.provider_request(host.effect_id),
                    ProviderRunIntakeAuthority(host.effect_id))
        binding = host.control.binding(host.effect_id)
        assert host.control_store.get_receipt(host.effect_id) is None
        restored = WorkControl(host.control_store, host.work)
        receipt = host.control_store.get_receipt(host.effect_id)
        assert receipt["details"]["authority"] == "work_intake_rejection"
        assert receipt["details"]["provider_started"] is False
        assert restored.binding(host.effect_id) == binding
        assert restored.reconcile_intake_rejections() == 0
        assert host.adapter.calls == 0 and host.runtime.list_runs() == []
        assert len(host.work.list_attempts(binding["work_item_id"])) == 1


async def test_orphaned_provider_result_becomes_control_unknown_without_retry(tmp_path):
    now = [100.0]
    async with _host(
        tmp_path,
        result_status="orphaned",
        clock=lambda: now[0],
    ) as host:
        result = await host.executor.execute(host.effect_id)
        binding = result["binding"]
        attempt = host.work.get_attempt(binding["attempt_id"])
        assert result["status"] == "unknown"
        assert attempt is not None and attempt.execution_status == "orphaned"
        assert host.work.get_writer_lease(attempt.attempt_id).status == "active"
        assert host.adapter.calls == 1
        assert host.control_store.get_receipt(host.effect_id) is None
        now[0] = 116.0
        host.control_store.expire_claims()
        effect = host.control_store.get_effect(host.effect_id)
        assert effect["state"] == "unknown_reconciling"
        assert len(host.control_store.due_unknown(owner="provider_submission")) == 1
        replay = await host.executor.execute(host.effect_id)
        assert replay["status"] == "unknown"
        assert host.adapter.calls == 1
        assert len(host.runtime.list_runs()) == 1


async def test_restart_projects_completed_work_to_control_without_provider_io(tmp_path):
    database: Path
    effect_id: str
    binding: dict[str, str]
    async with _host(tmp_path) as host:
        request = host.control.provider_request(host.effect_id)
        record = await host.runtime.start_accepted(
            request,
            ProviderRunIntakeAuthority(host.effect_id),
        )
        assert record.task_handle is not None
        await record.task_handle
        await host.coordinator.drain_provider_facts()
        database = host.database
        effect_id = host.effect_id
        binding = host.control.binding(effect_id)
        assert binding is not None
        attempt = host.work.get_attempt(binding["attempt_id"])
        assert attempt is not None
        assert attempt.metadata["provider_terminal_pipeline"]["state"] == "completed"
        assert host.control_store.get_receipt(effect_id) is None
        assert host.adapter.calls == 1

    work = WorkLedgerStore(database)
    control_store = ControlLedgerStore(database)
    control = WorkControl(control_store, work)
    coordinator = WorkLedgerCoordinator(work, work_control=control)
    runtime = ProviderRuntime()
    executor = WorkEffectExecutor(control, runtime, coordinator)
    try:
        projected = await executor.execute(effect_id)
        assert projected["status"] == "terminal" and not projected["replayed"]
        assert projected["binding"] == binding
        assert runtime.list_runs() == []
        assert control_store.get_receipt(effect_id)["outcome"] == "succeeded"
        replay = await executor.execute(effect_id)
        assert replay["replayed"] and runtime.list_runs() == []
    finally:
        await runtime.close()
        coordinator.close()
        control_store.close()
        work.close()


async def test_failure_after_control_receipt_commit_replays_without_provider_io(tmp_path):
    async with _host(tmp_path) as host:
        original = host.control.record_terminal_receipt

        def fail_after_receipt(effect_id):
            original(effect_id)
            raise RuntimeError("after Control receipt commit")

        with patch.object(
            host.control,
            "record_terminal_receipt",
            side_effect=fail_after_receipt,
        ):
            with pytest.raises(RuntimeError, match="after Control receipt commit"):
                await host.executor.execute(host.effect_id)

        committed = host.control_store.get_receipt(host.effect_id)
        assert committed is not None and committed["outcome"] == "succeeded"
        before = (
            host.adapter.calls,
            len(host.runtime.list_runs()),
            len(host.work.list_work_items()),
        )
        replay = await host.executor.execute(host.effect_id)
        assert replay["status"] == "terminal" and replay["replayed"]
        assert replay["receipt"] == committed
        assert (
            host.adapter.calls,
            len(host.runtime.list_runs()),
            len(host.work.list_work_items()),
        ) == before


async def test_pending_work_terminal_receipt_cannot_close_control_effect(tmp_path):
    async with _host(tmp_path) as host:
        request = host.control.provider_request(host.effect_id)
        record = await host.runtime.start_accepted(
            request,
            ProviderRunIntakeAuthority(host.effect_id),
        )
        assert record.task_handle is not None
        await record.task_handle
        await host.coordinator.drain_provider_facts()
        binding = host.control.binding(host.effect_id)
        assert binding is not None
        attempt = host.work.get_attempt(binding["attempt_id"])
        assert attempt is not None
        completed = dict(attempt.metadata["provider_terminal_pipeline"])
        pending = dict(completed)
        pending["state"] = "pending"
        pending.pop("completed_at", None)
        host.work.update_attempt(
            attempt.attempt_id,
            metadata={"provider_terminal_pipeline": pending},
        )
        with pytest.raises(ControlLedgerConflict, match="incomplete or mismatched"):
            host.control.record_terminal_receipt(host.effect_id)
        assert host.control_store.get_receipt(host.effect_id) is None
        host.work.update_attempt(
            attempt.attempt_id,
            metadata={"provider_terminal_pipeline": completed},
        )
        assert not host.control.record_terminal_receipt(host.effect_id)["replayed"]


@pytest.mark.parametrize(
    "mutation",
    ["task", "provider", "requirements", "source", "utterance", "context"],
)
async def test_mismatched_accepted_request_fails_before_work_or_provider(tmp_path, mutation):
    async with _host(tmp_path) as host:
        request = host.control.provider_request(host.effect_id)
        if mutation == "task":
            request.task = "Different task"
        elif mutation == "provider":
            request.provider = "other-provider"
        elif mutation == "requirements":
            request.requirements = ProviderRequirements(task_kind="general")
        elif mutation == "source":
            request.metadata["source_context_scope"] = "chat:other"
        elif mutation == "utterance":
            request.metadata["source_utterance_id"] = "other"
        else:
            request.metadata["source_user_context"] = "different context"
        with pytest.raises((ValueError, WorkLedgerConflict)):
            await host.runtime.start_accepted(
                request,
                ProviderRunIntakeAuthority(host.effect_id),
            )
        assert host.control.binding(host.effect_id) is None
        assert host.control_store.get_effect(host.effect_id)["state"] == "pending"
        assert host.adapter.calls == 0


def test_source_bound_seal_rejects_transcript_or_utterance_mismatch(tmp_path):
    database = tmp_path / "shared.sqlite3"
    workspace = tmp_path / "project"
    workspace.mkdir()
    work = WorkLedgerStore(database)
    project = work.create_or_get_project(workspace)
    store = ControlLedgerStore(database)
    control = WorkControl(store, work)
    admission = _admission()
    control.admit(admission, fence_scope="foreground-chat")
    try:
        payload = _payload(project.project_id, "generic-alpha")
        with pytest.raises((ControlLedgerConflict, ValueError)):
            control.seal(
                admission,
                replace(payload, source_user_text="different transcript"),
            )
        with pytest.raises(ControlLedgerConflict, match="transcript identity"):
            control.seal(
                admission,
                replace(payload, utterance_id="different-utterance"),
            )
        with pytest.raises(ControlLedgerConflict, match="title is not canonical"):
            control.seal(admission, replace(payload, title="x" * 97))
    finally:
        store.close()
        work.close()


async def test_independent_legacy_source_retains_ordinary_empty_origin_path(tmp_path):
    database = tmp_path / "shared.sqlite3"
    workspace = tmp_path / "project"
    workspace.mkdir()
    work = WorkLedgerStore(database)
    project = work.create_or_get_project(workspace)
    store = ControlLedgerStore(database)
    control = WorkControl(store, work)
    admission = _admission(mode="legacy")
    control.admit(admission, fence_scope="foreground-chat")
    coordinator = WorkLedgerCoordinator(work, work_control=control)
    runtime = ProviderRuntime()
    adapter = _RuntimeAdapter("generic-alpha", work)
    runtime.register(adapter)
    runtime.set_request_preparer(coordinator.prepare_request)
    coordinator.configure()
    request = ProviderRunRequest(
        provider="generic-alpha",
        task="Independent legacy task",
        cwd=str(workspace),
        requirements=ProviderRequirements(
            task_kind="workspace_mutation",
            workspace_access="write",
            workspace_ownership="caller",
        ),
        metadata={
            "source": "legacy-test",
            "session_id": "session-c2",
            "turn_id": "turn-one",
            "source_utterance_id": "utterance-one",
            "source_context_scope": "chat:session-c2",
            "intent": "execute",
            "work": {
                "project_id": project.project_id,
                "workspace_path": str(workspace),
                "workspace_mode": "local",
            },
        },
    )
    try:
        with (
            patch("config.settings.WORK_WORKTREE_ISOLATION", False),
            patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ),
        ):
            record = await runtime.start(request)
            assert record.task_handle is not None
            await record.task_handle
            await coordinator.drain_provider_facts()
        assert adapter.calls == 1
        attempts = work.list_attempts(work.list_work_items()[0].work_item_id)
        assert len(attempts) == 1 and attempts[0].origin_effect_id == ""
    finally:
        await runtime.close()
        runtime.set_request_preparer(None)
        coordinator.close()
        store.close()
        work.close()


async def test_separate_runtime_contenders_have_one_sqlite_winner_and_one_adapter(
    tmp_path,
):
    database = tmp_path / "shared.sqlite3"
    workspace = tmp_path / "project"
    workspace.mkdir()
    seed_work = WorkLedgerStore(database)
    project = seed_work.create_or_get_project(workspace)
    seed_store = ControlLedgerStore(database)
    seed_control = WorkControl(seed_store, seed_work)
    admission = _admission()
    seed_control.admit(admission, fence_scope="foreground-chat")
    effect_id = seed_control.seal(
        admission,
        _payload(project.project_id, "generic-alpha"),
    )["effect_id"]
    seed_store.close()
    seed_work.close()

    works = [WorkLedgerStore(database) for _ in range(2)]
    stores = [ControlLedgerStore(database) for _ in range(2)]
    controls = [WorkControl(stores[index], works[index]) for index in range(2)]
    coordinators = [
        WorkLedgerCoordinator(works[index], work_control=controls[index])
        for index in range(2)
    ]
    runtimes = [ProviderRuntime() for _ in range(2)]
    adapters = [
        _RuntimeAdapter("generic-alpha", works[index]) for index in range(2)
    ]
    executors = []
    for index in range(2):
        runtimes[index].register(adapters[index])
        runtimes[index].set_request_preparer(coordinators[index].prepare_request)
        executors.append(
            WorkEffectExecutor(controls[index], runtimes[index], coordinators[index])
        )
    gate = Barrier(2)
    original = WorkControl.bind_runtime_dispatch_intent

    def synchronized(self, *args, **kwargs):
        gate.wait(timeout=5)
        return original(self, *args, **kwargs)

    try:
        with (
            patch("config.settings.WORK_WORKTREE_ISOLATION", False),
            patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ),
            patch.object(
                WorkControl,
                "bind_runtime_dispatch_intent",
                synchronized,
            ),
        ):
            results = await asyncio.gather(
                *(executor.execute(effect_id) for executor in executors)
            )
        assert sum(adapter.calls for adapter in adapters) == 1
        assert sum(len(runtime.list_runs()) for runtime in runtimes) == 1
        assert len(works[0].list_work_items()) == 1
        bindings = [result["binding"] for result in results]
        assert bindings[0] == bindings[1] == controls[0].binding(effect_id)
        assert stores[0].get_effect(effect_id)["state"] == "dispatching"
        assert stores[0].get_receipt(effect_id) is None
    finally:
        for adapter in adapters:
            adapter.release.set()
        await asyncio.gather(
            *(runtime.close() for runtime in runtimes),
            return_exceptions=True,
        )
        for runtime in runtimes:
            runtime.set_request_preparer(None)
        for coordinator in coordinators:
            coordinator.close()
        for store in stores:
            store.close()
        for work in works:
            work.close()


def test_work_effect_owner_has_no_concrete_provider_and_only_cohort_installation():
    root = Path(__file__).resolve().parents[1]
    sources = "\n".join(
        (root / relative).read_text(encoding="utf-8").lower()
        for relative in (
            "server/work_control.py",
            "server/work_effect_executor.py",
            "agent_host/provider_runtime.py",
        )
    )
    assert 'provider == "codex"' not in sources
    assert 'provider == "openclaw"' not in sources
    assert "clientusermessageid" not in sources
    app_source = (root / "server" / "app.py").read_text(encoding="utf-8")
    cohort_start = app_source.index("    cooperative_chat = None")
    cohort_end = app_source.index(
        "    control_authority_enabled =", cohort_start)
    cohort = app_source[cohort_start:cohort_end]
    assert "from server.work_effect_executor import WorkEffectExecutor" in cohort
    assert "WorkEffectExecutor(cooperative_work_control" in cohort
    assert "WorkEffectExecutor" not in (
        app_source[:cohort_start] + app_source[cohort_end:])
    assert "ProviderRunIntakeAuthority" not in app_source
