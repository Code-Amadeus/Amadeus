from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.provider_contract import (
    ProviderCapabilities,
    ProviderManifest,
    ProviderOperation,
    ProviderRequirements,
    ProviderSelectionError,
    manifest_for_adapter,
    select_provider,
)
from agent_host.provider_catalog import BROWSER_MANIFEST as BUILTIN_BROWSER_MANIFEST
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_outcome import ProviderOutcomeEvidence
from agent_host.provider_types import ProviderEvent, ProviderRunRequest, ProviderRunResult
from agent_host.work_ledger_store import WorkLedgerStore
from server.event_bus import bus
from server.handlers.provider_handler import ProviderHandler
from server.protocol import Method
from server.work_ledger_coordinator import WorkLedgerCoordinator


OPENCLAW = ProviderManifest(
    provider_id="openclaw",
    display_name="OpenClaw",
    selection_priority=50,
    capabilities=ProviderCapabilities(task_kinds=("general",)),
)
LOCUS = ProviderManifest(
    provider_id="locus",
    display_name="Locus",
    selection_priority=100,
    capabilities=ProviderCapabilities(
        task_kinds=("general", "workspace_mutation"),
        workspace_access="write",
        workspace_ownership="negotiated",
        durability="host_restart",
        resume="same_attempt",
        cancellation="confirmed",
        interaction="diagnostic",
        event_model="canonical+native",
    ),
)
BROWSER = ProviderManifest(
    provider_id="browser",
    display_name="Browser",
    runtime_kind="stateful_tool",
    capabilities=ProviderCapabilities(task_kinds=("browser",), steering="next_turn"),
)


def test_manifest_is_semantic_and_serializable() -> None:
    payload = LOCUS.to_dict()
    assert payload["contract_version"] == "0.2"
    assert payload["ownership_modes"] == ["managed"]
    assert payload["capabilities"]["workspace_access"] == "write"
    assert payload["capabilities"]["workspace_ownership"] == "negotiated"
    assert payload["capabilities"]["resume"] == "same_attempt"
    assert payload["capabilities"]["event_model"] == "canonical+native"


def test_manifest_can_declare_semantic_operations_without_provider_checks() -> None:
    payload = BUILTIN_BROWSER_MANIFEST.to_dict()
    assert payload["contract_version"] == "0.3"
    operations = {
        item["operation_id"]: item
        for item in payload["capabilities"]["operations"]
    }
    assert operations["search"] == {
        "operation_id": "search",
        "execution": "observe_then_plan",
        "atomic": False,
        "outcome_facet": "browser.page_state",
    }
    assert operations["open"]["atomic"] is True


def test_invalid_operation_contracts_fail_at_manifest_construction() -> None:
    try:
        ProviderCapabilities(
            operations=(
                ProviderOperation("publish", atomic=False, execution="direct"),
            )
        )
    except ValueError as exc:
        assert "planning boundary" in str(exc)
    else:
        raise AssertionError("a non-atomic operation needs a lowering boundary")

    try:
        ProviderCapabilities(
            operations=(
                ProviderOperation("publish"),
                ProviderOperation("PUBLISH"),
            )
        )
    except ValueError as exc:
        assert "duplicate provider operation" in str(exc)
    else:
        raise AssertionError("operation ids must be unique case-insensitively")


def test_selector_matches_requirements_instead_of_provider_names() -> None:
    write = ProviderRequirements(
        task_kind="workspace_mutation",
        workspace_access="write",
    )
    selected = select_provider(
        write,
        [OPENCLAW, LOCUS, BROWSER],
        default_provider="openclaw",
    )
    assert selected.provider_id == "locus"
    assert selected.reason == "preferred_provider_incompatible"
    assert selected.rejected["openclaw"] == (
        "task_kind:workspace_mutation",
        "workspace_access:write",
    )

    general = select_provider(
        ProviderRequirements(),
        [OPENCLAW, LOCUS, BROWSER],
        default_provider="openclaw",
    )
    assert general.provider_id == "openclaw"
    assert general.reason == "preferred_provider"

    browser = select_provider(
        ProviderRequirements(
            task_kind="browser",
            preferred_provider="browser",
            preference_policy="require",
        ),
        [OPENCLAW, LOCUS, BROWSER],
        default_provider="openclaw",
    )
    assert browser.provider_id == "browser"


