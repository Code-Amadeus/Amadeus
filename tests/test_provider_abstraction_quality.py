from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as settings
from agent_host.provider_bootstrap import builtin_provider_specs
from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST
from agent_host.provider_contract import (
    ProviderCapabilities,
    ProviderManifest,
    ProviderRequirements,
    ProviderSelectionError,
    compatibility_errors,
    select_provider,
)
from agent_host.provider_runtime import ProviderRuntime, runtime as global_runtime
from agent_host.provider_types import (
    ProviderEvent,
    ProviderRunRequest,
    ProviderRunResult,
    ProviderPermissionResponse,
    ProviderSessionHandle,
)
from agent_host.provider_workspace import (
    WorkspaceContractError,
    prepare_workspace_binding,
    workspace_route_authority,
)
from server.app import _delegate_provider_selection
from server.handlers.provider_handler import ProviderHandler


def _manifest(
    provider_id: str,
    *,
    ownership: str = "caller",
    durability: str = "turn",
    resume: str = "none",
) -> ProviderManifest:
    return ProviderManifest(
        provider_id=provider_id,
        display_name=provider_id,
        capabilities=ProviderCapabilities(
            task_kinds=("general", "workspace_mutation"),
            workspace_access="write",
            workspace_ownership=ownership,  # type: ignore[arg-type]
            durability=durability,  # type: ignore[arg-type]
            resume=resume,  # type: ignore[arg-type]
        ),
    )


class _ClosableAdapter:
    provider_id = "closable"
    manifest = _manifest(provider_id)

    def __init__(self) -> None:
        self.close_calls = 0

    async def run(self, request, run_id, emit):
        return ProviderRunResult(status="done", result="ok")

    async def cancel(self, run_id):
        return None

    async def close(self) -> None:
        self.close_calls += 1


class _InteractiveAdapter:
    provider_id = "interactive_test"
    manifest = ProviderManifest(
        provider_id=provider_id,
        display_name="interactive test",
        capabilities=ProviderCapabilities(
            task_kinds=("general",),
            interaction="bidirectional",
        ),
    )

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.responses: list[ProviderPermissionResponse] = []

    async def run(self, request, run_id, emit):
        self.started.set()
        await self.release.wait()
        return ProviderRunResult(status="done", result="ok")

    async def cancel(self, run_id):
        self.release.set()
        return {"confirmed": True, "cancelled": True}

    async def resolve_permission(self, run_id, response):
        self.responses.append(response)
        return {"accepted": True}


class _ActivityAdapter:
    provider_id = "activity_test"
    manifest = _manifest(provider_id)

    async def run(self, request, run_id, emit):
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="tool.call",
                payload={"name": "shell", "item_id": "native-item"},
                metadata={"session_id": "provider-cannot-redirect"},
            )
        )
        return ProviderRunResult(status="done", result="ok")

    async def cancel(self, run_id):
        return None


class _SlowCancellationAdapter:
    provider_id = "slow_cancel_test"
    manifest = _manifest(provider_id)

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancel_started = asyncio.Event()
        self.confirm_cancel = asyncio.Event()
        self.cancel_calls = 0

    async def run(self, request, run_id, emit):
        self.started.set()
        await asyncio.Future()

    async def cancel(self, run_id):
        self.cancel_calls += 1
        self.cancel_started.set()
        await self.confirm_cancel.wait()
        return {"confirmed": True, "cancelled": True}


def test_provider_runtime_closes_adapter_owned_resources_without_provider_branching() -> None:
    async def scenario() -> None:
        runtime = ProviderRuntime()
        adapter = _ClosableAdapter()
        runtime.register(adapter)

        await runtime.close()

        assert adapter.close_calls == 1

    asyncio.run(scenario())


def test_runtime_routes_permissions_by_capability_not_provider_identity() -> None:
    async def scenario() -> None:
        runtime = ProviderRuntime()
        adapter = _InteractiveAdapter()
        runtime.register(adapter)
        record = await runtime.start(
            ProviderRunRequest(provider=adapter.provider_id, task="Wait for approval")
        )
        await asyncio.wait_for(adapter.started.wait(), timeout=2)
        response = ProviderPermissionResponse(request_id="native-request-1", allow=True)

        outcome = await runtime.resolve_permission(record.run_id, response)

        assert outcome["accepted"] is True
        assert adapter.responses == [response]
        assert any(event["type"] == "permission.allowed" for event in record.events)
        adapter.release.set()
        assert record.task_handle is not None
        await record.task_handle

    asyncio.run(scenario())


