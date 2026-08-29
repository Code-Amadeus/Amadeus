"""Run isolated real turns through the official Codex SDK adapter."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent_host.adapters.codex_app_server import CodexAppServerAdapter
from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_types import (
    ProviderRunRequest,
    ProviderSessionHandle,
    ProviderSteerRequest,
)


def _request(
    workspace: Path,
    task: str,
    *,
    write: bool,
    session: ProviderSessionHandle | None = None,
) -> ProviderRunRequest:
    return ProviderRunRequest(
        provider="codex",
        task=task,
        cwd=str(workspace),
        session=session,
        requirements=ProviderRequirements(
            task_kind="workspace_mutation" if write else "workspace_read",
            workspace_access="write" if write else "read",
            preferred_provider="codex",
            preference_policy="require",
        ),
    )


def _initialize_workspace(workspace: Path) -> None:
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=str(workspace),
        check=True,
        stdin=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "config", "user.email", "amadeus-probe@example.invalid"],
        cwd=str(workspace),
        check=True,
        stdin=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "config", "user.name", "Amadeus Probe"],
        cwd=str(workspace),
        check=True,
        stdin=subprocess.DEVNULL,
    )


def _sandbox_accessible_workspace() -> Path:
    """Create an isolated directory with ordinary inherited Windows ACLs."""

    parent = Path(tempfile.gettempdir()).resolve()
    workspace = (parent / f"amadeus-codex-sdk-{uuid.uuid4().hex[:10]}").resolve()
    if os.name != "nt":
        workspace.mkdir(parents=False, exist_ok=False)
        return workspace
    env = os.environ.copy()
    env["AMADEUS_CODEX_PROBE_ROOT"] = str(workspace)
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "New-Item -ItemType Directory "
                "-Path $env:AMADEUS_CODEX_PROBE_ROOT "
                "-ErrorAction Stop | Out-Null"
            ),
        ],
        cwd=str(parent),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode or not workspace.is_dir():
        raise RuntimeError(
            result.stderr.strip() or f"failed to create probe workspace: {workspace}"
        )
    return workspace


async def _run_read(workspace: Path, timeout_s: float) -> dict:
    adapter = CodexAppServerAdapter(
        turn_timeout_s=timeout_s,
        approval_mode="deny_all",
    )
    events = []

    async def emit(event) -> None:
        events.append(event.to_dict())

    try:
        result = await adapter.run(
            _request(
                workspace,
                (
                    "Inspect this repository without changing any files. "
                    "Reply with exactly CODEX_SDK_READ_OK if it is an empty "
                    "Git working tree."
                ),
                write=False,
            ),
            "codex_sdk_live_read",
            emit,
        )
        entries = sorted(path.name for path in workspace.iterdir() if path.name != ".git")
        passed = (
            result.status == "done"
            and "CODEX_SDK_READ_OK" in result.result
            and not entries
            and result.session is not None
        )
        return {
            "status": "passed" if passed else "failed",
            "provider_status": result.status,
            "result": result.result,
            "error": result.error,
            "session": result.session.to_dict() if result.session else None,
            "event_types": [event["type"] for event in events],
            "codex": result.metadata.get("codex", {}),
            "workspace_entries": entries,
        }
    finally:
        await adapter.close()


async def _run_write_resume(workspace: Path, timeout_s: float) -> dict:
    first_adapter = CodexAppServerAdapter(
        turn_timeout_s=timeout_s,
        approval_mode="auto_review",
    )
    first_events = []

    async def emit_first(event) -> None:
        first_events.append(event.to_dict())

    first = await first_adapter.run(
        _request(
            workspace,
            (
                "Create counter.txt in this repository with exactly the text "
                "phase-one followed by one newline. Read it back to verify it."
            ),
            write=True,
        ),
        "codex_sdk_write_first",
        emit_first,
    )
    await first_adapter.close()
    first_content = (
        (workspace / "counter.txt").read_text(encoding="utf-8")
        if (workspace / "counter.txt").is_file()
        else ""
    )

    second_adapter = CodexAppServerAdapter(
        turn_timeout_s=timeout_s,
        approval_mode="auto_review",
    )
    second_events = []

    async def emit_second(event) -> None:
        second_events.append(event.to_dict())

    try:
        second = await second_adapter.run(
            _request(
                workspace,
                (
                    "Continue the same work. Change the file you created in the "
                    "previous turn so its entire content is exactly phase-two "
                    "followed by one newline, then read it back to verify it."
                ),
                write=True,
                session=first.session,
            ),
            "codex_sdk_write_resume",
            emit_second,
        )
        second_content = (
            (workspace / "counter.txt").read_text(encoding="utf-8")
            if (workspace / "counter.txt").is_file()
            else ""
        )
        first_codex = first.metadata.get("codex", {})
        second_codex = second.metadata.get("codex", {})
        passed = (
            first.status == "done"
            and second.status == "done"
            and first.session is not None
            and second.session == first.session
            and first_content == "phase-one\n"
            and second_content == "phase-two\n"
            and first_codex.get("thread_id") == second_codex.get("thread_id")
            and first_codex.get("turn_id") != second_codex.get("turn_id")
        )
        return {
            "status": "passed" if passed else "failed",
            "first": {
                "provider_status": first.status,
                "error": first.error,
                "content": first_content,
                "session": first.session.to_dict() if first.session else None,
                "event_types": [event["type"] for event in first_events],
                "codex": first_codex,
            },
            "second": {
                "provider_status": second.status,
                "error": second.error,
                "content": second_content,
                "session": second.session.to_dict() if second.session else None,
                "event_types": [event["type"] for event in second_events],
                "codex": second_codex,
            },
        }
    finally:
        await second_adapter.close()


async def _run_interrupt(workspace: Path, timeout_s: float) -> dict:
    adapter = CodexAppServerAdapter(
        turn_timeout_s=timeout_s,
        cancel_confirm_timeout_s=60,
        approval_mode="auto_review",
    )
    tool_started = asyncio.Event()
    events = []

    async def emit(event) -> None:
        events.append(event.to_dict())
        if event.type == "tool.call":
            tool_started.set()

    run_task = asyncio.create_task(
        adapter.run(
            _request(
                workspace,
                (
                    "Run a Python command that waits for 120 seconds. Only after "
                    "that command finishes, create never.txt containing done."
                ),
                write=True,
            ),
            "codex_sdk_interrupt",
            emit,
        )
    )
    try:
        await asyncio.wait_for(tool_started.wait(), timeout=min(timeout_s, 120))
        cancel = await adapter.cancel("codex_sdk_interrupt")
        result = await asyncio.wait_for(run_task, timeout=90)
        target_exists = (workspace / "never.txt").exists()
        passed = (
            cancel.get("confirmed") is True
            and cancel.get("cancelled") is True
            and result.status == "cancelled"
            and not target_exists
        )
        return {
            "status": "passed" if passed else "failed",
            "cancel": cancel,
            "provider_status": result.status,
            "error": result.error,
            "target_exists": target_exists,
            "event_types": [event["type"] for event in events],
            "codex": result.metadata.get("codex", {}),
        }
    finally:
        if not run_task.done():
            run_task.cancel()
        await adapter.close()


async def _run_timeout(workspace: Path, timeout_s: float) -> dict:
    adapter = CodexAppServerAdapter(
        turn_timeout_s=timeout_s,
        cancel_confirm_timeout_s=20,
        approval_mode="auto_review",
    )
    events = []

    async def emit(event) -> None:
        events.append(event.to_dict())

    started = time.monotonic()
    try:
        result = await adapter.run(
            _request(
                workspace,
                (
                    "Run a Python command that waits for 120 seconds. Only after "
                    "that command finishes, create timeout-never.txt containing done."
                ),
                write=True,
            ),
            "codex_sdk_timeout",
            emit,
        )
        elapsed_s = round(time.monotonic() - started, 3)
        target_exists = (workspace / "timeout-never.txt").exists()
        passed = (
            result.status == "error"
            and "timed out" in str(result.error or "").lower()
            and not target_exists
            and elapsed_s <= timeout_s + 25
        )
        return {
            "status": "passed" if passed else "failed",
            "provider_status": result.status,
            "error": result.error,
            "elapsed_s": elapsed_s,
            "configured_timeout_s": timeout_s,
            "target_exists": target_exists,
            "event_types": [event["type"] for event in events],
            "codex": result.metadata.get("codex", {}),
        }
    finally:
        await adapter.close()


async def _run_steer(workspace: Path, timeout_s: float) -> dict:
    adapter = CodexAppServerAdapter(
        turn_timeout_s=timeout_s,
        approval_mode="auto_review",
    )
    first_tool = asyncio.Event()
    events = []

    async def emit(event) -> None:
        events.append(event.to_dict())
        if event.type == "tool.call":
            first_tool.set()

    run_task = asyncio.create_task(
        adapter.run(
            _request(
                workspace,
                (
                    "Inspect the Git working tree first. Then create original.txt "
                    "containing ORIGINAL followed by one newline and verify it."
                ),
                write=True,
            ),
            "codex_sdk_steer",
            emit,
        )
    )
    try:
        await asyncio.wait_for(first_tool.wait(), timeout=min(timeout_s, 120))
        steer = await adapter.steer(
            "codex_sdk_steer",
            ProviderSteerRequest(
                task=(
                    "Replace the remaining plan: do not create original.txt. "
                    "Create steered.txt containing STEERED followed by one newline "
                    "and verify that instead."
                ),
                revision=1,
            ),
        )
        result = await asyncio.wait_for(run_task, timeout=timeout_s)
        original_exists = (workspace / "original.txt").exists()
        steered_content = (
            (workspace / "steered.txt").read_text(encoding="utf-8")
            if (workspace / "steered.txt").is_file()
            else ""
        )
        passed = (
            steer.get("accepted") is True
            and result.status == "done"
            and not original_exists
            and steered_content == "STEERED\n"
        )
        return {
            "status": "passed" if passed else "failed",
            "steer": steer,
            "provider_status": result.status,
            "error": result.error,
            "original_exists": original_exists,
            "steered_content": steered_content,
            "event_types": [event["type"] for event in events],
            "codex": result.metadata.get("codex", {}),
        }
    finally:
        if not run_task.done():
            run_task.cancel()
        await adapter.close()


async def _run(timeout_s: float, scenario: str) -> dict:
    workspace = _sandbox_accessible_workspace()
    try:
        _initialize_workspace(workspace)
        if scenario == "read":
            return await _run_read(workspace, timeout_s)
        if scenario == "write-resume":
            return await _run_write_resume(workspace, timeout_s)
        if scenario == "interrupt":
            return await _run_interrupt(workspace, timeout_s)
        if scenario == "timeout":
            return await _run_timeout(workspace, timeout_s)
        if scenario == "steer":
            return await _run_steer(workspace, timeout_s)
        raise ValueError(f"unsupported scenario: {scenario}")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument(
        "--scenario",
        choices=("read", "write-resume", "interrupt", "timeout", "steer"),
        default="read",
    )
    args = parser.parse_args()
    if not args.live:
        print(json.dumps({"status": "dry_run", "live": False}, indent=2))
        return 0
    report = asyncio.run(_run(max(1.0, args.timeout), args.scenario))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
