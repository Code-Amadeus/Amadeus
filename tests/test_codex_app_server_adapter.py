from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai_codex import Sandbox
from openai_codex.generated.v2_all import ReasoningEffort

from agent_host.adapters.codex_app_server import CodexAppServerAdapter
from agent_host.provider_bootstrap import builtin_provider_specs
from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_types import (
    ProviderRunRequest,
    ProviderPermissionResponse,
    ProviderSessionHandle,
    ProviderSteerRequest,
)


@dataclass(slots=True)
class _Notification:
    method: str
    payload: dict[str, Any]


class _FakeTurn:
    def __init__(
        self,
        turn_id: str,
        events: list[_Notification] | None = None,
        *,
        blocked: bool = False,
    ) -> None:
        self.id = turn_id
        self.events = list(events or [])
        self.blocked = blocked
        self.started = asyncio.Event()
        self.control_events: asyncio.Queue[_Notification] = asyncio.Queue()
        self.steers: list[str] = []
        self.interrupts = 0

    async def stream(self):
        self.started.set()
        for event in self.events:
            yield event
        if not self.blocked:
            return
        while True:
            event = await self.control_events.get()
            yield event
            if event.method == "turn/completed":
                return

    async def steer(self, task: str):
        self.steers.append(task)
        return {"turnId": self.id}

    async def interrupt(self):
        self.interrupts += 1
        await self.control_events.put(_turn_completed(self.id, "interrupted"))
        return {"turnId": self.id}


class _FakeThread:
    def __init__(self, thread_id: str, turn: _FakeTurn) -> None:
        self.id = thread_id
        self.next_turn = turn
        self.turn_calls: list[tuple[str, dict[str, Any]]] = []

    async def turn(self, task: str, **kwargs):
        self.turn_calls.append((task, dict(kwargs)))
        return self.next_turn


class _FakeCodex:
    def __init__(self, threads: list[_FakeThread]) -> None:
        self.threads = list(threads)
        self.starts: list[dict[str, Any]] = []
        self.resumes: list[tuple[str, dict[str, Any]]] = []
        self.injected_items: list[tuple[str, list[dict[str, Any]]]] = []
        self.names: list[tuple[str, str]] = []
        self.config_writes: list[tuple[list[dict[str, Any]], str]] = []

    async def thread_start(self, **kwargs):
        self.starts.append(dict(kwargs))
        return self.threads.pop(0)

    async def thread_resume(self, thread_id: str, **kwargs):
        self.resumes.append((thread_id, dict(kwargs)))
        thread = self.threads.pop(0)
        assert thread.id == thread_id
        return thread

    async def thread_inject_items(
        self,
        thread_id: str,
        items: list[dict[str, Any]],
    ) -> None:
        self.injected_items.append((thread_id, items))

    async def thread_set_name(self, thread_id: str, name: str) -> None:
        self.names.append((thread_id, name))

    async def config_batch_write(
        self,
        edits: list[dict[str, Any]],
        *,
        file_path: str,
    ) -> None:
        self.config_writes.append((edits, file_path))


def _turn_completed(turn_id: str, status: str) -> _Notification:
    return _Notification(
        "turn/completed",
        {"turn": {"id": turn_id, "status": status, "error": None}},
    )


def _success_events(turn_id: str) -> list[_Notification]:
    return [
        _Notification(
            "item/agentMessage/delta",
            {
                "delta": (
                    "[PROGRESS:DESIGN] Keep the example deliberately small.\n"
                    "I am working on it.\n"
                )
            },
        ),
        _Notification(
            "item/started",
            {
                "item": {
                    "id": "command-1",
                    "type": "commandExecution",
                    "command": "python verify.py",
                    "status": "inProgress",
                }
            },
        ),
        _Notification(
            "item/completed",
            {
                "item": {
                    "id": "command-1",
                    "type": "commandExecution",
                    "command": "python verify.py",
                    "aggregatedOutput": "verified",
                    "exitCode": 0,
                    "status": "completed",
                }
            },
        ),
        _Notification(
            "item/completed",
            {
                "item": {
                    "id": "file-1",
                    "type": "fileChange",
                    "changes": [{"path": "result.txt", "kind": "add"}],
                    "status": "completed",
                }
            },
        ),
        _Notification(
            "item/completed",
            {
                "item": {
                    "id": "message-1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "The requested result is ready.",
                }
            },
        ),
        _turn_completed(turn_id, "completed"),
    ]


def _request(
    root: Path,
    task: str,
    *,
    write: bool,
    session: ProviderSessionHandle | None = None,
) -> ProviderRunRequest:
    return ProviderRunRequest(
        provider="codex",
        task=task,
        cwd=str(root),
        session=session,
        requirements=ProviderRequirements(
            task_kind="workspace_mutation" if write else "workspace_read",
            workspace_access="write" if write else "read",
            preferred_provider="codex",
            preference_policy="require",
        ),
    )