def test_cancellation_becomes_visible_immediately_and_duplicate_requests_coalesce() -> None:
    async def scenario() -> None:
        runtime = ProviderRuntime()
        adapter = _SlowCancellationAdapter()
        runtime.register(adapter)
        record = await runtime.start(
            ProviderRunRequest(
                provider=adapter.provider_id,
                task="Keep working until cancelled.",
            )
        )
        await asyncio.wait_for(adapter.started.wait(), timeout=2)

        first = asyncio.create_task(runtime.cancel(record.run_id))
        await asyncio.wait_for(adapter.cancel_started.wait(), timeout=2)
        assert record.metadata["liveness"]["state"] == "cancel_pending"
        assert any(
            event["type"] == "run.status"
            and event["payload"].get("liveness") == "cancel_pending"
            for event in record.events
        )

        duplicate = await runtime.cancel(record.run_id)
        assert duplicate["cancelled"] is False
        assert duplicate["reason"] == "cancel_pending"
        assert adapter.cancel_calls == 1

        adapter.confirm_cancel.set()
        confirmed = await asyncio.wait_for(first, timeout=2)
        assert confirmed["cancelled"] is True
        assert record.task_handle is not None
        await asyncio.gather(record.task_handle, return_exceptions=True)

    asyncio.run(scenario())


def test_runtime_binds_chat_origin_to_provider_neutral_activity_events() -> None:
    async def scenario() -> None:
        runtime = ProviderRuntime()
        runtime.register(_ActivityAdapter())
        record = await runtime.start(
            ProviderRunRequest(
                provider="activity_test",
                task="Show observable work.",
                metadata={
                    "session_id": "chat-session",
                    "turn_id": "chat-turn",
                    "work": {
                        "work_item_id": "work-one",
                        "attempt_id": "attempt-one",
                    },
                },
            )
        )
        assert record.task_handle is not None
        await record.task_handle
        activity = next(event for event in record.events if event["type"] == "tool.call")
        assert activity["metadata"]["session_id"] == "chat-session"
        assert activity["metadata"]["turn_id"] == "chat-turn"
        assert activity["task_id"] == "work-one"
        assert activity["attempt_id"] == "attempt-one"

    asyncio.run(scenario())


def test_non_linear_capabilities_are_not_treated_as_strength_ranks() -> None:
    remote_attach = _manifest(
        "remote_attach",
        durability="remote",
        resume="attach",
    )
    errors = compatibility_errors(
        remote_attach,
        ProviderRequirements(
            durability="host_restart",
            resume="same_attempt",
        ),
    )
    assert "durability:host_restart" in errors
    assert "resume:same_attempt" in errors


def test_workspace_ownership_is_an_operational_requirement() -> None:
    caller = _manifest("caller")
    errors = compatibility_errors(
        caller,
        ProviderRequirements(workspace_ownership="negotiated"),
    )
    assert errors == ("workspace_ownership:negotiated",)


def test_workspace_route_authority_follows_ownership_not_provider_identity() -> None:
    assert workspace_route_authority("caller") == "host"
    assert workspace_route_authority("negotiated") == "host"
    assert workspace_route_authority("provider") == "provider"
    assert workspace_route_authority("none") == "not_applicable"


def test_duplicate_provider_manifests_fail_instead_of_overwriting() -> None:
    try:
        select_provider(
            ProviderRequirements(),
            [_manifest("duplicate"), _manifest("duplicate")],
        )
    except ProviderSelectionError as exc:
        assert "duplicate provider manifest" in str(exc)
    else:
        raise AssertionError("duplicate provider ids must be rejected")


def test_bootstrap_separates_known_providers_from_runtime_availability() -> None:
    disabled = {
        spec.provider_id: spec
        for spec in builtin_provider_specs(
            direct_codex_enabled=False,
            codex_app_server_enabled=False,
        )
    }
    assert disabled["codex"].runtime_enabled is False
    assert disabled["codex"].instantiate_when_disabled is False
    assert set(disabled) == {"browser", "openclaw", "codex"}
    enabled = {
        spec.provider_id: spec
        for spec in builtin_provider_specs(
            direct_codex_enabled=True,
            codex_app_server_enabled=False,
        )
    }
    assert enabled["codex"].runtime_enabled is True
    assert set(enabled) == {"browser", "openclaw", "codex"}


def test_delegate_selector_only_accepts_injected_or_registered_manifests() -> None:
    try:
        _delegate_provider_selection(
            "Create README.md",
            {"provider": "direct_codex"},
            manifests=(_manifest("codex"),),
        )
    except ProviderSelectionError as exc:
        assert "required provider is not registered: direct_codex" in str(exc)
    else:
        raise AssertionError("provider aliases must not leak into host routing")


