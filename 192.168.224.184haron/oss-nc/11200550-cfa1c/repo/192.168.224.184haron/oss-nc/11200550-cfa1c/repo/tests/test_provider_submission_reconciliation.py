from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_host.provider_contract import ProviderCapabilities, ProviderManifest
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import (
    ProviderNativeExecutionHandle,
    ProviderRunResult,
    ProviderSessionHandle,
    ProviderSubmissionReconciliationRequest,
    ProviderSubmissionReconciliationResult,
)


class _ReconcilingAdapter:
    provider_id = "reconcile_test"
    manifest = ProviderManifest(
        provider_id=provider_id,
        display_name="Reconciliation test",
        capabilities=ProviderCapabilities(submission_reconciliation="query"),
    )

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.requests: list[ProviderSubmissionReconciliationRequest] = []

    async def run(self, request, run_id, emit) -> ProviderRunResult:
        del request, run_id, emit
        return ProviderRunResult(status="done")

    async def cancel(self, run_id: str) -> dict[str, Any]:
        del run_id
        return {"confirmed": False, "cancelled": False}

    async def reconcile_submission(
        self,
        request: ProviderSubmissionReconciliationRequest,
    ) -> ProviderSubmissionReconciliationResult:
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _AdvertisedWithoutMethod:
    provider_id = "missing_reconcile"
    manifest = ProviderManifest(
        provider_id=provider_id,
        display_name="Missing reconciliation method",
        capabilities=ProviderCapabilities(submission_reconciliation="query"),
    )

    async def run(self, request, run_id, emit) -> ProviderRunResult:
        del request, run_id, emit
        return ProviderRunResult(status="done")

    async def cancel(self, run_id: str) -> dict[str, Any]:
        del run_id
        return {"confirmed": False, "cancelled": False}


class _UnsupportedAdapter(_AdvertisedWithoutMethod):
    provider_id = "unsupported_reconcile"
    manifest = ProviderManifest(
        provider_id=provider_id,
        display_name="Unsupported reconciliation",
    )


def _restore_orphan(runtime: ProviderRuntime, provider: str, run_id: str) -> None:
    runtime.add_orphaned_run(
        provider=provider,
        run={
            "run_id": run_id,
            "task": "Perform the already-authorized task",
            "cwd": None,
            "updated_at": 10.0,
            "metadata": {
                "provider_session": ProviderSessionHandle(
                    provider=provider,
                    session_id="native-session-1",
                ).to_dict(),
                "work": {
                    "work_item_id": "work-1",
                    "attempt_id": "attempt-1",
                },
            },
        },
    )


def test_manifest_serializes_optional_submission_reconciliation() -> None:
    supported = ProviderCapabilities(submission_reconciliation="query")
    unsupported = ProviderCapabilities()

    assert supported.to_dict()["submission_reconciliation"] == "query"
    assert unsupported.to_dict()["submission_reconciliation"] == "none"
    with pytest.raises(ValueError, match="submission reconciliation mode"):
        ProviderCapabilities(submission_reconciliation="retry")  # type: ignore[arg-type]


def test_reconciliation_result_enforces_exact_match_shape() -> None:
    execution = ProviderNativeExecutionHandle(
        provider="reconcile_test",
        execution_id="native-run-1",
    )

    assert ProviderSubmissionReconciliationResult(
        state="matched_active",
        execution=execution,
    ).to_dict()["native_execution"]["execution_id"] == "native-run-1"
    terminal = ProviderSubmissionReconciliationResult(
        state="matched_terminal",
        execution=execution,
        terminal_result=ProviderRunResult(status="done", result="finished"),
    )
    assert terminal.to_dict()["terminal_result"]["status"] == "done"

    with pytest.raises(ValueError, match="requires native execution"):
        ProviderSubmissionReconciliationResult(state="matched_active")
    with pytest.raises(ValueError, match="cannot carry native execution"):
        ProviderSubmissionReconciliationResult(
            state="ambiguous",
            execution=execution,
        )
    with pytest.raises(ValueError, match="must be terminal"):
        ProviderSubmissionReconciliationResult(
            state="matched_terminal",
            execution=execution,
            terminal_result=ProviderRunResult(status="orphaned"),
        )


def test_runtime_rejects_advertised_capability_without_method() -> None:
    runtime = ProviderRuntime()

    with pytest.raises(ValueError, match="without reconcile_submission"):
        runtime.register(_AdvertisedWithoutMethod())


def test_runtime_inspects_active_match_without_mutating_orphan() -> None:
    async def scenario() -> None:
        outcome = ProviderSubmissionReconciliationResult(
            state="matched_active",
            execution=ProviderNativeExecutionHandle(
                provider="reconcile_test",
                execution_id="native-run-active",
            ),
        )
        adapter = _ReconcilingAdapter(outcome)
        runtime = ProviderRuntime()
        runtime.register(adapter)
        _restore_orphan(runtime, adapter.provider_id, "reconcile_test_run_1")
        record = runtime.get_run("reconcile_test_run_1")
        assert record is not None
        before = record.to_dict()

        observed = await runtime.inspect_orphaned_submission(record.run_id)

        assert observed is outcome
        assert adapter.requests == [
            ProviderSubmissionReconciliationRequest(
                provider="reconcile_test",
                run_id="reconcile_test_run_1",
                session=ProviderSessionHandle(
                    provider="reconcile_test",
                    session_id="native-session-1",
                ),
            )
        ]
        assert record.to_dict() == before
        assert record.status == "orphaned"
        assert record.events == []

    asyncio.run(scenario())