def test_sdk_adapter_maps_thread_turn_events_and_terminal_truth() -> None:
    async def scenario(root: Path) -> None:
        turn = _FakeTurn("turn-1", _success_events("turn-1"))
        thread = _FakeThread("thread-1", turn)
        codex = _FakeCodex([thread])
        adapter = CodexAppServerAdapter(
            codex=codex,
            model="deepseek-v4-flash",
            model_provider="deepseek",
            reasoning_effort="max",
            service_tier="",
        )
        events = []

        async def emit(event) -> None:
            events.append(event)

        result = await adapter.run(
            _request(root, "Create result.txt", write=True),
            "run-1",
            emit,
        )

        assert result.status == "done"
        assert result.result == "The requested result is ready."
        assert result.session == ProviderSessionHandle(
            provider="codex",
            session_id="thread-1",
            scope="work_item",
        )
        assert result.metadata["codex"]["turn_id"] == "turn-1"
        assert result.metadata["codex"]["cwd"] == str(root.resolve())
        assert codex.starts[0]["cwd"] == str(root.resolve())
        assert codex.starts[0]["sandbox"] == Sandbox.workspace_write
        assert codex.starts[0]["approval_mode"] is None
        assert codex.starts[0]["model"] == "deepseek-v4-flash"
        assert codex.starts[0]["model_provider"] == "deepseek"
        task, kwargs = thread.turn_calls[0]
        assert task.startswith("Amadeus handoff")
        assert "Create result.txt" in task
        assert "[PROGRESS:VALIDATION]" not in task
        assert "Optional host authoring capability" not in task
        assert codex.names == [("thread-1", "Amadeus · Create result.txt")]
        assert codex.injected_items[0][0] == "thread-1"
        hidden = codex.injected_items[0][1][0]["content"][0]["text"]
        assert hidden.startswith("Amadeus execution context")
        assert "Create result.txt" in hidden
        assert "[PROGRESS:VALIDATION]" in hidden
        assert "Codex desktop handoff contract" in hidden
        assert "never end the turn after only DESIGN" in hidden
        assert kwargs["cwd"] == str(root.resolve())
        assert kwargs["sandbox"] == Sandbox.workspace_write
        assert kwargs["approval_mode"] is None
        assert kwargs["model"] == "deepseek-v4-flash"
        assert kwargs["effort"] == ReasoningEffort("max")
        assert result.metadata["codex"]["model"] == "deepseek-v4-flash"
        assert result.metadata["codex"]["model_provider"] == "deepseek"
        assert result.metadata["codex"]["reasoning_effort"] == "max"
        assert result.activity_evidence is not None
        assert result.activity_evidence.to_dict() == {
            "terminal_observed": True,
            "progress_milestones": 2,
            "execution_items": 2,
            "observation_authority": "host",
            "schema_version": 1,
        }
        event_types = [event.type for event in events]
        assert "assistant.delta" in event_types
        assert "semantic.progress" in event_types
        assert event_types.count("tool.call") == 1
        assert event_types.count("tool.result") == 2
        assert all(event.run_id == "run-1" for event in events)
        command_call = next(event for event in events if event.type == "tool.call")
        assert command_call.payload["tool"] == "shell"
        assert command_call.payload["command"] == "python verify.py"
        command_result = next(
            event
            for event in events
            if event.type == "tool.result" and event.payload["name"] == "shell"
        )
        assert command_result.payload["success"] is True
        assert command_result.payload["exit_code"] == 0
        file_result = next(
            event
            for event in events
            if event.type == "tool.result" and event.payload["name"] == "file_change"
        )
        assert file_result.payload["changes"] == [
            {"path": "result.txt", "kind": "add", "diff": ""}
        ]

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-sdk-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_native_plan_becomes_an_early_reported_design_milestone() -> None:
    async def scenario(root: Path) -> None:
        events = [
            _Notification(
                "item/plan/delta",
                {
                    "itemId": "plan-1",
                    "delta": "Keep receipt authority in the host and replan once after rejection.",
                },
            ),
            _Notification(
                "item/completed",
                {
                    "item": {
                        "id": "plan-1",
                        "type": "plan",
                        "text": "",
                    }
                },
            ),
            _Notification(
                "item/completed",
                {
                    "item": {
                        "id": "message-plan",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "Inspection complete.",
                    }
                },
            ),
            _turn_completed("turn-native-plan", "completed"),
        ]
        turn = _FakeTurn("turn-native-plan", events)
        adapter = CodexAppServerAdapter(codex=_FakeCodex([_FakeThread("thread-plan", turn)]))
        observed = []

        async def emit(event) -> None:
            observed.append(event)

        result = await adapter.run(
            _request(root, "Inspect the receipt design", write=False),
            "run-native-plan",
            emit,
        )

        assert result.status == "done"
        milestones = [event for event in observed if event.type == "semantic.progress"]
        assert len(milestones) == 1
        assert milestones[0].payload == {
            "milestone": "design",
            "summary": (
                "Keep receipt authority in the host and replan once after rejection."
            ),
            "source": "codex_native_plan",
            "explicit": False,
            "verified": False,
            "status": "reported",
        }
        assert result.activity_evidence is not None
        assert result.activity_evidence.progress_milestones == 1
        assert result.activity_evidence.execution_items == 0

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-plan-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_completed_codex_commentary_becomes_an_unverified_direction_update() -> None:
    async def scenario(root: Path) -> None:
        events = [
            _Notification(
                "item/completed",
                {
                    "item": {
                        "id": "message-direction",
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": (
                            "I am mapping the existing board into AUIP state, then I will "
                            "verify connected and standalone play."
                        ),
                    }
                },
            ),
            _Notification(
                "item/completed",
                {
                    "item": {
                        "id": "message-final",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "The integration is complete.",
                    }
                },
            ),
            _turn_completed("turn-direction", "completed"),
        ]
        adapter = CodexAppServerAdapter(
            codex=_FakeCodex([_FakeThread("thread-direction", _FakeTurn("turn-direction", events))])
        )
        observed = []

        async def emit(event) -> None:
            observed.append(event)

        result = await adapter.run(
            _request(root, "Connect the existing game", write=True),
            "run-direction",
            emit,
        )

        updates = [event for event in observed if event.type == "assistant.update"]
        assert len(updates) == 1
        assert updates[0].payload["status"] == "reported_direction"
        assert "then I will verify" in updates[0].payload["text"]
        assert result.result == "The integration is complete."

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-direction-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_dynamic_tool_title_becomes_a_bounded_reported_direction() -> None:
    async def scenario(root: Path) -> None:
        events = [
            _Notification(
                "item/started",
                {
                    "item": {
                        "id": "dynamic-1",
                        "type": "dynamicToolCall",
                        "tool": "js",
                        "arguments": {
                            "code": "writeWorkspaceFile()",
                            "title": "Updating the game for simultaneous two-player battle",
                            "timeout_ms": 30_000,
                        },
                        "status": "inProgress",
                    }
                },
            ),
            _Notification(
                "item/completed",
                {
                    "item": {
                        "id": "dynamic-1",
                        "type": "dynamicToolCall",
                        "tool": "js",
                        "status": "completed",
                        "output": "pvz.html updated",
                    }
                },
            ),
            _Notification(
                "item/completed",
                {
                    "item": {
                        "id": "message-dynamic",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "The requested update is staged.",
                    }
                },
            ),
            _turn_completed("turn-dynamic", "completed"),
        ]
        adapter = CodexAppServerAdapter(
            codex=_FakeCodex(
                [_FakeThread("thread-dynamic", _FakeTurn("turn-dynamic", events))]
            )
        )
        observed = []

        async def emit(event) -> None:
            observed.append(event)

        result = await adapter.run(
            _request(root, "Update the game", write=True),
            "run-dynamic",
            emit,
        )

        assert result.status == "done"
        updates = [event for event in observed if event.type == "assistant.update"]
        assert len(updates) == 1
        assert updates[0].payload == {
            "text": "Updating the game for simultaneous two-player battle",
            "source": "codex_native_tool_title",
            "explicit": False,
            "status": "reported_direction",
        }
        call = next(event for event in observed if event.type == "tool.call")
        assert call.payload["name"] == "js"
        assert call.payload["title"] == updates[0].payload["text"]
        assert call.payload["input"]["code"] == "writeWorkspaceFile()"
        assert not any(event.type == "semantic.progress" for event in observed)

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-tool-title-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_completed_final_message_does_not_replay_streamed_progress_kinds() -> None:
    async def scenario(root: Path) -> None:
        events = [
            _Notification(
                "item/agentMessage/delta",
                {"delta": "[PROGRESS:DESIGN] Inspect the existing app first.\n"},
            ),
            _Notification(
                "item/completed",
                {
                    "item": {
                        "id": "message-final-progress",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": (
                            "[PROGRESS:DESIGN] Inspect the existing app first. "
                            "The integration is now complete.\n"
                            "[PROGRESS:VALIDATION] The preflights pass.\n"
                            "Done."
                        ),
                    }
                },
            ),
            _turn_completed("turn-final-progress", "completed"),
        ]
        adapter = CodexAppServerAdapter(
            codex=_FakeCodex(
                [
                    _FakeThread(
                        "thread-final-progress",
                        _FakeTurn("turn-final-progress", events),
                    )
                ]
            )
        )
        observed = []

        async def emit(event) -> None:
            observed.append(event)

        result = await adapter.run(
            _request(root, "Inspect the integration", write=False),
            "run-final-progress",
            emit,
        )

        milestones = [
            event.payload["milestone"]
            for event in observed
            if event.type == "semantic.progress"
        ]
        assert milestones == ["design", "validation"]
        assert result.result == "Done."

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-final-progress-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_unknown_and_collaboration_items_fail_closed_as_execution() -> None:
    async def scenario(root: Path, item_type: str) -> None:
        item_id = f"{item_type}-one"
        events = [
            _Notification(
                "item/agentMessage/delta",
                {"delta": "[PROGRESS:DESIGN] Choose the implementation shape.\n"},
            ),
            _Notification(
                "item/started",
                {
                    "item": {
                        "id": item_id,
                        "type": item_type,
                        "tool": "spawn_agent",
                        "status": "inProgress",
                    }
                },
            ),
            _Notification(
                "item/completed",
                {
                    "item": {
                        "id": item_id,
                        "type": item_type,
                        "tool": "spawn_agent",
                        "status": "completed",
                    }
                },
            ),
            _Notification(
                "item/completed",
                {
                    "item": {
                        "id": f"message-{item_type}",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "",
                    }
                },
            ),
            _turn_completed(f"turn-{item_type}", "completed"),
        ]
        adapter = CodexAppServerAdapter(
            codex=_FakeCodex(
                [
                    _FakeThread(
                        f"thread-{item_type}",
                        _FakeTurn(f"turn-{item_type}", events),
                    )
                ]
            )
        )
        observed = []

        async def emit(event) -> None:
            observed.append(event)

        result = await adapter.run(
            _request(root, "Continue the workspace task", write=True),
            f"run-{item_type}",
            emit,
        )

        assert result.activity_evidence is not None
        assert result.activity_evidence.progress_milestones == 1
        assert result.activity_evidence.execution_items == 1
        assert any(event.type == "tool.call" for event in observed)
        assert any(event.type == "tool.result" for event in observed)

    for item_type in ("collabAgentToolCall", "futureNativeToolCall"):
        with tempfile.TemporaryDirectory(
            prefix=f"amadeus-codex-{item_type}-"
        ) as temp_dir:
            asyncio.run(scenario(Path(temp_dir), item_type))