def test_selector_distinguishes_require_prefer_and_force() -> None:
    required_openclaw = ProviderRequirements(
        task_kind="workspace_mutation",
        workspace_access="write",
        preferred_provider="openclaw",
        preference_policy="require",
    )
    try:
        select_provider(required_openclaw, [OPENCLAW, LOCUS])
    except ProviderSelectionError as exc:
        assert "incompatible" in str(exc)
    else:
        raise AssertionError("a required incompatible provider must be rejected")

    preferred_openclaw = ProviderRequirements(
        task_kind="workspace_mutation",
        workspace_access="write",
        preferred_provider="openclaw",
        preference_policy="prefer",
    )
    assert select_provider(preferred_openclaw, [OPENCLAW, LOCUS]).provider_id == "locus"

    forced_openclaw = ProviderRequirements(
        task_kind="workspace_mutation",
        workspace_access="write",
        preferred_provider="openclaw",
        preference_policy="force",
    )
    forced = select_provider(forced_openclaw, [OPENCLAW, LOCUS])
    assert forced.provider_id == "openclaw"
    assert forced.reason == "forced_provider"


def test_no_compatible_provider_fails_explicitly() -> None:
    try:
        select_provider(
            ProviderRequirements(
                task_kind="workspace_mutation",
                workspace_access="write",
            ),
            [OPENCLAW, BROWSER],
            default_provider="openclaw",
        )
    except ProviderSelectionError as exc:
        assert "no registered provider" in str(exc)
    else:
        raise AssertionError("routing must not silently choose an incompatible provider")


def test_legacy_adapter_gets_conservative_compatibility_manifest() -> None:
    class LegacyAdapter:
        provider_id = "legacy"

    manifest = manifest_for_adapter(LegacyAdapter())
    assert manifest.provider_id == "legacy"
    assert manifest.declared is False
    assert manifest.contract_version == "0.1-compat"
    assert manifest.capabilities.workspace_access == "none"


class EnvelopeAdapter:
    provider_id = "envelope"
    manifest = ProviderManifest(
        provider_id=provider_id,
        display_name="Envelope test provider",
        capabilities=ProviderCapabilities(task_kinds=("general",)),
    )

    async def run(self, request, run_id, emit):
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="semantic.progress",
                payload={"summary": "observed"},
                metadata={
                    "replay": True,
                    "provider_ownership": "attached",
                    # An adapter cannot escape the Task/Attempt to which the
                    # control plane bound its run.
                    "work": {
                        "work_item_id": "spoofed-task",
                        "attempt_id": "spoofed-attempt",
                        "attempt_epoch": 99,
                    },
                },
            )
        )
        return ProviderRunResult(
            status="done",
            result="ok",
            metadata={
                "outcome_evidence": {
                    "facet": "spoofed.native_claim",
                    "operation": "spoof",
                    "observation_authority": "host",
                },
                "work": {
                    "work_item_id": "spoofed-result-task",
                    "attempt_id": "spoofed-result-attempt",
                    "attempt_epoch": 100,
                }
            },
            outcome_evidence=ProviderOutcomeEvidence(
                facet="test.observed_state",
                operation="inspect",
                observed={"value": "host adapter receipt"},
            ),
        )

    async def cancel(self, run_id):
        return {"confirmed": True, "cancelled": True}