def test_workspace_binding_records_host_truth_without_provider_guessing() -> None:
    with tempfile.TemporaryDirectory(prefix="amadeus-workspace-contract-") as temp_dir:
        root = Path(temp_dir)
        request = ProviderRunRequest(
            provider="caller",
            task="Write the file",
            cwd=str(root),
            requirements=ProviderRequirements(
                task_kind="workspace_mutation",
                workspace_access="write",
                workspace_ownership="caller",
                preferred_provider="caller",
                preference_policy="require",
            ),
        )
        binding = prepare_workspace_binding(request, _manifest("caller"))
        assert binding.status == "ready"
        assert binding.ownership == "caller"
        assert binding.host_readable is True
        assert binding.host_writable is True
        assert Path(binding.cwd) == root.resolve()

        codex_binding = prepare_workspace_binding(
            ProviderRunRequest(
                provider="codex",
                task="Write the file",
                cwd=str(root),
                requirements=ProviderRequirements(
                    task_kind="workspace_mutation",
                    workspace_access="write",
                ),
            ),
            CODEX_APP_SERVER_MANIFEST,
        )
        assert codex_binding.ownership == "caller"
        assert codex_binding.status == "ready"

        request.cwd = str(root / "missing")
        try:
            prepare_workspace_binding(request, _manifest("caller"))
        except WorkspaceContractError as exc:
            assert "not a directory" in str(exc)
        else:
            raise AssertionError("a missing caller workspace must fail before adapter.run")

    provider_owned = prepare_workspace_binding(
        ProviderRunRequest(
            provider="provider_owned",
            task="Allocate a workspace",
            requirements=ProviderRequirements(
                workspace_access="write",
                workspace_ownership="provider",
            ),
        ),
        _manifest("provider_owned", ownership="provider"),
    )
    assert provider_owned.status == "provider_pending"


class _SpoofingWorkspaceAdapter:
    provider_id = "workspace_test"
    manifest = _manifest(provider_id)

    async def run(self, request, run_id, emit):
        return ProviderRunResult(
            status="done",
            result="ok",
            metadata={
                "workspace_binding": {"cwd": "spoofed"},
                "host_outcome_requirement": {
                    "operation": "spoofed",
                    "facet": "spoofed.result",
                },
                "provider_session": {
                    "provider": "workspace_test",
                    "session_id": "spoofed-native-context",
                    "scope": "work_item",
                    "version": 1,
                },
            },
        )

    async def cancel(self, run_id):
        return {"confirmed": True, "cancelled": True}


class _TypedSessionAdapter:
    provider_id = "session_test"
    manifest = ProviderManifest(
        provider_id=provider_id,
        display_name="session test",
        capabilities=ProviderCapabilities(
            task_kinds=("general",),
            workspace_access="none",
            workspace_ownership="none",
            resume="attach",
        ),
    )

    async def run(self, request, run_id, emit):
        return ProviderRunResult(
            status="done",
            result="ok",
            session=ProviderSessionHandle(
                provider=self.provider_id,
                session_id=(
                    "native-session-2" if request.session else "native-session-1"
                ),
                scope=request.session.scope if request.session else "work_item",
            ),
        )

    async def cancel(self, run_id):
        return {"confirmed": True, "cancelled": True}


class _OrphanedSessionAdapter:
    provider_id = "orphaned_session_test"
    manifest = ProviderManifest(
        provider_id=provider_id,
        display_name="orphaned session test",
        capabilities=ProviderCapabilities(
            task_kinds=("general",),
            workspace_access="none",
            workspace_ownership="none",
            resume="attach",
        ),
    )

    async def run(self, request, run_id, emit):
        return ProviderRunResult(
            status="orphaned",
            error="accepted native run has an unknown outcome",
            metadata={"runtime_resumable": False},
            session=ProviderSessionHandle(
                provider=self.provider_id,
                session_id="native-session-orphaned",
                scope="work_item",
            ),
        )

    async def cancel(self, run_id):
        return {"confirmed": False, "cancelled": False}