def test_streamed_progress_prefix_replay_is_suppressed_but_replanning_remains() -> None:
    async def scenario(root: Path) -> None:
        events = [
            _Notification(
                "item/agentMessage/delta",
                {"delta": "[PROGRESS:DESIGN] Inspect the app and public interface.\n"},
            ),
            _Notification(
                "item/agentMessage/delta",
                {
                    "delta": (
                        "[PROGRESS:DESIGN] Inspect the app and public interface. "
                        "The interface is loaded now.\n"
                    )
                },
            ),
            _Notification(
                "item/agentMessage/delta",
                {
                    "delta": (
                        "[PROGRESS:DESIGN] Use a Reactive Controller because the "
                        "core loop needs sustained local input.\n"
                    )
                },
            ),
            _Notification(
                "item/completed",
                {
                    "item": {
                        "id": "message-prefix-final",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "Done.",
                    }
                },
            ),
            _turn_completed("turn-prefix-progress", "completed"),
        ]
        adapter = CodexAppServerAdapter(
            codex=_FakeCodex(
                [
                    _FakeThread(
                        "thread-prefix-progress",
                        _FakeTurn("turn-prefix-progress", events),
                    )
                ]
            )
        )
        observed = []

        async def emit(event) -> None:
            observed.append(event)

        await adapter.run(
            _request(root, "Inspect and integrate", write=False),
            "run-prefix-progress",
            emit,
        )

        summaries = [
            event.payload["summary"]
            for event in observed
            if event.type == "semantic.progress"
        ]
        assert summaries == [
            "Inspect the app and public interface.",
            "Use a Reactive Controller because the core loop needs sustained local input.",
        ]

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-prefix-progress-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_windows_runtime_path_prefers_a_real_powershell_binary() -> None:
    if os.name != "nt":
        return
    with tempfile.TemporaryDirectory(prefix="amadeus-codex-shell-") as temp_dir:
        root = Path(temp_dir)
        real = root / "PowerShell"
        alias = root / "Microsoft" / "WindowsApps"
        system = root / "System32"
        real.mkdir(parents=True)
        alias.mkdir(parents=True)
        system.mkdir(parents=True)
        (real / "pwsh.exe").touch()
        (alias / "pwsh.exe").touch()

        normalized = CodexAppServerAdapter._codex_process_env(
            {"PATH": os.pathsep.join((str(alias), str(system), str(real)))}
        )
        assert normalized is not None
        entries = normalized["PATH"].split(os.pathsep)
        assert entries[0] == str(real)

        without_real = CodexAppServerAdapter._codex_process_env(
            {"PATH": os.pathsep.join((str(alias), str(system)))}
        )
        assert without_real is not None
        assert str(alias) not in without_real["PATH"].split(os.pathsep)
        assert str(system) in without_real["PATH"].split(os.pathsep)


