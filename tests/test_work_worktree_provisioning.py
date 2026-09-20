"""P1 worktree isolation intake tests.

The provisioner is stubbed; no test shells out to the real Codex CLI.
Runnable directly by tools/run_tests.py and compatible with pytest.
"""

from __future__ import annotations

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
from server.workspace_provisioner import WorkspaceProvisioningError
from server.work_ledger_coordinator import WorkLedgerCoordinator


class StubProvisioner:
    """Records ensure calls and hands out fresh fake worktree directories."""

    def __init__(
        self,
        root: Path,
        *,
        policy: str = "worktree",
        failure: WorkspaceProvisioningError | None = None,
    ) -> None:
        self.root = root
        self.policy = policy
        self.failure = failure
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        work_item_external_id: str,
        project_cwd: str,
        policy: str = "worktree",
        base_ref: str | None = None,
        name: str | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "work_item_external_id": work_item_external_id,
                "project_cwd": project_cwd,
                "policy": policy,
                "name": name,
            }
        )
        if self.failure is not None:
            raise self.failure
        index = len(self.calls)
        if self.policy == "worktree":
            cwd = self.root / f"worktree-{index}"
            cwd.mkdir(parents=True, exist_ok=True)
            git_branch: str | None = f"amadeus/wt-{index}"
            base = "main"
        else:
            cwd = Path(project_cwd)
            git_branch = None
            base = None
        return {
            "created": True,
            "workspace": {
                "allocationId": work_item_external_id,
                "backend": "stub-git-worktree",
                "cwd": str(cwd),
                "gitBranch": git_branch,
                "baseRef": base,
                "policy": self.policy,
                "exists": True,
            },
        }


def _prepare(
    coordinator: WorkLedgerCoordinator,
    *,
    cwd: Path,
    task: str,
    provider: str = "codex",
    mode: str = "agent",
    work_item_id: str = "",
    continuation: str = "",
) -> tuple[ProviderRunRequest, str, str]:
    metadata: dict[str, Any] = {"source": "test"}
    if provider == "codex":
        metadata["provider_manifest"] = CODEX_APP_SERVER_MANIFEST.to_dict()
    elif provider == "codex":
        metadata["provider_manifest"] = CODEX_APP_SERVER_MANIFEST.to_dict()
    if work_item_id:
        metadata["work"] = {"work_item_id": work_item_id}
    if continuation:
        metadata["continuation"] = continuation
        if continuation == "retry" and work_item_id:
            attempts = coordinator.store.list_attempts(work_item_id)
            if attempts:
                metadata["retry_of"] = attempts[-1].attempt_id
    request = ProviderRunRequest(
        provider=provider,
        task=task,
        cwd=str(cwd),
        mode=mode,
        metadata=metadata,
    )
    prepared = coordinator.prepare_request(request)
    work = prepared.metadata["work"]
    return prepared, str(work["work_item_id"]), str(work["attempt_id"])


def _with_flag(value: bool):
    class _Guard:
        def __enter__(self) -> None:
            self.previous = settings.WORK_WORKTREE_ISOLATION
            settings.WORK_WORKTREE_ISOLATION = value

        def __exit__(self, *_exc: Any) -> None:
            settings.WORK_WORKTREE_ISOLATION = self.previous

    return _Guard()


def _with_scratch_root(path: Path):
    """Keep scratch provisioning inside the test's temp directory.

    Unrouted work now lands in the scratch root, so a suite that left this
    pointing at the configured default would create directories inside the
    developer's own checkout every time it ran.
    """

    class _Guard:
        def __enter__(self) -> None:
            self.previous = settings.WORK_SCRATCH_ROOT
            settings.WORK_SCRATCH_ROOT = str(path)

        def __exit__(self, *_exc: Any) -> None:
            settings.WORK_SCRATCH_ROOT = self.previous

    return _Guard()


def test_flag_off_keeps_p0_intake_untouched() -> None:
    with tempfile.TemporaryDirectory(prefix="wt_flag_off_") as temp:
        root = Path(temp)
        project = root / "project"
        project.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            stub = StubProvisioner(root)
            coordinator = WorkLedgerCoordinator(store, workspace_provisioner=stub)
            with _with_flag(False):
                prepared, item_id, _attempt_id = _prepare(
                    coordinator, cwd=project, task="Write a feature"
                )
            assert stub.calls == [], "flag off must never call the provisioner"
            item = store.get_work_item(item_id)
            assert item is not None
            assert item.workspace_mode == "local"
            assert Path(item.workspace_path) == project
            policy = item.metadata["workspace_policy"]
            assert policy["decision"] == "reuse_supplied_workspace"
            assert policy["automatic_worktree"] is False
            assert Path(prepared.cwd) == project
    print("ok: flag off leaves the P0 local single-writer path byte-identical")