def test_runtime_inspects_durable_request_without_fabricating_runtime_record() -> None:
    async def scenario() -> None:
        outcome = ProviderSubmissionReconciliationResult(
            state="matched_active",
            execution=ProviderNativeExecutionHandle(
                provider="reconcile_test",
                execution_id="native-run-from-durable-work",
            ),
        )
        adapter = _ReconcilingAdapter(outcome)
        runtime = ProviderRuntime()
        runtime.register(adapter)
        request = ProviderSubmissionReconciliationRequest(
            provider=adapter.provider_id,
            run_id="host-run-from-durable-work",
            session=None,
        )

        observed = await runtime.inspect_submission(request)

        assert observed is outcome
        assert adapter.requests == [request]
        assert runtime.list_runs() == []

        unsupported = _UnsupportedAdapter()
        runtime.register(unsupported)
        unavailable = await runtime.inspect_submission(
            ProviderSubmissionReconciliationRequest(
                provider=unsupported.provider_id,
                run_id="host-run-unsupported",
            )
        )
        assert unavailable == ProviderSubmissionReconciliationResult(
            state="unavailable",
            reason="provider_does_not_support_submission_reconciliation",
        )
        assert runtime.list_runs() == []

    asyncio.run(scenario())


def test_runtime_observes_terminal_match_without_publishing_it() -> None:
    async def scenario() -> None:
        outcome = ProviderSubmissionReconciliationResult(
            state="matched_terminal",
            execution=ProviderNativeExecutionHandle(
                provider="reconcile_test",
                execution_id="native-run-terminal",
            ),
            terminal_result=ProviderRunResult(
                status="done",
                result="native result",
                session=ProviderSessionHandle(
                    provider="reconcile_test",
                    session_id="native-session-1",
                ),
            ),
        )
        adapter = _ReconcilingAdapter(outcome)
        runtime = ProviderRuntime()
        runtime.register(adapter)
        _restore_orphan(runtime, adapter.provider_id, "reconcile_test_run_2")
        record = runtime.get_run("reconcile_test_run_2")
        assert record is not None
        before = record.to_dict()

        observed = await runtime.inspect_orphaned_submission(record.run_id)

        assert observed.state == "matched_terminal"
        assert observed.terminal_result is not None
        assert observed.terminal_result.result == "native result"
        assert record.to_dict() == before
        assert record.status == "orphaned"

    asyncio.run(scenario())


def test_runtime_returns_typed_unavailable_without_query_or_state_change() -> None:
    async def scenario() -> None:
        runtime = ProviderRuntime()
        adapter = _UnsupportedAdapter()
        runtime.register(adapter)
        _restore_orphan(runtime, adapter.provider_id, "unsupported_run_1")
        record = runtime.get_run("unsupported_run_1")
        assert record is not None
        before = record.to_dict()

        observed = await runtime.inspect_orphaned_submission(record.run_id)

        assert observed == ProviderSubmissionReconciliationResult(
            state="unavailable",
            reason="provider_does_not_support_submission_reconciliation",
        )
        assert record.to_dict() == before

    asyncio.run(scenario())


def test_runtime_contains_query_failure_and_rejects_wrong_provider_identity() -> None:
    async def scenario() -> None:
        failing = _ReconcilingAdapter(RuntimeError("transport unavailable"))
        runtime = ProviderRuntime()
        runtime.register(failing)
        _restore_orphan(runtime, failing.provider_id, "reconcile_test_run_3")
        record = runtime.get_run("reconcile_test_run_3")
        assert record is not None
        before = record.to_dict()

        unavailable = await runtime.inspect_orphaned_submission(record.run_id)

        assert unavailable.state == "unavailable"
        assert unavailable.reason == "provider_query_failed:RuntimeError"
        assert record.to_dict() == before

        failing.outcome = ProviderSubmissionReconciliationResult(
            state="matched_active",
            execution=ProviderNativeExecutionHandle(
                provider="another_provider",
                execution_id="native-run-wrong-provider",
            ),
        )
        with pytest.raises(ValueError, match="belongs to another provider"):
            await runtime.inspect_orphaned_submission(record.run_id)

    asyncio.run(scenario())


def test_runtime_does_not_use_reconciliation_as_a_general_status_query() -> None:
    async def scenario() -> None:
        adapter = _ReconcilingAdapter(
            ProviderSubmissionReconciliationResult(state="not_observed")
        )
        runtime = ProviderRuntime()
        runtime.register(adapter)
        _restore_orphan(runtime, adapter.provider_id, "reconcile_test_run_4")
        record = runtime.get_run("reconcile_test_run_4")
        assert record is not None
        record.status = "running"

        observed = await runtime.inspect_orphaned_submission(record.run_id)

        assert observed == ProviderSubmissionReconciliationResult(
            state="unavailable",
            reason="provider_run_is_not_orphaned",
        )
        assert adapter.requests == []

        with pytest.raises(ValueError, match="unknown provider run"):
            await runtime.inspect_orphaned_submission("missing-run")

    asyncio.run(scenario())


def test_reconciliation_request_keeps_provider_session_opaque_and_bound() -> None:
    session = ProviderSessionHandle(
        provider="reconcile_test",
        session_id="opaque-native-session",
    )

    request = ProviderSubmissionReconciliationRequest(
        provider="reconcile_test",
        run_id="reconcile_test_run_5",
        session=session,
    )

    assert request.session is session
    with pytest.raises(ValueError, match="another provider"):
        ProviderSubmissionReconciliationRequest(
            provider="reconcile_test",
            run_id="reconcile_test_run_5",
            session=ProviderSessionHandle(
                provider="other",
                session_id="opaque-native-session",
            ),
        )