def test_sdk_runtime_config_isolated_from_codex_desktop_defaults() -> None:
    captured = []
    previous = os.environ.get("AMADEUS_TEST_PROVIDER_KEY")
    os.environ["AMADEUS_TEST_PROVIDER_KEY"] = "test-secret"
    try:
        adapter = CodexAppServerAdapter(
            codex_factory=lambda config: captured.append(config) or object(),
            model="deepseek-v4-flash",
            model_provider="deepseek",
            reasoning_effort="max",
            provider_base_url="https://api.deepseek.com",
            provider_api_key_env="AMADEUS_TEST_PROVIDER_KEY",
        )
        asyncio.run(adapter._ensure_codex())
    finally:
        if previous is None:
            os.environ.pop("AMADEUS_TEST_PROVIDER_KEY", None)
        else:
            os.environ["AMADEUS_TEST_PROVIDER_KEY"] = previous

    assert len(captured) == 1
    config = captured[0]
    assert config.config_overrides[:3] == (
        'model_providers.deepseek.name="deepseek (managed by Amadeus)"',
        'model_providers.deepseek.base_url="https://api.deepseek.com"',
        'model_providers.deepseek.wire_api="responses"',
    )
    assert any(value.startswith("model_providers.deepseek.auth.command=") for value in config.config_overrides)
    assert any(value.startswith("model_providers.deepseek.auth.args=") for value in config.config_overrides)
    assert all("env_key" not in value for value in config.config_overrides)
    assert config.env["AMADEUS_TEST_PROVIDER_KEY"] == ""
    assert all("gpt-5.6-sol" not in value for value in config.config_overrides)
    assert adapter.reasoning_effort == ReasoningEffort("max")