def test_flag_on_provisions_a_worktree_for_a_new_provider_write_task() -> None:
    with tempfile.TemporaryDirectory(prefix="wt_flag_on_") as temp:
        root = Path(temp)
        project = root / "project"
        project.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            stub = StubProvisioner(root)
            coordinator = WorkLedgerCoordinator(store, workspace_provisioner=stub)
            with _with_flag(True):
                prepared, item_id, _attempt_id = _prepare(
                    coordinator,
                    cwd=project,
                    task="Write a feature",
                    provider="codex",
                )
            assert len(stub.calls) == 1
            assert stub.calls[0]["work_item_external_id"] == item_id
            assert Path(stub.calls[0]["project_cwd"]) == project
            item = store.get_work_item(item_id)
            assert item is not None
            assert item.workspace_mode == "worktree"
            worktree = root / "worktree-1"
            assert Path(item.workspace_path) == worktree
            assert Path(prepared.cwd) == worktree
            assert item.branch == "amadeus/wt-1"
            policy = item.metadata["workspace_policy"]
            assert policy["decision"] == "ensured_worktree"
            assert policy["automatic_worktree"] is True
            recorded = item.metadata["workspace_allocation"]
            assert recorded["allocation_id"] == item_id
            assert recorded["backend"] == "stub-git-worktree"
            assert recorded["external_id"] == item_id
    print("ok: a new Provider write task runs in a Host worktree with full audit facts")


def test_two_write_tasks_on_one_project_no_longer_collide() -> None:
    with tempfile.TemporaryDirectory(prefix="wt_concurrent_") as temp:
        root = Path(temp)
        project = root / "project"
        project.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            stub = StubProvisioner(root)
            coordinator = WorkLedgerCoordinator(store, workspace_provisioner=stub)
            with _with_flag(False):
                _prepare(coordinator, cwd=project, task="First write task")
                try:
                    _prepare(coordinator, cwd=project, task="Second write task")
                    raise AssertionError(
                        "P0 must fail closed for a second writer on one root"
                    )
                except WorkLedgerConflict:
                    pass
        with WorkLedgerStore(root / "ledger2.sqlite3") as store:
            stub = StubProvisioner(root)
            coordinator = WorkLedgerCoordinator(store, workspace_provisioner=stub)
            with _with_flag(True):
                _first, first_item, _a1 = _prepare(
                    coordinator, cwd=project, task="First write task"
                )
                _second, second_item, _a2 = _prepare(
                    coordinator, cwd=project, task="Second write task"
                )
            assert first_item != second_item
            leases = store.list_writer_leases(active_only=True)
            assert len(leases) == 2
            assert leases[0].workspace_identity != leases[1].workspace_identity
    print("ok: worktree isolation unlocks two concurrent writers on one project")


def test_ensure_failure_fails_closed_without_creating_an_item() -> None:
    with tempfile.TemporaryDirectory(prefix="wt_fail_closed_") as temp:
        root = Path(temp)
        project = root / "project"
        project.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            stub = StubProvisioner(
                root,
                failure=WorkspaceProvisioningError("boom", code="worktree_failed"),
            )
            coordinator = WorkLedgerCoordinator(store, workspace_provisioner=stub)
            with _with_flag(True):
                try:
                    _prepare(coordinator, cwd=project, task="Write a feature")
                    raise AssertionError("ensure failure must refuse the write task")
                except WorkLedgerConflict as exc:
                    assert "worktree_failed" in str(exc)
            assert store.list_work_items() == []
            assert store.list_writer_leases(active_only=True) == []
    print("ok: ensure failure refuses the run instead of falling back silently")


def test_retry_reuses_the_worktree_without_a_second_ensure() -> None:
    with tempfile.TemporaryDirectory(prefix="wt_retry_") as temp:
        root = Path(temp)
        project = root / "project"
        project.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            stub = StubProvisioner(root)
            coordinator = WorkLedgerCoordinator(store, workspace_provisioner=stub)
            with _with_flag(True):
                _prepared, item_id, attempt_id = _prepare(
                    coordinator, cwd=project, task="Write a feature"
                )
                store.update_attempt(attempt_id, execution_status="failed")
                retried, retried_item, retried_attempt = _prepare(
                    coordinator,
                    cwd=project,
                    task="Write a feature",
                    work_item_id=item_id,
                    continuation="retry",
                )
            assert retried_item == item_id
            assert retried_attempt != attempt_id
            assert len(stub.calls) == 1, "retry must not re-ensure a workspace"
            assert Path(retried.cwd) == root / "worktree-1"
    print("ok: retry stays in the original worktree without re-provisioning")


