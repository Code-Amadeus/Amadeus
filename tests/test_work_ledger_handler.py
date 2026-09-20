"""WorkLedgerHandler API and canvas-action boundary tests."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as settings
from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST
from agent_host.provider_types import ProviderRunRequest
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from server.canvas_action_router import CanvasActionRouter
from server.handlers.work_ledger_handler import WorkLedgerHandler
from server.protocol import Method
from server.work_ledger_coordinator import (
    WORKSPACE_ROUTING_SURFACE,
    WorkLedgerCoordinator,
)


def _prepare(
    coordinator: WorkLedgerCoordinator,
    *,
    cwd: Path,
    task: str,
    mode: str = "agent",
    provider: str = "fake",
) -> tuple[ProviderRunRequest, str, str]:
    request = ProviderRunRequest(
        provider=provider,
        task=task,
        cwd=str(cwd),
        mode=mode,
        metadata={"source": "handler-test"},
    )
    coordinator.prepare_request(request)
    binding = request.metadata["work"]
    return request, str(binding["work_item_id"]), str(binding["attempt_id"])


async def _finish(
    coordinator: WorkLedgerCoordinator,
    request: ProviderRunRequest,
    run_id: str,
) -> None:
    await coordinator._on_provider_event(
        Method.PROVIDER_EVENT,
        {
            "provider": request.provider,
            "run_id": run_id,
            "type": "run.created",
            "payload": {"task": request.task, "cwd": request.cwd, "mode": request.mode},
            "metadata": request.metadata,
        },
    )
    await coordinator._on_provider_result(
        Method.PROVIDER_RESULT,
        {
            "provider": request.provider,
            "run_id": run_id,
            "status": "done",
            "result": "Finished this attempt.",
            "error": "",
            "metadata": request.metadata,
        },
    )


class FakeProviderRun:
    def __init__(self, coordinator: WorkLedgerCoordinator) -> None:
        self.coordinator = coordinator
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(params))
        if params.get("resume"):
            return {
                "run": {
                    "run_id": str(params["resume"]),
                    "status": "running",
                    "metadata": dict(params.get("metadata") or {}),
                }
            }
        request = ProviderRunRequest(
            provider=str(params.get("provider") or "fake"),
            task=str(params.get("task") or "continue"),
            cwd=str(params.get("cwd") or ""),
            mode=str(params.get("mode") or "agent"),
            metadata=dict(params.get("metadata") or {}),
        )
        self.coordinator.prepare_request(request)
        attempt_id = str(request.metadata["work"]["attempt_id"])
        run_id = f"fake_continue_{len(self.calls)}"
        self.coordinator.store.bind_provider_run(attempt_id, run_id)
        return {
            "run": {
                "run_id": run_id,
                "status": "queued",
                "metadata": request.metadata,
            }
        }


def test_canvas_route_binds_selection_focus_and_execution_to_canonical_task() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_handler_canvas_") as temp:
            root = Path(temp)
            (root / "project-a").mkdir()
            (root / "project-b").mkdir()
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(store)
                _, item_a, attempt_a = _prepare(
                    coordinator,
                    cwd=root / "project-a",
                    task="Task A",
                    mode="plan",
                )
                _, item_b, _ = _prepare(
                    coordinator,
                    cwd=root / "project-b",
                    task="Task B",
                    mode="plan",
                )
                fake_provider = FakeProviderRun(coordinator)
                handler = WorkLedgerHandler(coordinator, provider_run=fake_provider)

                def current_revision() -> str:
                    return str(coordinator.snapshot()["revision"])

                selected = await handler.route_action(
                    {
                        "action": "select",
                        "workItemId": item_a,
                        "revision": current_revision(),
                    }
                )
                assert selected["ok"] is True
                assert selected["work"]["selectedWorkItemId"] == item_a
                assert coordinator.workspace_routing_focus()["mode"] == "auto"

                with patch(
                    "server.work_ledger_coordinator.cwd_in_project_registry",
                    return_value=True,
                ):
                    pinned = await handler.route_action(
                        {
                            "target": "work_item",
                            "action": "set_focus",
                            "workItemId": item_a,
                            "focusMode": "pinned",
                            "revision": current_revision(),
                        }
                    )
                assert pinned["ok"] is True
                assert pinned["work"]["workspaceFocusMode"] == "pinned"
                assert pinned["work"]["workspaceFocusWorkItemId"] == item_a
                assert store.get_focus(WORKSPACE_ROUTING_SURFACE).work_item_id == item_a  # type: ignore[union-attr]

                viewed_b = await handler.route_action(
                    {
                        "target": "work_item",
                        "action": "select",
                        "workItemId": item_b,
                        "revision": current_revision(),
                    }
                )
                assert viewed_b["ok"] is True
                assert viewed_b["work"]["selectedWorkItemId"] == item_b
                assert coordinator.workspace_routing_focus()["workItemId"] == item_a

                unlocked = await handler.route_action(
                    {
                        "target": "work_item",
                        "action": "set_focus",
                        "focusMode": "auto",
                        "revision": current_revision(),
                    }
                )
                assert unlocked["ok"] is True
                assert unlocked["work"]["focusMode"] == "auto"
                assert unlocked["work"]["selectedWorkItemId"] == item_b
                assert unlocked["work"]["workspaceFocusMode"] == "auto"
                assert coordinator.workspace_routing_focus()["mode"] == "auto"

                await handler.route_action(
                    {
                        "target": "work_item",
                        "action": "select",
                        "workItemId": item_a,
                        "revision": current_revision(),
                    }
                )

                attempt_count = len(store.list_attempts(item_b))
                denied = await handler.route_action(
                    {"action": "continue", "workItemId": item_b}
                )
                assert denied == {"ok": False, "error": "unsupported_action"}
                assert len(store.list_attempts(item_b)) == attempt_count
                assert fake_provider.calls == []

                store.update_attempt(attempt_a, execution_status="succeeded")
                canonical = coordinator.snapshot()
                revision = canonical["revision"]
                background = await handler.route_action(
                    {
                        "target": "work_item",
                        "action": "continue",
                        "workItemId": item_b,
                        "attemptId": store.list_attempts(item_b)[-1].attempt_id,
                        "revision": revision,
                    }
                )
                assert background == {"ok": False, "error": "unsupported_action"}
                before_continue = len(store.list_attempts(item_a))
                current = await handler.route_action(
                    {
                        "target": "work_item",
                        "action": "continue",
                        "workItemId": item_a,
                        "attemptId": attempt_a,
                        "revision": revision,
                    }
                )
                assert current == {"ok": False, "error": "unsupported_action"}
                assert len(store.list_attempts(item_a)) == before_continue

                missing_revision = await handler.route_action(
                    {"target": "work_item", "action": "accept", "workItemId": item_a}
                )
                assert missing_revision["ok"] is False
                assert missing_revision["error"] == "missing_revision"

    asyncio.run(run())


def test_accept_reopen_archive_and_continue_is_not_a_followup_path() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_handler_state_") as temp:
            root = Path(temp)
            (root / "project").mkdir()
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(store)
                initial, item_id, _ = _prepare(
                    coordinator,
                    cwd=root / "project",
                    task="Durable task",
                )
                await _finish(coordinator, initial, "fake_initial")
                assert store.get_work_item(item_id).state == "review_ready"  # type: ignore[union-attr]

                fake_provider = FakeProviderRun(coordinator)
                handler = WorkLedgerHandler(coordinator, provider_run=fake_provider)
                stale = await handler.route_action(
                    {
                        "target": "work_item",
                        "action": "accept",
                        "workItemId": item_id,
                        "revision": "stale-revision",
                    }
                )
                assert stale["ok"] is False and stale["error"] == "stale_revision"

                accepted = await handler.route_action(
                    {
                        "target": "work_item",
                        "action": "accept",
                        "workItemId": item_id,
                        "revision": coordinator.snapshot()["revision"],
                    }
                )
                assert accepted["ok"] is True
                assert store.get_work_item(item_id).state == "accepted"  # type: ignore[union-attr]
                accepted_assessments = store.list_completions(item_id)
                assert accepted_assessments[-1].source == "user"
                assert "Slice disposition menu" in accepted_assessments[-1].rationale

                # Electron and Slice share one idempotent disposition path.
                repeated = await handler.handle(Method.WORK_ACCEPT, {"workItemId": item_id})
                assert repeated is not None
                assert len(store.list_completions(item_id)) == len(accepted_assessments)
                assert Method.WORK_CONTINUE not in handler.methods

                assert await handler.handle(
                    Method.WORK_CONTINUE,
                    {"workItemId": item_id},
                ) is None

                await handler.handle(Method.WORK_REOPEN, {"workItemId": item_id})
                assert store.get_work_item(item_id).state == "open"  # type: ignore[union-attr]
                assert await handler.handle(
                    Method.WORK_CONTINUE,
                    {"workItemId": item_id, "task": "Continue the same task"},
                ) is None
                assert [attempt.attempt_number for attempt in store.list_attempts(item_id)] == [1]

                archived = await handler.route_action(
                    {
                        "target": "work_item",
                        "action": "archive",
                        "workItemId": item_id,
                        "revision": coordinator.snapshot()["revision"],
                    }
                )
                assert archived["ok"] is True
                assert store.get_work_item(item_id).state == "archived"  # type: ignore[union-attr]
                archive_assessments = store.list_completions(item_id)
                assert archive_assessments[-1].source == "user"
                assert archive_assessments[-1].work_item_state == "archived"
                repeated_archive = await handler.handle(Method.WORK_ARCHIVE, {"workItemId": item_id})
                assert repeated_archive is not None
                assert len(store.list_completions(item_id)) == len(archive_assessments)
                assert await handler.handle(
                    Method.WORK_CONTINUE,
                    {"workItemId": item_id},
                ) is None

                await handler.handle(Method.WORK_REOPEN, {"workItemId": item_id})
                assert await handler.handle(
                    Method.WORK_CONTINUE,
                    {"workItemId": item_id},
                ) is None
                assert [attempt.attempt_number for attempt in store.list_attempts(item_id)] == [1]
                assert fake_provider.calls == []

    asyncio.run(run())


def test_start_retry_and_resume_have_distinct_attempt_semantics() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_handler_attempt_actions_") as temp:
            root = Path(temp)
            (root / "start").mkdir()
            (root / "retry").mkdir()
            (root / "resume").mkdir()
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(store)
                fake_provider = FakeProviderRun(coordinator)
                handler = WorkLedgerHandler(coordinator, provider_run=fake_provider)

                started = await handler.handle(
                    Method.WORK_START,
                    {
                        "provider": "fake",
                        "task": "Brand new work",
                        "cwd": str(root / "start"),
                        "mode": "plan",
                    },
                )
                assert started is not None
                started_item = started["run"]["metadata"]["work"]["work_item_id"]
                started_work = started["run"]["metadata"]["work"]
                assert started_work["attempt_number"] == 1
                assert len(store.list_attempts(started_item)) == 1

                started_again = await handler.handle(
                    Method.WORK_START,
                    {
                        "provider": "fake",
                        "task": "Another new instruction in the same cwd",
                        "cwd": str(root / "start"),
                        "mode": "plan",
                        "metadata": {"project_id": started_work["project_id"]},
                    },
                )
                assert started_again is not None
                second_work = started_again["run"]["metadata"]["work"]
                assert second_work["work_item_id"] != started_item
                assert second_work["project_id"] == started_work["project_id"]
                assert second_work["attempt_number"] == 1
                assert Path(second_work["workspace_path"]) == (root / "start").resolve()
                assert len(store.list_attempts(second_work["work_item_id"])) == 1

                # work.start is always a new semantic instruction.  Caller
                # metadata must not be able to disguise it as Retry/Resume or
                # bind it back onto an existing WorkItem.
                malicious_start = await handler.handle(
                    Method.WORK_START,
                    {
                        "provider": "fake",
                        "task": "A third, genuinely new instruction",
                        "cwd": str(root / "start"),
                        "mode": "plan",
                        "resume": "fake_old_run",
                        "workItemId": started_item,
                        "metadata": {
                            "continuation": "retry",
                            "retry_of": started_work["attempt_id"],
                            "work_item_id": started_item,
                            "work": {
                                "project_id": started_work["project_id"],
                                "work_item_id": started_item,
                                "attempt_id": started_work["attempt_id"],
                            },
                        },
                    },
                )
                assert malicious_start is not None
                malicious_work = malicious_start["run"]["metadata"]["work"]
                assert malicious_work["work_item_id"] not in {
                    started_item,
                    second_work["work_item_id"],
                }
                assert malicious_work["project_id"] == started_work["project_id"]
                assert len(store.list_attempts(started_item)) == 1
                assert fake_provider.calls[-1].get("resume") is None
                assert fake_provider.calls[-1]["metadata"]["continuation"] == "new"
                assert "retry_of" not in fake_provider.calls[-1]["metadata"]

                retry_request, retry_item, retry_attempt = _prepare(
                    coordinator,
                    cwd=root / "retry",
                    task="Retry exactly this input",
                )
                await coordinator._on_provider_event(
                    Method.PROVIDER_EVENT,
                    {
                        "provider": "fake",
                        "run_id": "fake_retry_failed",
                        "type": "run.created",
                        "payload": {"task": retry_request.task, "cwd": retry_request.cwd, "mode": retry_request.mode},
                        "metadata": retry_request.metadata,
                    },
                )
                await coordinator._on_provider_result(
                    Method.PROVIDER_RESULT,
                    {
                        "provider": "fake",
                        "run_id": "fake_retry_failed",
                        "status": "error",
                        "result": "",
                        "error": "failed",
                        "metadata": retry_request.metadata,
                    },
                )
                retried = await handler.handle(Method.WORK_RETRY, {"workItemId": retry_item})
                assert retried is not None
                retry_attempts = store.list_attempts(retry_item)
                assert [attempt.attempt_number for attempt in retry_attempts] == [1, 2]
                assert retry_attempts[0].attempt_id == retry_attempt
                assert retry_attempts[1].task == "Retry exactly this input"
                assert fake_provider.calls[-1]["metadata"]["continuation"] == "retry"
                assert fake_provider.calls[-1]["metadata"]["retry_of"] == retry_attempt
                assert (
                    fake_provider.calls[-1]["metadata"]["checkpoint_handoff"]["previous_attempt"]["attempt_id"]
                    == retry_attempt
                )

                resume_request, resume_item, resume_attempt_id = _prepare(
                    coordinator,
                    cwd=root / "resume",
                    task="Resume provider checkpoint",
                )
                store.bind_provider_run(resume_attempt_id, "fake_orphaned_run")
                store.update_attempt(
                    resume_attempt_id,
                    execution_status="orphaned",
                    metadata={"runtime_resumable": True},
                )
                before = len(store.list_attempts(resume_item))
                resumed = await handler.handle(Method.WORK_RESUME, {"workItemId": resume_item})
                assert resumed is not None
                assert resumed["run"]["run_id"] == "fake_orphaned_run"
                assert len(store.list_attempts(resume_item)) == before
                assert fake_provider.calls[-1]["resume"] == "fake_orphaned_run"
                assert resume_request.metadata["work"]["attempt_id"] == resume_attempt_id

    asyncio.run(run())


def test_resume_requires_live_checkpoint_and_rolls_back_failed_writer_lease() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_handler_resume_guard_") as temp:
            root = Path(temp)
            workspace = root / "project"
            workspace.mkdir()
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(store)
                _, item_id, attempt_id = _prepare(
                    coordinator,
                    cwd=workspace,
                    task="Interrupted write with missing checkpoint",
                )
                store.bind_provider_run(attempt_id, "missing_runtime_checkpoint")
                store.update_attempt(attempt_id, execution_status="running")
                coordinator.adopt_runtime_records([])

                attempt = store.get_attempt(attempt_id)
                assert attempt is not None and attempt.execution_status == "orphaned"
                assert attempt.metadata["runtime_resumable"] is False
                assert coordinator.detail(item_id)["canResume"] is False
                assert store.get_writer_lease(attempt_id).status == "stale"  # type: ignore[union-attr]

                calls = 0

                async def fail_resume(_params: dict[str, Any]) -> dict[str, Any]:
                    nonlocal calls
                    calls += 1
                    raise RuntimeError("provider checkpoint vanished")

                handler = WorkLedgerHandler(coordinator, provider_run=fail_resume)
                try:
                    await handler.handle(Method.WORK_RESUME, {"workItemId": item_id})
                except WorkLedgerConflict as exc:
                    assert "checkpoint is unavailable" in str(exc)
                else:
                    raise AssertionError("an unregistered orphan must not advertise Resume")
                assert calls == 0

                # A checkpoint can disappear after startup discovery.  The
                # failed provider call must not leave the reacquired writer
                # lease active and poison the workspace.
                store.update_attempt(
                    attempt_id,
                    metadata={"runtime_resumable": True},
                )
                try:
                    await handler.handle(Method.WORK_RESUME, {"workItemId": item_id})
                except RuntimeError as exc:
                    assert "checkpoint vanished" in str(exc)
                else:
                    raise AssertionError("the simulated runtime failure must propagate")
                assert calls == 1
                assert store.get_writer_lease(attempt_id).status == "stale"  # type: ignore[union-attr]
                assert store.get_attempt(attempt_id).metadata["runtime_resumable"] is False  # type: ignore[union-attr]
                assert coordinator.detail(item_id)["canResume"] is False

    asyncio.run(run())


def test_retry_amendments_compose_and_preserve_lineage() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_handler_amendments_") as temp:
            root = Path(temp)
            workspace = root / "project"
            workspace.mkdir()
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(store, clock=lambda: 1784478600.0)
                fake_provider = FakeProviderRun(coordinator)
                handler = WorkLedgerHandler(coordinator, provider_run=fake_provider)
                request, item_id, first_attempt_id = _prepare(
                    coordinator,
                    cwd=workspace,
                    task="Build a complete chess game.",
                )
                await coordinator._on_provider_event(
                    Method.PROVIDER_EVENT,
                    {
                        "provider": "fake",
                        "run_id": "amendment-run-1",
                        "type": "run.created",
                        "payload": {"task": request.task, "cwd": request.cwd, "mode": request.mode},
                        "metadata": request.metadata,
                    },
                )
                await coordinator._on_provider_result(
                    Method.PROVIDER_RESULT,
                    {
                        "provider": "fake",
                        "run_id": "amendment-run-1",
                        "status": "error",
                        "error": "needs correction",
                        "metadata": request.metadata,
                    },
                )

                before = len(store.list_attempts(item_id))
                try:
                    await handler.handle(
                        Method.WORK_RETRY,
                        {"workItemId": item_id, "amendment_text": "x" * 2001},
                    )
                except ValueError as exc:
                    assert "2000" in str(exc)
                else:
                    raise AssertionError("an overlong amendment must be rejected")
                assert len(store.list_attempts(item_id)) == before

                first_retry = await handler.handle(
                    Method.WORK_RETRY,
                    {
                        "workItemId": item_id,
                        "amendment_text": "Use HTML canvas instead of pygame.",
                    },
                )
                assert first_retry is not None
                attempts = store.list_attempts(item_id)
                second = attempts[-1]
                expected_first = (
                    "Build a complete chess game.\n\n"
                    "[USER AMENDMENT 1 @ 2026-07-19T16:30:00+00:00]\n"
                    "Use HTML canvas instead of pygame."
                )
                assert second.task == expected_first
                assert second.metadata["amended_from"] == first_attempt_id
                assert second.metadata["amendments"][0]["number"] == 1
                assert second.metadata["interventions"][0]["kind"] == "user_amendment"
                assert second.metadata["interventions"][0]["status"] == "injected"
                assert first_retry["run"]["metadata"]["amended_from"] == first_attempt_id

                await coordinator._on_provider_result(
                    Method.PROVIDER_RESULT,
                    {
                        "provider": "fake",
                        "run_id": first_retry["run"]["run_id"],
                        "status": "error",
                        "error": "one more correction",
                        "metadata": first_retry["run"]["metadata"],
                    },
                )
                second_retry = await handler.handle(
                    Method.WORK_RETRY,
                    {
                        "workItemId": item_id,
                        "amendmentText": "Keep everything in one file.",
                    },
                )
                assert second_retry is not None
                attempts = store.list_attempts(item_id)
                third = attempts[-1]
                assert third.task == (
                    f"{expected_first}\n\n"
                    "[USER AMENDMENT 2 @ 2026-07-19T16:30:00+00:00]\n"
                    "Keep everything in one file."
                )
                assert third.metadata["amended_from"] == second.attempt_id
                assert [entry["number"] for entry in third.metadata["amendments"]] == [1, 2]
                assert [entry["amended_from"] for entry in third.metadata["amendments"]] == [
                    first_attempt_id,
                    second.attempt_id,
                ]
                assert len(third.metadata["interventions"]) == 2

    asyncio.run(run())


def test_denied_codex_permission_requires_explicit_bounded_authorized_retry() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_handler_permission_retry_") as temp:
            root = Path(temp)
            workspace = root / "project"
            workspace.mkdir()
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(store, clock=lambda: 1784478600.0)
                fake_provider = FakeProviderRun(coordinator)
                handler = WorkLedgerHandler(coordinator, provider_run=fake_provider)
                request, item_id, attempt_id = _prepare(
                    coordinator,
                    cwd=workspace,
                    task="Fetch the protected dependency.",
                    provider="codex",
                )
                store.update_attempt(attempt_id, execution_status="failed")
                permission = store.create_permission_request(
                    item_id,
                    attempt_id=attempt_id,
                    capability="network.fetch",
                    action="download_dependency",
                    scope_paths=["https://packages.example.test/tool"],
                    reason="Network access was denied by provider policy.",
                    reversibility="read-only",
                    options=["deny"],
                    idempotency_key="codex-denied-network",
                    metadata={
                        "kind": "provider_permission",
                        "provider": "codex",
                        "diagnostic_only": True,
                        "retry_required": True,
                    },
                )
                permission = store.resolve_permission_request(
                    permission.request_id,
                    "denied",
                    metadata={"resolution": "provider_denied"},
                )

                projected = coordinator.detail(item_id)
                assert projected["canRetry"] is True
                assert projected["retryAuthorizationRequestId"] == permission.request_id

                try:
                    await handler.handle(
                        Method.WORK_RETRY,
                        {
                            "workItemId": item_id,
                            "authorization_permission_request_id": permission.request_id,
                            "amendment_text": "Also allow every future request.",
                        },
                    )
                except WorkLedgerConflict as exc:
                    assert "cannot include a separate correction" in str(exc)
                else:
                    raise AssertionError("authorized Retry must not accept mixed renderer text")

                retried = await handler.handle(
                    Method.WORK_RETRY,
                    {
                        "workItemId": item_id,
                        "authorizationPermissionRequestId": permission.request_id,
                    },
                )
                assert retried is not None
                latest = store.list_attempts(item_id)[-1]
                assert latest.metadata["authorization_permission_request_id"] == permission.request_id
                assert latest.metadata["interventions"][-1]["kind"] == "permission_authorization"
                assert latest.metadata["interventions"][-1]["source"] == "button"
                assert latest.metadata["interventions"][-1]["permission_request_id"] == permission.request_id
                assert "[USER AMENDMENT 1 @ 2026-07-19T16:30:00+00:00]" in latest.task
                assert "[PER-REQUEST AUTHORIZATION]" in latest.task
                assert "Capability: network.fetch" in latest.task
                assert "Action: download_dependency" in latest.task
                assert "https://packages.example.test/tool" in latest.task
                assert "immediately following" in latest.task
                assert "always-allow rule" in latest.task

                # The renderer cannot bind a denied request from another
                # attempt or invent broader capability/scope fields.
                store.update_attempt(latest.attempt_id, execution_status="failed")
                before = len(store.list_attempts(item_id))
                try:
                    await handler.handle(
                        Method.WORK_RETRY,
                        {
                            "workItemId": item_id,
                            "authorization_permission_request_id": permission.request_id,
                            "capability": "filesystem.root",
                            "scope": ["C:\\"],
                        },
                    )
                except WorkLedgerConflict as exc:
                    assert "latest attempt" in str(exc)
                else:
                    raise AssertionError("authorization must bind to the latest failed attempt")
                assert len(store.list_attempts(item_id)) == before

    asyncio.run(run())


def test_active_provider_permission_resumes_through_the_runtime_contract() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_handler_native_permission_") as temp:
            root = Path(temp)
            workspace = root / "project"
            workspace.mkdir()
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(store)
                request, item_id, attempt_id = _prepare(
                    coordinator,
                    cwd=workspace,
                    task="Run a protected verification.",
                    provider="interactive",
                )
                run_id = "interactive_run_1"
                await coordinator._on_provider_event(
                    Method.PROVIDER_EVENT,
                    {
                        "provider": "interactive",
                        "run_id": run_id,
                        "type": "run.created",
                        "payload": {"task": request.task, "cwd": request.cwd},
                        "metadata": request.metadata,
                    },
                )
                await coordinator._on_provider_event(
                    Method.PROVIDER_EVENT,
                    {
                        "provider": "interactive",
                        "run_id": run_id,
                        "type": "run.status",
                        "payload": {"status": "running"},
                        "metadata": request.metadata,
                    },
                )
                await coordinator._on_provider_event(
                    Method.PROVIDER_EVENT,
                    {
                        "provider": "interactive",
                        "run_id": run_id,
                        "type": "permission.requested",
                        "payload": {
                            "permissionRequest": {
                                "request_id": "native-approval-1",
                                "capability": "shell.execute",
                                "action": "execute_command",
                                "scope": [str(workspace)],
                                "reason": "Run the verification command.",
                                "options": ["allow_once", "deny"],
                                "diagnosticOnly": False,
                            }
                        },
                        "metadata": request.metadata,
                    },
                )
                permission = store.list_permission_requests(
                    item_id,
                    attempt_id=attempt_id,
                    status="pending",
                )[0]
                calls = []

                async def provider_permission(called_run_id, response):
                    calls.append((called_run_id, response))
                    return {"accepted": True}

                handler = WorkLedgerHandler(
                    coordinator,
                    provider_permission=provider_permission,
                )
                snapshot = coordinator.snapshot()
                resolved = await handler.handle(
                    Method.WORK_PERMISSION_RESOLVE,
                    {
                        "permissionRequestId": permission.request_id,
                        "workItemId": item_id,
                        "attemptId": attempt_id,
                        "revision": snapshot["revision"],
                        "decision": "allow_once",
                    },
                )

                assert resolved is not None and resolved["ok"] is True
                assert calls[0][0] == run_id
                assert calls[0][1].request_id == "native-approval-1"
                assert calls[0][1].allow is True
                assert store.get_permission_request(permission.request_id).status == "allowed"  # type: ignore[union-attr]
                assert store.get_attempt(attempt_id).execution_status == "running"  # type: ignore[union-attr]

    asyncio.run(run())


def test_terminal_attempt_expires_only_provider_permission_checkpoints() -> None:
    """A dead Provider approval must not survive as actionable Work state.

    Product permissions are intentionally different: an export approval may
    remain pending after execution because it authorizes a Host side effect.
    """

    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_handler_terminal_permission_") as temp:
            root = Path(temp)
            workspace = root / "project"
            workspace.mkdir()
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(store)
                request, item_id, attempt_id = _prepare(
                    coordinator,
                    cwd=workspace,
                    task="Build and verify the game.",
                    provider="interactive",
                )
                run_id = "interactive_terminal_permission"
                await coordinator._on_provider_event(
                    Method.PROVIDER_EVENT,
                    {
                        "provider": "interactive",
                        "run_id": run_id,
                        "type": "run.created",
                        "payload": {"task": request.task, "cwd": request.cwd},
                        "metadata": request.metadata,
                    },
                )
                await coordinator._on_provider_event(
                    Method.PROVIDER_EVENT,
                    {
                        "provider": "interactive",
                        "run_id": run_id,
                        "type": "permission.requested",
                        "payload": {
                            "permissionRequest": {
                                "request_id": "native-terminal-approval",
                                "capability": "shell.execute",
                                "action": "execute_command",
                                "scope": [str(workspace)],
                                "reason": "Run the verification command.",
                                "options": ["allow_once", "deny"],
                                "diagnosticOnly": False,
                            }
                        },
                        "metadata": request.metadata,
                    },
                )
                product_permission = store.create_permission_request(
                    item_id,
                    attempt_id=attempt_id,
                    capability="external.export",
                    action="copy_to_desktop",
                    scope_paths=[str(root / "Desktop" / "game.html")],
                    reason="Export the completed artifact.",
                    reversibility="replaceable",
                    options=["allow_once", "deny"],
                    idempotency_key="host-product-permission",
                    metadata={"kind": "desktop_export"},
                )

                await coordinator._on_provider_result(
                    Method.PROVIDER_RESULT,
                    {
                        "provider": "interactive",
                        "run_id": run_id,
                        "status": "done",
                        "result": "The game was created and verified.",
                        "error": "",
                        "metadata": request.metadata,
                    },
                )

                permissions = store.list_permission_requests(
                    item_id,
                    attempt_id=attempt_id,
                )
                provider_permission = next(
                    permission
                    for permission in permissions
                    if permission.metadata.get("kind") == "provider_permission"
                )
                assert provider_permission.status == "expired"
                assert provider_permission.metadata["resolution"] == "attempt_terminal"
                assert store.get_permission_request(product_permission.request_id).status == "pending"  # type: ignore[union-attr]

                # Startup recovery is idempotent and closes the same class of
                # stale checkpoint left by a crash after terminal persistence.
                stale = store.create_permission_request(
                    item_id,
                    attempt_id=attempt_id,
                    capability="shell.execute",
                    action="execute_command",
                    scope_paths=[str(workspace)],
                    reason="Late replay of a native approval.",
                    reversibility="unknown",
                    options=["allow_once", "deny"],
                    idempotency_key="late-provider-permission",
                    metadata={"kind": "provider_permission"},
                )
                assert coordinator._expire_terminal_provider_permissions() == 1
                assert store.get_permission_request(stale.request_id).status == "expired"  # type: ignore[union-attr]
                assert store.get_permission_request(product_permission.request_id).status == "pending"  # type: ignore[union-attr]

    asyncio.run(run())


def test_keeping_a_draft_as_a_project_is_gated_by_the_projection() -> None:
    """The surface may only do what the projection says it may.

    Promotion is the one action the voice path deliberately does not offer, so
    this boundary is the whole of its authorisation: re-deriving the condition
    inside the handler would let the button and the rule drift apart.
    """

    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_handler_promote_") as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            previous_scratch = settings.WORK_SCRATCH_ROOT
            settings.WORK_SCRATCH_ROOT = str(root / "scratch")
            try:
                with WorkLedgerStore(root / "ledger.sqlite3") as store, patch(
                    "server.work_ledger_coordinator.cwd_in_project_registry",
                    return_value=True,
                ):
                    coordinator = WorkLedgerCoordinator(store)
                    _, project_item, _ = _prepare(
                        coordinator, cwd=project, task="Work on a real project"
                    )
                    draft_request = ProviderRunRequest(
                        provider="codex",
                        task="Build a chess game",
                        cwd="",
                        mode="agent",
                        metadata={
                            "source": "handler-test",
                            "provider_manifest": CODEX_APP_SERVER_MANIFEST.to_dict(),
                        },
                    )
                    coordinator.prepare_request(draft_request)
                    draft_item = str(draft_request.metadata["work"]["work_item_id"])
                    draft_record = store.get_work_item(draft_item)
                    assert draft_record is not None
                    coordinator.bind_session_context(
                        "draft-session",
                        draft_record.project_id,
                        work_item_id=draft_item,
                        source="test",
                    )
                    draft_context = coordinator.conversation_binding("draft-session")
                    assert draft_context is not None
                    assert draft_context["projectId"] == ""
                    assert draft_context["canPromoteToProject"] is True

                    # A later task in the same Draft may be the Session's
                    # foreground context when the user promotes an earlier app.
                    # The whole Draft workspace and its original conversation
                    # must still become one Project.
                    sibling = store.create_work_item(
                        draft_record.project_id,
                        title="Polish the chess game",
                        goal="Keep iterating in the same Draft workspace.",
                        workspace_path=draft_record.workspace_path,
                    )
                    coordinator.bind_session_context(
                        "draft-session",
                        sibling.project_id,
                        work_item_id=sibling.work_item_id,
                        source="test.sibling",
                    )

                    handler = WorkLedgerHandler(coordinator, provider_run=FakeProviderRun(coordinator))
                    router = CanvasActionRouter(work_action=handler.route_action)

                    stale = await router.route(
                        {
                            "target": "work_item",
                            "action": "promote_to_project",
                            "workItemId": draft_item,
                            "revision": "stale-revision",
                        }
                    )
                    assert stale["ok"] is False and stale["error"] == "stale_revision"

                    refused = await router.route(
                        {
                            "target": "work_item",
                            "action": "promote_to_project",
                            "workItemId": project_item,
                            "revision": coordinator.snapshot()["revision"],
                        }
                    )
                    assert refused["ok"] is False
                    assert refused["error"] == "work_action_not_available"

                    with patch(
                        "core.session_manager.get_current_session_id",
                        return_value="draft-session",
                    ):
                        kept = await router.route(
                            {
                                "target": "work_item",
                                "action": "promote_to_project",
                                "workItemId": draft_item,
                                "revision": coordinator.snapshot()["revision"],
                            }
                        )
                    assert kept["ok"] is True
                    assert kept["promoted"]["workItemId"] == draft_item
                    assert store.get_project_by_path(
                        kept["promoted"]["workspacePath"]
                    ) is not None
                    promoted_binding = store.get_conversation_binding("draft-session")
                    assert promoted_binding is not None
                    assert promoted_binding.project_id == kept["promoted"]["projectId"]
                    assert promoted_binding.anchor_work_item_id == sibling.work_item_id
                    assert store.get_work_item(sibling.work_item_id).project_id == kept["promoted"]["projectId"]  # type: ignore[union-attr]

                    # Offered once. The action is gone now that it is a project.
                    again = await router.route(
                        {
                            "target": "work_item",
                            "action": "promote_to_project",
                            "workItemId": draft_item,
                            "revision": coordinator.snapshot()["revision"],
                        }
                    )
                    assert again["ok"] is False
                    assert again["error"] == "work_action_not_available"
            finally:
                settings.WORK_SCRATCH_ROOT = previous_scratch

    asyncio.run(run())


def test_the_slice_can_pick_a_finished_task_back_up() -> None:
    """Reopen has always worked; only the desktop app could reach it.

    An accepted or archived task offered no action on the Slice at all, so
    finished work was a dead end on the surface the user actually looks at.
    """

    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_handler_reopen_") as temp:
            root = Path(temp)
            (root / "project").mkdir()
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(store)
                initial, item_id, _ = _prepare(
                    coordinator, cwd=root / "project", task="Durable task"
                )
                await _finish(coordinator, initial, "fake_initial")
                handler = WorkLedgerHandler(coordinator, provider_run=FakeProviderRun(coordinator))

                await handler.route_action(
                    {
                        "target": "work_item",
                        "action": "accept",
                        "workItemId": item_id,
                        "revision": coordinator.snapshot()["revision"],
                    }
                )
                assert store.get_work_item(item_id).state == "accepted"  # type: ignore[union-attr]

                reopened = await handler.route_action(
                    {
                        "target": "work_item",
                        "action": "reopen",
                        "workItemId": item_id,
                        "revision": coordinator.snapshot()["revision"],
                    }
                )
                assert reopened["ok"] is True
                assert store.get_work_item(item_id).state == "open"  # type: ignore[union-attr]

                # Already open: the projection stops offering it, and the
                # handler reads that same projection rather than re-deriving it.
                again = await handler.route_action(
                    {
                        "target": "work_item",
                        "action": "reopen",
                        "workItemId": item_id,
                        "revision": coordinator.snapshot()["revision"],
                    }
                )
                assert again["ok"] is False
                assert again["error"] == "work_action_not_available"

    asyncio.run(run())


def test_work_snapshot_exposes_current_session_as_read_only_presentation() -> None:
    with tempfile.TemporaryDirectory(prefix="work_handler_session_projection_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(
                store,
                current_session_id=lambda: "session-current",
            )
            snapshot = coordinator.snapshot()
            assert snapshot["currentSessionId"] == "session-current"
            assert coordinator._task_dock(snapshot)["currentSessionId"] == "session-current"


def _main() -> None:
    test_work_snapshot_exposes_current_session_as_read_only_presentation()
    test_keeping_a_draft_as_a_project_is_gated_by_the_projection()
    test_the_slice_can_pick_a_finished_task_back_up()
    test_canvas_route_binds_selection_focus_and_execution_to_canonical_task()
    test_accept_reopen_archive_and_continue_is_not_a_followup_path()
    test_start_retry_and_resume_have_distinct_attempt_semantics()
    test_resume_requires_live_checkpoint_and_rolls_back_failed_writer_lease()
    test_retry_amendments_compose_and_preserve_lineage()
    test_denied_codex_permission_requires_explicit_bounded_authorized_retry()
    test_active_provider_permission_resumes_through_the_runtime_contract()
    test_terminal_attempt_expires_only_provider_permission_checkpoints()
    print("ok: work ledger handler enforces canvas and task lifecycle boundaries")


if __name__ == "__main__":
    _main()
