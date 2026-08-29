from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.adapters.direct_codex import DirectCodexAdapter
from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderRunRequest
from config import settings
from server.handlers.provider_handler import ProviderHandler
from server.protocol import Method


_FAKE_CODEX = r"""
import json
import pathlib
import sys
import time


def emit(value):
    print(json.dumps(value), flush=True)


if sys.argv[-1:] == ["--version"]:
    print("codex-cli test-1.0")
    sys.exit(0)

if sys.argv[-2:] == ["login", "status"]:
    if "FAIL_AUTH" in sys.argv:
        print("token=must-not-escape", file=sys.stderr)
        sys.exit(7)
    print("Logged in using test credentials")
    sys.exit(0)

prompt = sys.stdin.read()
emit({"type": "thread.started", "thread_id": "thread_fake"})
if "SILENCE" in prompt:
    time.sleep(0.16)
emit({"type": "turn.started"})

if "HANG" in prompt:
    time.sleep(30)

if "FAIL" in prompt:
    print("token=should-not-escape", file=sys.stderr, flush=True)
    print("sk-proj-direct-secret", file=sys.stderr, flush=True)
    emit({"type": "error", "message": "fake codex failure"})
    sys.exit(2)

message = "Direct Codex completed."
if "WRITE" in prompt:
    message = (
        "[PROGRESS:DESIGN] Keep the output small so the user can inspect it.\n"
        "[PROGRESS:CAPABILITY] The requested text output is now available.\n"
        + message
    )
emit({
    "type": "item.completed",
    "item": {"id": "message_1", "type": "agent_message", "text": message},
})
if "WRITE" in prompt:
    target = pathlib.Path.cwd() / "direct.txt"
    target.write_text("DIRECT_OK\n", encoding="utf-8")
    native_path = str(target)
    emit({
        "type": "item.started",
        "item": {
            "id": "file_1",
            "type": "file_change",
            "changes": [{"path": native_path, "kind": "add"}],
            "status": "in_progress",
        },
    })
    emit({
        "type": "item.completed",
        "item": {
            "id": "file_1",
            "type": "file_change",
            "changes": [{"path": native_path, "kind": "add"}],
            "status": "completed",
        },
    })
if "COMMAND" in prompt:
    emit({
        "type": "item.started",
        "item": {
            "id": "command_1",
            "type": "command_execution",
            "command": "python verify.py",
            "aggregated_output": "",
            "exit_code": None,
            "status": "in_progress",
        },
    })
    emit({
        "type": "item.completed",
        "item": {
            "id": "command_1",
            "type": "command_execution",
            "command": "python verify.py",
            "aggregated_output": "verified",
            "exit_code": 0,
            "status": "completed",
        },
    })
print("authorization=secret-value", file=sys.stderr, flush=True)
emit({
    "type": "turn.completed",
    "usage": {"input_tokens": 10, "output_tokens": 4},
})
"""


def _fake_cli(root: Path) -> Path:
    path = root / "fake_codex.py"
    path.write_text(textwrap.dedent(_FAKE_CODEX), encoding="utf-8")
    return path


def _request(cwd: Path, task: str, *, write: bool) -> ProviderRunRequest:
    return ProviderRunRequest(
        provider="codex",
        task=task,
        cwd=str(cwd),
        requirements=ProviderRequirements(
            task_kind="workspace_mutation" if write else "workspace_read",
            workspace_access="write" if write else "read",
            preferred_provider="codex",
            preference_policy="require",
        ),
    )


def test_manifest_and_command_are_narrow() -> None:
    manifest = DirectCodexAdapter.manifest
    assert manifest.provider_id == "codex"
    assert manifest.capabilities.workspace_ownership == "caller"
    assert manifest.capabilities.durability == "turn"
    assert manifest.capabilities.resume == "none"
    assert manifest.capabilities.interaction == "none"
    assert manifest.capabilities.cancellation == "confirmed"

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-command-") as temp_dir:
        root = Path(temp_dir)
        adapter = DirectCodexAdapter(
            cli_path="codex-test",
            prefix_args=("--wrapper",),
            ignore_user_config=True,
        )
        write_command = adapter._build_command(root, sandbox="workspace-write")
        assert write_command[:3] == ["codex-test", "--wrapper", "exec"]
        assert "--json" in write_command
        assert "--ephemeral" in write_command
        assert "--ignore-user-config" in write_command
        assert write_command[-5:] == ["--sandbox", "workspace-write", "-C", str(root), "-"]