def test_runtime_envelope_and_work_ledger_identity_are_assembled(tmp_path) -> None:
    async def scenario() -> None:
        store = WorkLedgerStore(tmp_path / "work.sqlite3")
        coordinator = WorkLedgerCoordinator(store)
        runtime = ProviderRuntime()
        runtime.register(EnvelopeAdapter())
        runtime.set_request_preparer(coordinator.prepare_request)
        captured_events: list[dict] = []
        captured_results: list[dict] = []

        async def capture_event(_method: str, params: dict) -> None:
            if params.get("provider") == "envelope":
                captured_events.append(dict(params))

        async def capture_result(_method: str, params: dict) -> None:
            if params.get("provider") == "envelope":
                captured_results.append(dict(params))

        bus.on(Method.PROVIDER_EVENT, capture_event)
        bus.on(Method.PROVIDER_RESULT, capture_result)
        try:
            record = await runtime.start(
                ProviderRunRequest(
                    provider="envelope",
                    task="Inspect the disposable workspace",
                    cwd=str(tmp_path),
                    requirements=ProviderRequirements(task_kind="general"),
                    ownership="managed",
                    metadata={
                        "source": "provider_abstraction_test",
                        "provider_ownership": "attached",
                        "provider_requirements": {"task_kind": "spoofed"},
                        "provider_selection": {
                            "provider_id": "envelope",
                            "reason": "required_provider",
                        },
                    },
                )
            )
            assert record.task_handle is not None
            await record.task_handle

            assert [event["sequence"] for event in captured_events] == list(
                range(1, len(captured_events) + 1)
            )
            assert all(event["observed_at"] > 0 for event in captured_events)
            assert all(event["task_id"] for event in captured_events)
            assert all(event["attempt_id"] for event in captured_events)
            assert all(event["attempt_epoch"] == 1 for event in captured_events)
            assert all(event["ownership"] == "managed" for event in captured_events)
            progress = next(
                event for event in captured_events if event["type"] == "semantic.progress"
            )
            assert progress["replay"] is True
            assert progress["provider"] == "envelope"
            assert progress["run_id"] == record.run_id
            assert progress["metadata"]["provider_ownership"] == "managed"
            assert progress["metadata"]["provider_requirements"]["task_kind"] == "general"
            assert progress["metadata"]["work"]["work_item_id"] == progress["task_id"]
            assert progress["metadata"]["work"]["attempt_id"] == progress["attempt_id"]

            # New consumers can resolve the durable attempt without reaching
            # back into provider-specific metadata.work. Old events remain
            # supported by the coordinator's fallback below this path.
            event_without_metadata = {**progress, "metadata": {}}
            attempt = coordinator._attempt_for_event(event_without_metadata, adopt=False)
            assert attempt is not None
            assert attempt.attempt_id == progress["attempt_id"]

            assert captured_results
            terminal = captured_results[-1]
            assert terminal["task_id"] == progress["task_id"]
            assert terminal["attempt_id"] == progress["attempt_id"]
            assert terminal["attempt_epoch"] == 1
            assert terminal["event_sequence"] == len(captured_events)
            assert terminal["metadata"]["outcome_evidence"] == {
                "facet": "test.observed_state",
                "operation": "inspect",
                "expected": {},
                "observed": {"value": "host adapter receipt"},
                "observation_authority": "host",
                "pending_input": False,
                "schema_version": 1,
            }

            attempt = store.get_attempt(progress["attempt_id"])
            assert attempt is not None
            assert attempt.metadata["provider_requirements"]["task_kind"] == "general"
            assert attempt.metadata["provider_selection"]["reason"] == "required_provider"
        finally:
            bus.off(Method.PROVIDER_EVENT, capture_event)
            bus.off(Method.PROVIDER_RESULT, capture_result)
            runtime.set_request_preparer(None)
            coordinator.close()

    asyncio.run(scenario())


def test_runtime_discovery_adds_manifests_without_changing_provider_ids() -> None:
    runtime = ProviderRuntime()
    runtime.register(EnvelopeAdapter())
    assert runtime.list_providers() == ["envelope"]
    manifests = runtime.list_provider_manifests()
    assert [item["provider_id"] for item in manifests] == ["envelope"]
    assert manifests[0]["capabilities"]["task_kinds"] == ["general"]


def test_runtime_rejects_undeclared_attached_ownership() -> None:
    async def scenario() -> None:
        runtime = ProviderRuntime()
        runtime.register(EnvelopeAdapter())
        try:
            await runtime.start(
                ProviderRunRequest(
                    provider="envelope",
                    task="Attach an external task",
                    requirements=ProviderRequirements(ownership="attached"),
                    ownership="attached",
                )
            )
        except ValueError as exc:
            assert "does not support attached ownership" in str(exc)
        else:
            raise AssertionError("attached ownership must be declared by the provider")

    asyncio.run(scenario())


def test_provider_list_appends_manifests_without_replacing_legacy_ids() -> None:
    async def scenario() -> None:
        handler = ProviderHandler()
        response = await handler._list({})
        assert all(isinstance(provider, str) for provider in response["providers"])
        manifests = response["provider_manifests"]
        assert {item["provider_id"] for item in manifests} == set(response["providers"])
        assert all("capabilities" in item for item in manifests)

    asyncio.run(scenario())


def _main() -> None:
    test_manifest_is_semantic_and_serializable()
    test_manifest_can_declare_semantic_operations_without_provider_checks()
    test_invalid_operation_contracts_fail_at_manifest_construction()
    test_selector_matches_requirements_instead_of_provider_names()
    test_selector_distinguishes_require_prefer_and_force()
    test_no_compatible_provider_fails_explicitly()
    test_legacy_adapter_gets_conservative_compatibility_manifest()
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory(prefix="amadeus-provider-v02-") as temp_dir:
        test_runtime_envelope_and_work_ledger_identity_are_assembled(Path(temp_dir))
    test_runtime_discovery_adds_manifests_without_changing_provider_ids()
    test_runtime_rejects_undeclared_attached_ownership()
    test_provider_list_appends_manifests_without_replacing_legacy_ids()
    print("ok: provider abstraction v0.2 contract and envelope are assembled")


if __name__ == "__main__":
    _main()