def test_sdk_adapter_forwards_sol_medium_fast_without_changing_global_codex() -> None:
    async def scenario(root: Path) -> None:
        turn = _FakeTurn("turn-fast", _success_events("turn-fast"))
        thread = _FakeThread("thread-fast", turn)
        codex = _FakeCodex([thread])
        adapter = CodexAppServerAdapter(
            codex=codex,
            model="gpt-5.6-sol",
            model_provider="openai",
            reasoning_effort="medium",
            service_tier="fast",
            provider_base_url="",
            provider_api_key_env="",
            sync_desktop_provider=False,
        )

        async def emit(_event) -> None:
            return None

        result = await adapter.run(
            _request(root, "Inspect the workspace", write=False),
            "run-fast",
            emit,
        )

        assert result.status == "done"
        assert codex.starts[0]["model"] == "gpt-5.6-sol"
        assert codex.starts[0]["model_provider"] == "openai"
        assert codex.starts[0]["service_tier"] == "fast"
        _task, turn_options = thread.turn_calls[0]
        assert turn_options["model"] == "gpt-5.6-sol"
        assert turn_options["effort"] == ReasoningEffort.medium
        assert turn_options["service_tier"] == "fast"
        expected_metadata = {
            "model": "gpt-5.6-sol",
            "model_provider": "openai",
            "reasoning_effort": "medium",
            "service_tier": "fast",
        }
        assert {
            key: result.metadata["codex"][key]
            for key in expected_metadata
        } == expected_metadata

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-fast-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))

    captured = []
    adapter = CodexAppServerAdapter(
        codex_factory=lambda config: captured.append(config) or object(),
        model="gpt-5.6-sol",
        model_provider="openai",
        reasoning_effort="medium",
        service_tier="fast",
        provider_base_url="",
        provider_api_key_env="",
        sync_desktop_provider=False,
    )
    asyncio.run(adapter._ensure_codex())
    assert captured[0].config_overrides == ("features.fast_mode=true",)


def test_sdk_adapter_rejects_unknown_service_tier() -> None:
    try:
        CodexAppServerAdapter(codex=object(), service_tier="turbo")
    except ValueError as exc:
        assert "CODEX_APP_SERVER_SERVICE_TIER" in str(exc)
    else:
        raise AssertionError("unknown Codex service tier must fail at configuration time")