def test_startup_readiness_checks_transport_auth_and_redacts_failure() -> None:
    with tempfile.TemporaryDirectory(prefix="amadeus-codex-readiness-") as temp_dir:
        root = Path(temp_dir)
        cli = _fake_cli(root)
        ready = DirectCodexAdapter(
            cli_path=sys.executable,
            prefix_args=(str(cli),),
        ).startup_readiness(timeout_s=2)
        assert ready["ready"] is True
        assert ready["reason"] == "ready"
        assert ready["version"] == "codex-cli test-1.0"
        assert ready["authentication"] == "available"
        assert "credentials" not in str(ready)

        unavailable = DirectCodexAdapter(
            cli_path=sys.executable,
            prefix_args=(str(cli), "FAIL_AUTH"),
        ).startup_readiness(timeout_s=2)
        assert unavailable["ready"] is False
        assert unavailable["reason"] == "authentication_unavailable"
        assert unavailable["authentication"] == "unavailable"
        assert "must-not-escape" not in unavailable["diagnostic"]
        assert "[REDACTED]" in unavailable["diagnostic"]

        missing = DirectCodexAdapter(
            cli_path=str(root / "missing-codex"),
        ).startup_readiness(timeout_s=2)
        assert missing["ready"] is False
        assert missing["reason"] == "cli_start_failed"


def test_normal_bootstrap_registers_only_a_ready_direct_codex() -> None:
    async def listed(handler: ProviderHandler) -> dict:
        result = await handler.handle(Method.PROVIDER_LIST, {})
        assert isinstance(result, dict)
        return result

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-bootstrap-") as temp_dir:
        root = Path(temp_dir)
        cli = _fake_cli(root)
        common = (
            patch.object(settings, "DIRECT_CODEX_PROVIDER_ENABLED", True),
            patch.object(settings, "CODEX_APP_SERVER_PROVIDER_ENABLED", False),
            patch.object(settings, "DIRECT_CODEX_CLI_PATH", sys.executable),
        )

        unavailable_runtime = ProviderRuntime()
        with (
            common[0],
            common[1],
            common[2],
            patch.object(
                settings,
                "DIRECT_CODEX_CLI_PREFIX_ARGS",
                f'"{cli}" FAIL_AUTH',
            ),
            patch("server.handlers.provider_handler.runtime", unavailable_runtime),
        ):
            handler = ProviderHandler()
            snapshot = asyncio.run(listed(handler))
        codex = next(
            item
            for item in snapshot["provider_availability"]
            if item["provider_id"] == "codex"
        )
        assert codex["configured"] is True
        assert codex["ready"] is False
        assert codex["registered"] is False
        assert codex["reason"] == "authentication_unavailable"
        assert "codex" not in snapshot["providers"]

        ready_runtime = ProviderRuntime()
        with (
            patch.object(settings, "DIRECT_CODEX_PROVIDER_ENABLED", True),
            patch.object(settings, "CODEX_APP_SERVER_PROVIDER_ENABLED", False),
            patch.object(settings, "DIRECT_CODEX_CLI_PATH", sys.executable),
            patch.object(settings, "DIRECT_CODEX_CLI_PREFIX_ARGS", f'"{cli}"'),
            patch("server.handlers.provider_handler.runtime", ready_runtime),
        ):
            handler = ProviderHandler()
            snapshot = asyncio.run(listed(handler))
        codex = next(
            item
            for item in snapshot["provider_availability"]
            if item["provider_id"] == "codex"
        )
        assert codex["ready"] is True
        assert codex["registered"] is True
        assert codex["reason"] == "ready"
        assert "codex" in snapshot["providers"]


