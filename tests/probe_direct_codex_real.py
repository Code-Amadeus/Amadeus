r"""Opt-in real Direct Codex conformance probe.

This file intentionally does not match ``test_*.py``.  It spends a real Codex
turn and must be invoked explicitly:

    uv run --locked --no-sync python -X utf8 tests\probe_direct_codex_real.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as settings
from agent_host.adapters.direct_codex import DirectCodexAdapter
from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderRunRequest
from agent_host.work_ledger_store import WorkLedgerStore
from server.event_bus import bus
from server.handlers.work_activity_handler import WorkActivityCoordinator
from server.protocol import Method
from server.work_ledger_coordinator import WorkLedgerCoordinator


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


async def _run_probe(root: Path, state_root: Path) -> dict:
    previous_allowlist = settings.WORK_PROJECT_ALLOWLIST
    previous_scratch_root = settings.WORK_SCRATCH_ROOT
    # The product destination registry still reads this deployment setting.
    # Point it only at the disposable repository so the real route crosses the
    # same trust check as production without touching a user repository.
    settings.WORK_PROJECT_ALLOWLIST = str(root)
    settings.WORK_SCRATCH_ROOT = str(state_root / "scratch")
    store = WorkLedgerStore(state_root / "work.sqlite3")
    coordinator = WorkLedgerCoordinator(store)
    coordinator.configure()
    project = store.create_or_get_project(root, name="direct-codex-probe")
    session_id = "direct-codex-real-product-route"
    coordinator.set_session_project(session_id, project.project_id)
    activity = WorkActivityCoordinator()
    activity.configure()
    runtime = ProviderRuntime()
    configured_cli = str(os.environ.get("DIRECT_CODEX_REAL_CLI_PATH") or "").strip()
    adapter = DirectCodexAdapter(
        cli_path=configured_cli or ("npx.cmd" if os.name == "nt" else "npx"),
        prefix_args=() if configured_cli else ("-y", "@openai/codex"),
        timeout_s=600,
        silence_warn_s=20,
        # Codex CLI 0.146.0 on native Windows downgrades write runs to
        # read-only when --ignore-user-config removes repository trust.
        ignore_user_config=False,
    )
    runtime.register(adapter)
    runtime.set_request_preparer(coordinator.prepare_request)

    provider_events: list[dict] = []
    provider_results: list[dict] = []
    activities: list[dict] = []
    canvases: list[dict] = []

    async def capture_provider_event(_method: str, params: dict) -> None:
        if params.get("provider") == "codex":
            provider_events.append(dict(params))

    async def capture_provider_result(_method: str, params: dict) -> None:
        if params.get("provider") == "codex":
            provider_results.append(dict(params))

    async def capture_activity(_method: str, params: dict) -> None:
        activities.append(dict(params))

    async def capture_canvas(_method: str, params: dict) -> None:
        canvases.append(dict(params))

    bus.on(Method.PROVIDER_EVENT, capture_provider_event)
    bus.on(Method.PROVIDER_RESULT, capture_provider_result)
    bus.on(Method.WALLPAPER_ACTIVITY, capture_activity)
    bus.on(Method.WALLPAPER_CANVAS, capture_canvas)
    try:
        requirements = ProviderRequirements(
            task_kind="workspace_mutation",
            workspace_access="write",
            preferred_provider="codex",
            preference_policy="require",
        )
        record = await runtime.start(
            ProviderRunRequest(
                provider="codex",
                task=(
                    "In this disposable Git repository, create only direct_codex_probe.txt "
                    "containing exactly DIRECT_CODEX_MATRIX_OK followed by a newline. "
                    "Use one PowerShell command if the file tool is unavailable. Verify the "
                    "content, then finish with the exact token DIRECT_CODEX_MATRIX_OK."
                ),
                mode="agent",
                requirements=requirements,
                ownership="managed",
                metadata={
                    "source": "direct_codex_real_probe",
                    "session_id": session_id,
                    "provider_selection": {
                        "provider_id": "codex",
                        "reason": "required_provider",
                        "compatible_candidates": ["codex"],
                    },
                },
            )
        )
        if record.task_handle is None:
            raise AssertionError("runtime did not start Direct Codex")
        await record.task_handle

        target = root / "direct_codex_probe.txt"
        assert record.status == "done", record.to_dict()
        if not target.is_file():
            diagnostic = {
                "record": record.to_dict(),
                "event_types": [event.get("type") for event in provider_events],
                "tool_events": [
                    event
                    for event in provider_events
                    if event.get("type") in {"tool.call", "tool.result", "run.failed"}
                ][-12:],
                "git_status": _git(root, "status", "--short"),
            }
            raise AssertionError(
                "Direct Codex reported completion without the requested file:\n"
                + json.dumps(diagnostic, ensure_ascii=False, indent=2)
            )
        assert target.read_text(encoding="utf-8") == "DIRECT_CODEX_MATRIX_OK\n"
        git_top = Path(_git(root, "rev-parse", "--show-toplevel")).resolve()
        assert git_top == root.resolve()
        git_status = _git(root, "status", "--short")
        assert "direct_codex_probe.txt" in git_status
        assert provider_results and provider_results[-1]["status"] == "done"

        attempts = store.list_attempts(store.list_work_items(limit=10)[0].work_item_id)
        assert len(attempts) == 1
        attempt = attempts[0]
        assert attempt.provider == "codex"
        assert attempt.execution_status == "succeeded"
        assert record.metadata.get("workspace_routing_source") == "session_project"
        assert record.metadata.get("work", {}).get("project_id") == project.project_id
        completions = store.list_completions(attempt.work_item_id)
        assert completions
        assert completions[-1].execution_status == "succeeded"

        assert any(event.get("type") == "assistant.delta" for event in provider_events)
        assert any(event.get("type") == "tool.call" for event in provider_events)
        assert any(
            event.get("type") == "run.finished"
            and event.get("payload", {}).get("status") == "done"
            for event in provider_events
        )
        assert canvases
        assert any(entry.get("activity") == "work" for entry in activities)
        await activity.release_work_presentation(
            record.run_id,
            reason="direct_codex_real_probe_observer_terminal",
        )
        assert activities[-1].get("activity") == ""

        result_metadata = provider_results[-1].get("metadata", {}).get("codex", {})
        assert Path(result_metadata.get("cwd", "")).resolve() == root.resolve()
        assert result_metadata.get("sandbox") == "workspace-write"
        assert result_metadata.get("isolated_user_config") is False
        return {
            "checks": {
                "actual_file": True,
                "provider_cwd": True,
                "git_ownership": True,
                "provider_completion": True,
                "ledger_completion": True,
                "canonical_events": True,
                "ui_projection": True,
                "bounded_provider_diagnostics": True,
                "product_project_route": True,
                "no_explicit_cwd": True,
            },
            "run_id": record.run_id,
            "thread_id": result_metadata.get("thread_id", ""),
            "event_count": len(provider_events),
            "canvas_count": len(canvases),
            "git_status": git_status,
            "result": record.result,
        }
    finally:
        bus.off(Method.PROVIDER_EVENT, capture_provider_event)
        bus.off(Method.PROVIDER_RESULT, capture_provider_result)
        bus.off(Method.WALLPAPER_ACTIVITY, capture_activity)
        bus.off(Method.WALLPAPER_CANVAS, capture_canvas)
        runtime.set_request_preparer(None)
        coordinator.close()
        settings.WORK_PROJECT_ALLOWLIST = previous_allowlist
        settings.WORK_SCRATCH_ROOT = previous_scratch_root


def main() -> None:
    external_workspace = str(os.environ.get("DIRECT_CODEX_REAL_WORKSPACE") or "").strip()
    if external_workspace:
        root = Path(external_workspace).resolve()
        state_root = Path(
            str(os.environ.get("DIRECT_CODEX_REAL_STATE") or (root.parent / "state"))
        ).resolve()
        if not root.is_dir() or not state_root.is_dir():
            raise RuntimeError("pre-created Direct Codex probe workspace/state directory is missing")
        _run_in_workspace(root, state_root)
        return

    with tempfile.TemporaryDirectory(prefix="amadeus-direct-codex-real-") as temp_dir:
        base = Path(temp_dir).resolve()
        root = base / "repo"
        root.mkdir()
        state_root = base / "state"
        state_root.mkdir()
        _run_in_workspace(root, state_root)


def _run_in_workspace(root: Path, state_root: Path) -> None:
    if any(root.iterdir()):
        raise RuntimeError(f"probe workspace must be empty: {root}")
    _git(root, "init", "--quiet")
    _git(root, "config", "user.email", "amadeus-probe@example.invalid")
    _git(root, "config", "user.name", "Amadeus Probe")
    seed = root / "README.md"
    seed.write_text("# Disposable Direct Codex probe\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "--quiet", "-m", "seed disposable probe")
    summary = asyncio.run(_run_probe(root, state_root))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("ok: real Direct Codex disposable-repository matrix passed")


if __name__ == "__main__":
    main()