def test_sdk_adapter_syncs_custom_provider_for_desktop_resume() -> None:
    async def scenario(root: Path) -> None:
        config_path = root / ".codex" / "config.toml"
        env_file = root / ".env"
        env_file.write_text("AMADEUS_TEST_PROVIDER_KEY=test-secret\n", encoding="utf-8")
        turn = _FakeTurn("turn-sync", _success_events("turn-sync"))
        codex = _FakeCodex([_FakeThread("thread-sync", turn)])
        adapter = CodexAppServerAdapter(
            codex=codex,
            model="deepseek-v4-flash",
            model_provider="deepseek",
            provider_base_url="https://api.deepseek.com",
            provider_api_key_env="AMADEUS_TEST_PROVIDER_KEY",
            provider_auth_env_file=str(env_file),
            sync_desktop_provider=True,
            desktop_config_path=str(config_path),
        )

        async def emit(_event) -> None:
            return None

        result = await adapter.run(
            _request(root, "Create result.txt", write=True),
            "run-sync",
            emit,
        )

        assert result.status == "done"
        assert len(codex.config_writes) == 1
        edits, written_path = codex.config_writes[0]
        assert written_path == str(config_path.resolve())
        assert edits[0]["keyPath"] == "model_providers.deepseek"
        assert edits[0]["mergeStrategy"] == "upsert"
        provider = edits[0]["value"]
        assert provider["base_url"] == "https://api.deepseek.com"
        assert provider["auth"]["args"][-1] == "AMADEUS_TEST_PROVIDER_KEY"
        assert "test-secret" not in repr(edits)
        assert result.metadata["codex"]["desktop_provider_config"] == "ready"

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-provider-sync-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_sdk_adapter_resumes_only_the_host_attached_thread() -> None:
    async def scenario(root: Path) -> None:
        turn = _FakeTurn("turn-2", _success_events("turn-2"))
        thread = _FakeThread("thread-existing", turn)
        codex = _FakeCodex([thread])
        adapter = CodexAppServerAdapter(codex=codex)

        async def emit(_event) -> None:
            return None

        result = await adapter.run(
            _request(
                root,
                "Amend the existing result",
                write=True,
                session=ProviderSessionHandle(
                    provider="codex",
                    session_id="thread-existing",
                ),
            ),
            "run-2",
            emit,
        )

        assert result.status == "done"
        assert codex.starts == []
        assert codex.resumes[0][0] == "thread-existing"
        assert codex.names == []
        assert codex.injected_items[0][0] == "thread-existing"
        assert result.session is not None
        assert result.session.session_id == "thread-existing"

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-resume-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_sdk_recovery_turn_is_honest_and_does_not_replay_the_user_request() -> None:
    async def scenario(root: Path) -> None:
        turn = _FakeTurn("turn-recovery", _success_events("turn-recovery"))
        thread = _FakeThread("thread-recovery", turn)
        codex = _FakeCodex([thread])
        adapter = CodexAppServerAdapter(codex=codex)
        request = _request(
            root,
            "Prepare the existing application for AUIP.",
            write=True,
            session=ProviderSessionHandle(
                provider="codex",
                session_id="thread-recovery",
            ),
        )
        request.metadata.update(
            {
                "source_user_text": "请你接入它。",
                "presentation_locale": "zh-CN",
                "provider_recovery": {
                    "reason": "progress_only_completion",
                    "ordinal": 1,
                },
            }
        )

        async def emit(_event) -> None:
            return None

        result = await adapter.run(request, "run-recovery", emit)

        assert result.status == "done"
        visible = thread.turn_calls[0][0]
        assert visible.startswith("Amadeus 执行续接")
        assert "上一轮在汇报进度后" in visible
        assert "请你接入它" not in visible
        hidden = codex.injected_items[0][1][0]["content"][0]["text"]
        assert "Prepare the existing application for AUIP." in hidden
        assert codex.names == []

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-recovery-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_sdk_adapter_hides_auip_protocol_from_the_attached_user_turn() -> None:
    async def scenario(root: Path) -> None:
        turn = _FakeTurn("turn-auip", _success_events("turn-auip"))
        thread = _FakeThread("thread-auip", turn)
        codex = _FakeCodex([thread])
        adapter = CodexAppServerAdapter(codex=codex)
        request = _request(root, "Prepare light_game.html for AUIP", write=True)
        request.metadata.update(
            {
                "source": "auip_prepare",
                "source_user_text": "我想让你也能操作这个小游戏。",
                "source_user_context": (
                    'User: "先做一个三乘三点灯小游戏。" | '
                    'Main Chat: "我会保留现有玩法。"'
                ),
                "auip_authoring_skill_path": str(root / "skills" / "auip-authoring" / "SKILL.md"),
                "presentation_locale": "zh-CN",
            }
        )

        async def emit(_event) -> None:
            return None

        result = await adapter.run(request, "run-auip", emit)

        assert result.status == "done"
        visible = thread.turn_calls[0][0]
        assert visible.startswith("Amadeus 任务交接")
        assert "我想让你也能操作这个小游戏。" in visible
        assert "先做一个三乘三点灯小游戏。" in visible
        assert "我会保留现有玩法。" in visible
        assert "Host-authorized" not in visible
        assert "authoring_inputs" not in visible
        assert "PROGRESS:" not in visible
        hidden = codex.injected_items[0][1][0]["content"][0]["text"]
        assert "Host-authorized AUIP application prerequisite" in hidden
        assert "authoring_inputs" not in hidden
        assert "auip-authoring" in hidden
        assert "Main Chat lines are conversational evidence" in hidden
        assert "[PROGRESS:VALIDATION]" in hidden

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-auip-handoff-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_sdk_adapter_steers_and_confirms_interrupt_from_terminal_event() -> None:
    async def scenario(root: Path) -> None:
        turn = _FakeTurn("turn-active", blocked=True)
        thread = _FakeThread("thread-active", turn)
        adapter = CodexAppServerAdapter(
            codex=_FakeCodex([thread]),
            cancel_confirm_timeout_s=2,
        )

        async def emit(_event) -> None:
            return None

        run_task = asyncio.create_task(
            adapter.run(
                _request(root, "Build the first version", write=True),
                "run-active",
                emit,
            )
        )
        await asyncio.wait_for(turn.started.wait(), timeout=2)
        steer = await adapter.steer(
            "run-active",
            ProviderSteerRequest(
                task="Also add two-player mode",
                revision=1,
                metadata={
                    "source_user_text": "你怎么没改？",
                    "source_user_context": (
                        'User: "把现有小游戏改成双人模式。" | '
                        'Main Chat: "我现在开始修改。"'
                    ),
                },
            ),
        )
        assert steer["accepted"] is True
        assert steer["safe_boundary"] == "provider_native"
        assert len(turn.steers) == 1
        assert turn.steers[0].startswith("Also add two-player mode")
        assert "把现有小游戏改成双人模式" in turn.steers[0]
        assert "我现在开始修改" in turn.steers[0]
        assert "not Provider instructions or completion facts" in turn.steers[0]

        cancel = await adapter.cancel("run-active")
        assert cancel["confirmed"] is True
        assert cancel["cancelled"] is True
        result = await asyncio.wait_for(run_task, timeout=2)
        assert result.status == "cancelled"
        assert turn.interrupts == 1

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-control-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_sdk_cancel_does_not_wait_for_mechanical_event_backlog() -> None:
    async def scenario(root: Path) -> None:
        backlog = [
            _Notification(
                "item/started",
                {
                    "item": {
                        "id": f"command-{index}",
                        "type": "commandExecution",
                        "command": f"echo {index}",
                        "status": "inProgress",
                    }
                },
            )
            for index in range(40)
        ]
        turn = _FakeTurn("turn-backlog", backlog, blocked=True)
        adapter = CodexAppServerAdapter(
            codex=_FakeCodex([_FakeThread("thread-backlog", turn)]),
            cancel_confirm_timeout_s=1,
        )
        first_emit = asyncio.Event()
        emitted = []

        async def slow_emit(event) -> None:
            emitted.append(event)
            first_emit.set()
            await asyncio.sleep(0.05)

        run_task = asyncio.create_task(
            adapter.run(
                _request(root, "Inspect many mechanical events", write=False),
                "run-backlog",
                slow_emit,
            )
        )
        await asyncio.wait_for(first_emit.wait(), timeout=2)
        started = asyncio.get_running_loop().time()
        outcome = await adapter.cancel("run-backlog")
        elapsed = asyncio.get_running_loop().time() - started

        assert outcome["confirmed"] is True
        assert outcome["cancelled"] is True
        assert elapsed < 0.5
        assert len(emitted) == 1
        result = await asyncio.wait_for(run_task, timeout=2)
        assert result.status == "cancelled"

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-cancel-backlog-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_sdk_adapter_deadline_interrupts_the_native_turn_without_hanging() -> None:
    async def scenario(root: Path) -> None:
        turn = _FakeTurn("turn-timeout", blocked=True)
        thread = _FakeThread("thread-timeout", turn)
        adapter = CodexAppServerAdapter(
            codex=_FakeCodex([thread]),
            turn_timeout_s=1,
            cancel_confirm_timeout_s=1,
        )

        async def emit(_event) -> None:
            return None

        started = asyncio.get_running_loop().time()
        result = await adapter.run(
            _request(root, "Keep working until interrupted", write=True),
            "run-timeout",
            emit,
        )
        elapsed = asyncio.get_running_loop().time() - started

        assert result.status == "error"
        assert result.error == "Codex turn timed out after 1s"
        assert turn.interrupts == 1
        assert elapsed < 2.5

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-timeout-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_sdk_native_approval_waits_for_the_host_permission_decision() -> None:
    async def scenario(root: Path) -> None:
        turn = _FakeTurn("turn-approval", blocked=True)
        thread = _FakeThread("thread-approval", turn)
        adapter = CodexAppServerAdapter(
            codex=_FakeCodex([thread]),
            approval_timeout_s=2,
        )
        events = []

        async def emit(event) -> None:
            events.append(event)

        run_task = asyncio.create_task(
            adapter.run(
                _request(root, "Run the requested verification", write=True),
                "run-approval",
                emit,
            )
        )
        await asyncio.wait_for(turn.started.wait(), timeout=2)
        approval_task = asyncio.create_task(
            asyncio.to_thread(
                adapter._handle_sdk_approval,
                "item/commandExecution/requestApproval",
                {
                    "threadId": "thread-approval",
                    "turnId": "turn-approval",
                    "itemId": "command-approval",
                    "startedAtMs": 1,
                    "cwd": str(root),
                    "command": "python verify.py",
                    "reason": "Run the project's verification command.",
                },
            )
        )
        for _ in range(100):
            requested = next(
                (event for event in events if event.type == "permission.requested"),
                None,
            )
            if requested is not None:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("native approval was not projected")

        permission = requested.payload["permissionRequest"]
        assert permission["capability"] == "shell.execute"
        assert permission["options"] == ["allow_once", "deny"]
        assert approval_task.done() is False
        outcome = await adapter.resolve_permission(
            "run-approval",
            ProviderPermissionResponse(
                request_id=permission["request_id"],
                allow=True,
            ),
        )
        assert outcome == {"accepted": True}
        assert await asyncio.wait_for(approval_task, timeout=2) == {"decision": "accept"}

        await turn.control_events.put(_turn_completed(turn.id, "completed"))
        result = await asyncio.wait_for(run_task, timeout=2)
        assert result.status == "done"

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-approval-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_sdk_native_approval_survives_slow_permission_projection() -> None:
    async def scenario(root: Path) -> None:
        turn = _FakeTurn("turn-slow-approval", blocked=True)
        thread = _FakeThread("thread-slow-approval", turn)
        adapter = CodexAppServerAdapter(
            codex=_FakeCodex([thread]),
            approval_timeout_s=2,
        )
        events = []
        permission_visible = asyncio.Event()
        release_projection = asyncio.Event()

        async def emit(event) -> None:
            events.append(event)
            if event.type == "permission.requested":
                permission_visible.set()
                await release_projection.wait()

        run_task = asyncio.create_task(
            adapter.run(
                _request(root, "Run the requested verification", write=True),
                "run-slow-approval",
                emit,
            )
        )
        await asyncio.wait_for(turn.started.wait(), timeout=2)
        approval_task = asyncio.create_task(
            asyncio.to_thread(
                adapter._handle_sdk_approval,
                "item/commandExecution/requestApproval",
                {
                    "threadId": "thread-slow-approval",
                    "turnId": "turn-slow-approval",
                    "itemId": "command-slow-approval",
                    "cwd": str(root),
                    "command": "python verify.py",
                },
            )
        )
        await asyncio.wait_for(permission_visible.wait(), timeout=2)
        permission = next(
            event.payload["permissionRequest"]
            for event in events
            if event.type == "permission.requested"
        )

        outcome = await adapter.resolve_permission(
            "run-slow-approval",
            ProviderPermissionResponse(
                request_id=permission["request_id"],
                allow=True,
            ),
        )
        assert outcome == {"accepted": True}
        # The native response must not wait for unrelated slow event-bus
        # subscribers once the user decision has reached the adapter.
        assert await asyncio.wait_for(approval_task, timeout=0.5) == {
            "decision": "accept"
        }

        release_projection.set()
        await turn.control_events.put(_turn_completed(turn.id, "completed"))
        result = await asyncio.wait_for(run_task, timeout=2)
        assert result.status == "done"
        assert not any(event.type == "permission.expired" for event in events)

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-slow-approval-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_sdk_native_approval_timeout_declines_and_emits_expiry() -> None:
    async def scenario(root: Path) -> None:
        turn = _FakeTurn("turn-expiry", blocked=True)
        thread = _FakeThread("thread-expiry", turn)
        adapter = CodexAppServerAdapter(
            codex=_FakeCodex([thread]),
            approval_timeout_s=0.05,
        )
        events = []

        async def emit(event) -> None:
            events.append(event)

        run_task = asyncio.create_task(
            adapter.run(_request(root, "Try one file change", write=True), "run-expiry", emit)
        )
        await asyncio.wait_for(turn.started.wait(), timeout=2)
        native = await asyncio.to_thread(
            adapter._handle_sdk_approval,
            "item/fileChange/requestApproval",
            {
                "threadId": "thread-expiry",
                "turnId": "turn-expiry",
                "itemId": "file-expiry",
                "startedAtMs": 1,
                "grantRoot": str(root),
            },
        )
        assert native == {"decision": "decline"}
        for _ in range(100):
            if any(event.type == "permission.expired" for event in events):
                break
            await asyncio.sleep(0.01)
        assert any(event.type == "permission.expired" for event in events)

        await turn.control_events.put(_turn_completed(turn.id, "completed"))
        await asyncio.wait_for(run_task, timeout=2)

    with tempfile.TemporaryDirectory(prefix="amadeus-codex-approval-expiry-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_bootstrap_exposes_exactly_one_codex_transport() -> None:
    app_server = {
        spec.provider_id: spec
        for spec in builtin_provider_specs(
            direct_codex_enabled=False,
            codex_app_server_enabled=True,
        )
    }
    assert app_server["codex"].runtime_enabled is True
    assert app_server["codex"].factory.__name__ == "_codex_app_server_adapter"

    try:
        builtin_provider_specs(
            direct_codex_enabled=True,
            codex_app_server_enabled=True,
        )
    except RuntimeError as exc:
        assert "cannot both own Provider id" in str(exc)
    else:
        raise AssertionError("two Codex transports must fail before registration")


def _main() -> None:
    test_sdk_adapter_maps_thread_turn_events_and_terminal_truth()
    test_native_plan_becomes_an_early_reported_design_milestone()
    test_dynamic_tool_title_becomes_a_bounded_reported_direction()
    test_unknown_and_collaboration_items_fail_closed_as_execution()
    test_windows_runtime_path_prefers_a_real_powershell_binary()
    test_sdk_runtime_config_isolated_from_codex_desktop_defaults()
    test_sdk_adapter_forwards_sol_medium_fast_without_changing_global_codex()
    test_sdk_adapter_rejects_unknown_service_tier()
    test_sdk_adapter_resumes_only_the_host_attached_thread()
    test_sdk_recovery_turn_is_honest_and_does_not_replay_the_user_request()
    test_sdk_adapter_hides_auip_protocol_from_the_attached_user_turn()
    test_sdk_adapter_steers_and_confirms_interrupt_from_terminal_event()
    test_sdk_adapter_deadline_interrupts_the_native_turn_without_hanging()
    test_sdk_native_approval_waits_for_the_host_permission_decision()
    test_sdk_native_approval_survives_slow_permission_projection()
    test_sdk_native_approval_timeout_declines_and_emits_expiry()
    test_bootstrap_exposes_exactly_one_codex_transport()
    print("ok: official Codex SDK is a thin, provider-neutral adapter boundary")


if __name__ == "__main__":
    _main()
