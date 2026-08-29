"""Workspace-less providers stay inside the control plane without a fake cwd."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.provider_catalog import BROWSER_MANIFEST
from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_outcome import ProviderOutcomeEvidence
from agent_host.provider_types import ProviderRunRequest, ProviderRunResult
from agent_host.work_ledger_store import WorkLedgerStore
from server.work_ledger_coordinator import WorkLedgerCoordinator


class _BrowserProbeAdapter:
    provider_id = "browser"
    manifest = BROWSER_MANIFEST

    def __init__(self) -> None:
        self.requests: list[ProviderRunRequest] = []

    async def run(self, request, _run_id, _emit):
        self.requests.append(request)
        return ProviderRunResult(
            status="done",
            result="Opened Bilibili in the browser.",
            metadata={
                "result_type": "ok",
                "browser": {
                    "current_url": "https://www.bilibili.com/",
                    "page_title": "哔哩哔哩",
                },
            },
            outcome_evidence=ProviderOutcomeEvidence(
                facet="browser.page_state",
                operation="open",
                expected={"url": "https://www.bilibili.com/"},
                observed={
                    "url": "https://www.bilibili.com/",
                    "title": "哔哩哔哩",
                },
            ),
        )

    async def cancel(self, _run_id):
        return {"cancelled": True, "confirmed": True}


def _requirements() -> ProviderRequirements:
    return ProviderRequirements(
        task_kind="browser",
        workspace_access="none",
        workspace_ownership="none",
        steering="immediate",
    )


def test_workspace_less_provider_runs_without_repo_or_git_attribution() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="workspace_less_provider_") as temp:
            root = Path(temp)
            store = WorkLedgerStore(root / "ledger.sqlite3")
            coordinator = WorkLedgerCoordinator(store)
            runtime = ProviderRuntime()
            adapter = _BrowserProbeAdapter()
            runtime.register(adapter)
            runtime.set_request_preparer(coordinator.prepare_request)
            coordinator.configure()
            try:
                record = await runtime.start(
                    ProviderRunRequest(
                        provider="browser",
                        task="Open https://www.bilibili.com/",
                        mode="open",
                        requirements=_requirements(),
                        metadata={
                            "source": "workspace-less-test",
                            "session_id": "browser-session",
                            "browser_action": "open",
                        },
                    )
                )
                assert record.task_handle is not None
                await record.task_handle

                assert len(adapter.requests) == 1
                prepared = adapter.requests[0]
                assert prepared.cwd is None
                assert prepared.metadata["workspace_binding"] == {
                    "cwd": "",
                    "access": "none",
                    "ownership": "none",
                    "status": "not_required",
                    "source": "not_applicable",
                    "host_readable": False,
                    "host_writable": False,
                }
                work = prepared.metadata["work"]
                assert work["workspace_mode"] == "none"
                assert work["workspace_path"] == ""

                item = store.get_work_item(str(work["work_item_id"]))
                attempt = store.get_attempt(str(work["attempt_id"]))
                project = store.get_project(str(work["project_id"]))
                assert item is not None and item.workspace_mode == "none"
                assert item.workspace_path == ""
                assert item.workspace_identity.startswith("none:")
                assert attempt is not None and attempt.execution_status == "succeeded"
                assert store.get_writer_lease(attempt.attempt_id) is None
                assert "git_baseline" not in attempt.metadata
                assert "git_delta" not in attempt.metadata
                assert project is not None and project.state == "retired"
                assert project.metadata["routing_visible"] is False

                projected = coordinator._project_item(item)
                assert projected["workspaceMode"] == "none"
                assert projected["workspacePath"] == ""
                assert projected["workspaceExists"] is False
                assert projected["attention"] != "error"
            finally:
                coordinator.close()

    asyncio.run(run())


if __name__ == "__main__":
    test_workspace_less_provider_runs_without_repo_or_git_attribution()
    print("ok: workspace-less provider remains ledgered without a fake cwd")