def test_read_only_tasks_never_provision_but_provider_identity_does_not_bypass_policy() -> None:
    with tempfile.TemporaryDirectory(prefix="wt_skip_") as temp:
        root = Path(temp)
        project = root / "project"
        project.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            stub = StubProvisioner(root)
            coordinator = WorkLedgerCoordinator(store, workspace_provisioner=stub)
            with _with_flag(True):
                _prepare(coordinator, cwd=project, task="Inspect only", mode="plan")
                _prepare(
                    coordinator,
                    cwd=project,
                    task="Codex write",
                    provider="codex",
                )
            assert len(stub.calls) == 1
    print("ok: workspace policy follows write semantics instead of Provider identity")


def test_local_degradation_is_recorded_honestly() -> None:
    with tempfile.TemporaryDirectory(prefix="wt_degraded_") as temp:
        root = Path(temp)
        project = root / "project"
        project.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            stub = StubProvisioner(root, policy="local")
            coordinator = WorkLedgerCoordinator(store, workspace_provisioner=stub)
            with _with_flag(True):
                _prepared, item_id, _attempt_id = _prepare(
                    coordinator, cwd=project, task="Write in non-git project"
                )
            item = store.get_work_item(item_id)
            assert item is not None
            assert item.workspace_mode == "local"
            assert Path(item.workspace_path) == project
            policy = item.metadata["workspace_policy"]
            assert policy["decision"] == "ensured_local_degraded"
            assert policy["automatic_worktree"] is False
    print("ok: non-git degradation is recorded as local isolation, not a fake worktree")


def test_isolation_does_not_change_where_the_next_task_is_routed() -> None:
    """P1 work order section 11.3.

    Sections 5.5-5.7 each followed one task through its own life -- continuation,
    restart, two writers -- and none of them asked what the *next* task sees.
    That blind spot is where isolation actually broke: the first worktree turned
    every later instruction into an ambiguity the model could not resolve,
    because disambiguating it meant transcribing a workspace_ref (0/28).
    """

    with tempfile.TemporaryDirectory(prefix="wt_next_task_routing_") as temp:
        root = Path(temp)
        project = root / "project"
        project.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store, _with_scratch_root(
            root / "scratch"
        ):
            stub = StubProvisioner(root)
            coordinator = WorkLedgerCoordinator(store, workspace_provisioner=stub)
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):

                def route_of_a_new_instruction() -> dict[str, Any]:
                    context = coordinator.workspace_routing_context()
                    # The project is the only candidate throughout: worktrees
                    # are not destinations, and neither is the scratch root.
                    candidates = list(context.get("candidates") or [])
                    assert len(candidates) == 1, candidates
                    assert Path(candidates[0]["workspacePath"]) == project
                    route = coordinator.resolve_workspace_route({})
                    assert route["status"] == "resolved", route
                    assert route.get("reason") != "project_intent_required"
                    # An unnamed instruction is new work, so it never reaches
                    # the project -- that fallthrough is how a chess game got
                    # written into the user's own repository.
                    assert route["source"] == "scratch_default"
                    assert Path(route["cwd"]) != project
                    return route

                with _with_flag(False):
                    _prepare(coordinator, cwd=project, task="Write before isolation")
                baseline = route_of_a_new_instruction()

                with _with_flag(True):
                    _prepare(coordinator, cwd=project, task="First isolated write")
                    _prepare(coordinator, cwd=project, task="Second isolated write")

                # Two worktrees now exist and are recorded, so this is the exact
                # state that used to produce candidates=2 and then candidates=3.
                workspaces = {item.workspace_path for item in store.list_work_items()}
                assert len(workspaces) == 3, workspaces
                # Scratch is a project row, but never a routing candidate.
                assert len(store.list_projects()) == 2

                assert route_of_a_new_instruction() == baseline

                # Naming the project explicitly also lands at its root. New work
                # never inherits a past task's worktree -- which matters most
                # when task lookup fails to identify the task a follow-up meant.
                project_id = next(
                    project_row.project_id
                    for project_row in store.list_projects()
                    if Path(project_row.canonical_path) == project
                )
                named = coordinator.resolve_workspace_route({"project_id": project_id})
                assert named["status"] == "resolved"
                assert Path(named["cwd"]) == project
    print("ok: turning isolation on leaves the routing of the next task unchanged")


def main() -> None:
    test_flag_off_keeps_p0_intake_untouched()
    test_flag_on_provisions_a_worktree_for_a_new_provider_write_task()
    test_two_write_tasks_on_one_project_no_longer_collide()
    test_ensure_failure_fails_closed_without_creating_an_item()
    test_retry_reuses_the_worktree_without_a_second_ensure()
    test_read_only_tasks_never_provision_but_provider_identity_does_not_bypass_policy()
    test_local_degradation_is_recorded_honestly()
    test_isolation_does_not_change_where_the_next_task_is_routed()


if __name__ == "__main__":
    main()