def test_runtime_owns_and_protects_workspace_binding() -> None:
    async def scenario(root: Path) -> None:
        runtime = ProviderRuntime()
        runtime.register(_SpoofingWorkspaceAdapter())
        record = await runtime.start(
            ProviderRunRequest(
                provider="workspace_test",
                task="Write a file",
                cwd=str(root),
                requirements=ProviderRequirements(
                    task_kind="workspace_mutation",
                    workspace_access="write",
                    workspace_ownership="caller",
                    preferred_provider="workspace_test",
                    preference_policy="require",
                ),
                metadata={
                    "host_outcome_requirement": {
                        "operation": "verify",
                        "facet": "host.verified_result",
                    }
                },
            )
        )
        assert record.task_handle is not None
        await record.task_handle
        binding = record.metadata["workspace_binding"]
        assert binding["cwd"] == str(root.resolve())
        assert binding["ownership"] == "caller"
        assert record.metadata["provider_manifest"]["provider_id"] == "workspace_test"
        assert record.metadata["host_outcome_requirement"] == {
            "operation": "verify",
            "facet": "host.verified_result",
        }
        assert "provider_session" not in record.metadata

    with tempfile.TemporaryDirectory(prefix="amadeus-runtime-workspace-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_disabled_codex_is_not_registered_for_execution() -> None:
    previous_codex = settings.DIRECT_CODEX_PROVIDER_ENABLED
    previous_app_server = settings.CODEX_APP_SERVER_PROVIDER_ENABLED
    try:
        settings.DIRECT_CODEX_PROVIDER_ENABLED = False
        settings.CODEX_APP_SERVER_PROVIDER_ENABLED = False
        ProviderHandler()
        assert global_runtime.get_manifest("codex") is None
        assert global_runtime.get_manifest("codex") is None
    finally:
        settings.DIRECT_CODEX_PROVIDER_ENABLED = previous_codex
        settings.CODEX_APP_SERVER_PROVIDER_ENABLED = previous_app_server


def test_provider_session_handle_is_typed_and_provider_bound() -> None:
    handle = ProviderSessionHandle(
        provider="OpenClaw",
        session_id="agent:main:dashboard:test",
        scope="work_item",
    )
    assert handle.provider == "openclaw"
    assert ProviderSessionHandle.from_dict(handle.to_dict()) == handle
    try:
        ProviderSessionHandle.from_dict(
            {
                "provider": "openclaw",
                "session_id": "agent:main:dashboard:test",
                "scope": "global",
            }
        )
    except ValueError as exc:
        assert "scope" in str(exc)
    else:
        raise AssertionError("an unbounded Provider Session scope must be rejected")


def test_runtime_serializes_only_the_typed_provider_session() -> None:
    async def scenario() -> None:
        runtime = ProviderRuntime()
        runtime.register(_TypedSessionAdapter())
        record = await runtime.start(
            ProviderRunRequest(
                provider="session_test",
                task="Continue the external task.",
                metadata={
                    "provider_session": {
                        "provider": "session_test",
                        "session_id": "spoofed",
                    }
                },
            )
        )
        assert record.task_handle is not None
        await record.task_handle
        assert record.status == "done"
        assert record.metadata["provider_session"] == {
            "provider": "session_test",
            "session_id": "native-session-1",
            "scope": "work_item",
            "version": 1,
        }

        attached = ProviderSessionHandle(
            provider="session_test",
            session_id="native-session-1",
            scope="work_item",
        )
        continued = await runtime.start(
            ProviderRunRequest(
                provider="session_test",
                task="A later operation.",
                session=attached,
            )
        )
        assert continued.task_handle is not None
        await continued.task_handle
        assert continued.metadata["provider_session"] == {
            "provider": "session_test",
            "session_id": "native-session-2",
            "scope": "work_item",
            "version": 1,
        }

    asyncio.run(scenario())


def test_runtime_preserves_an_orphaned_typed_session_as_nonterminal() -> None:
    async def scenario() -> None:
        runtime = ProviderRuntime()
        runtime.register(_OrphanedSessionAdapter())
        record = await runtime.start(
            ProviderRunRequest(
                provider="orphaned_session_test",
                task="Observe an accepted native run.",
            )
        )
        assert record.task_handle is not None
        await record.task_handle
        assert record.status == "orphaned"
        assert record.metadata["liveness"]["state"] == "orphaned"
        assert record.metadata["runtime_resumable"] is False
        assert record.metadata["provider_session"] == {
            "provider": "orphaned_session_test",
            "session_id": "native-session-orphaned",
            "scope": "work_item",
            "version": 1,
        }

    asyncio.run(scenario())


def _main() -> None:
    test_provider_runtime_closes_adapter_owned_resources_without_provider_branching()
    test_runtime_routes_permissions_by_capability_not_provider_identity()
    test_runtime_binds_chat_origin_to_provider_neutral_activity_events()
    test_non_linear_capabilities_are_not_treated_as_strength_ranks()
    test_workspace_ownership_is_an_operational_requirement()
    test_workspace_route_authority_follows_ownership_not_provider_identity()
    test_duplicate_provider_manifests_fail_instead_of_overwriting()
    test_bootstrap_separates_known_providers_from_runtime_availability()
    test_delegate_selector_only_accepts_injected_or_registered_manifests()
    test_workspace_binding_records_host_truth_without_provider_guessing()
    test_runtime_owns_and_protects_workspace_binding()
    test_disabled_codex_is_not_registered_for_execution()
    test_provider_session_handle_is_typed_and_provider_bound()
    test_runtime_serializes_only_the_typed_provider_session()
    test_runtime_preserves_an_orphaned_typed_session_as_nonterminal()
    print("ok: provider abstraction quality boundaries are enforced")


if __name__ == "__main__":
    _main()