def test_success_maps_native_events_and_preserves_workspace() -> None:
    async def scenario(root: Path) -> None:
        subprocess.run(["git", "init", "--quiet"], cwd=str(root), check=True)
        adapter = DirectCodexAdapter(
            cli_path=sys.executable,
            prefix_args=(_fake_cli(root),),
            timeout_s=5,
            silence_warn_s=1,
        )
        events = []

        async def emit(event) -> None:
            events.append(event)

        result = await adapter.run(
            _request(root, "WRITE COMMAND", write=True),
            "codex_test_success",
            emit,
        )
        assert result.status == "done"
        assert result.result == "Direct Codex completed."
        assert (root / "direct.txt").read_text(encoding="utf-8") == "DIRECT_OK\n"
        event_types = [event.type for event in events]
        assert "assistant.delta" in event_types
        milestones = [event.payload for event in events if event.type == "semantic.progress"]
        assert [item["milestone"] for item in milestones] == ["design", "capability"]
        assert all(item["verified"] is False for item in milestones)
        assistant_text = "".join(
            str(event.payload.get("text") or "")
            for event in events
            if event.type == "assistant.delta"
        )
        assert "[PROGRESS:" not in assistant_text
        assert "tool.call" in event_types
        assert "tool.result" in event_types
        assert "artifact.created" in event_types
        assert any(
            event.type == "run.status"
            and event.payload.get("stage") == "turn_completed"
            and event.payload.get("status") == "running"
            for event in events
        )
        assert all(
            event.metadata["codex"]["thread_id"] == "thread_fake"
            for event in events[1:]
        )
        codex = result.metadata["codex"]
        assert codex["cwd"] == str(root.resolve())
        assert codex["sandbox"] == "workspace-write"
        assert codex["usage"] == {"input_tokens": 10, "output_tokens": 4}
        assert "secret-value" not in codex["diagnostics"]
        assert "[REDACTED]" in codex["diagnostics"]
        assert codex["workspace_changed"] is True

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-success-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_read_request_uses_read_only_and_failure_is_not_completion() -> None:
    async def scenario(root: Path) -> None:
        adapter = DirectCodexAdapter(
            cli_path=sys.executable,
            prefix_args=(_fake_cli(root),),
            timeout_s=5,
            silence_warn_s=1,
        )
        assert adapter._sandbox_for(_request(root, "FAIL", write=False)) == "read-only"
        events = []

        async def emit(event) -> None:
            events.append(event)

        result = await adapter.run(
            _request(root, "FAIL", write=False),
            "codex_test_failure",
            emit,
        )
        assert result.status == "error"
        assert result.error == "fake codex failure"
        assert any(event.type == "run.failed" for event in events)
        diagnostics = result.metadata["codex"]["diagnostics"]
        assert "should-not-escape" not in diagnostics
        assert "sk-proj-direct-secret" not in diagnostics
        assert "[REDACTED]" in diagnostics

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-failure-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_silence_is_visible_and_recovery_is_explicit() -> None:
    async def scenario(root: Path) -> None:
        adapter = DirectCodexAdapter(
            cli_path=sys.executable,
            prefix_args=(_fake_cli(root),),
            timeout_s=5,
            silence_warn_s=0.05,
        )
        events = []

        async def emit(event) -> None:
            events.append(event)

        result = await adapter.run(
            _request(root, "SILENCE", write=False),
            "codex_test_silence",
            emit,
        )
        assert result.status == "done"
        statuses = [event.payload for event in events if event.type == "run.status"]
        assert any(payload.get("liveness") == "stalled" for payload in statuses)
        assert any(
            payload.get("liveness") == "active" and payload.get("recovered") is True
            for payload in statuses
        )

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-silence-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_mutation_without_workspace_change_is_an_error() -> None:
    async def scenario(root: Path) -> None:
        subprocess.run(["git", "init", "--quiet"], cwd=str(root), check=True)
        adapter = DirectCodexAdapter(
            cli_path=sys.executable,
            prefix_args=(_fake_cli(root),),
            timeout_s=5,
            silence_warn_s=1,
        )

        async def emit(_event) -> None:
            return None

        result = await adapter.run(
            _request(root, "MUTATION WITHOUT OUTPUT", write=True),
            "codex_test_no_change",
            emit,
        )
        assert result.status == "error"
        assert "without an observable workspace change" in str(result.error)
        assert result.metadata["codex"]["workspace_changed"] is False

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-no-change-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_cancel_confirms_process_tree_exit() -> None:
    async def scenario(root: Path) -> None:
        adapter = DirectCodexAdapter(
            cli_path=sys.executable,
            prefix_args=(_fake_cli(root),),
            timeout_s=60,
            silence_warn_s=1,
        )

        async def emit(_event) -> None:
            return None

        run_task = asyncio.create_task(
            adapter.run(
                _request(root, "HANG", write=False),
                "codex_test_cancel",
                emit,
            )
        )
        for _ in range(100):
            if "codex_test_cancel" in adapter._processes:
                break
            await asyncio.sleep(0.01)
        outcome = await adapter.cancel("codex_test_cancel")
        assert outcome["confirmed"] is True
        assert outcome["cancelled"] is True
        result = await asyncio.wait_for(run_task, timeout=5)
        assert result.status == "error"

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-cancel-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def _main() -> None:
    test_manifest_and_command_are_narrow()
    test_startup_readiness_checks_transport_auth_and_redacts_failure()
    test_normal_bootstrap_registers_only_a_ready_direct_codex()
    test_success_maps_native_events_and_preserves_workspace()
    test_read_request_uses_read_only_and_failure_is_not_completion()
    test_silence_is_visible_and_recovery_is_explicit()
    test_mutation_without_workspace_change_is_an_error()
    test_cancel_confirms_process_tree_exit()
    print("ok: Direct Codex adapter preserves the Provider contract")


if __name__ == "__main__":
    _main()
