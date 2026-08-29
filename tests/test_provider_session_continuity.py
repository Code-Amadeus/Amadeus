"""Provider Session attachment stays subordinate to durable WorkItem identity."""

from __future__ import annotations

import tempfile
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.provider_catalog import (
    BROWSER_MANIFEST,
    CODEX_APP_SERVER_MANIFEST,
    OPENCLAW_MANIFEST,
)
from agent_host.provider_types import ProviderRunRequest, ProviderSessionHandle
from agent_host.provider_contract import ProviderRequirements, ProviderSelection
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from server.work_ledger_coordinator import WorkLedgerCoordinator
from server.work_steer_control import route_active_amendment
from server.app import _delegate_provider_selection, _handle_delegate


def test_workspace_less_work_item_attaches_its_provider_session() -> None:
    with tempfile.TemporaryDirectory(prefix="provider-session-ledger-") as temp:
        with WorkLedgerStore(Path(temp) / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            coordinator.configure()
            try:
                first = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="openclaw",
                        task="Find and summarize the Amadeus page.",
                        metadata={
                            "session_id": "voice-session",
                            "intent": "execute",
                            "provider_manifest": OPENCLAW_MANIFEST.to_dict(),
                        },
                    )
                )
                work_item_id = str(first.metadata["work"]["work_item_id"])
                item = store.get_work_item(work_item_id)
                assert item is not None and item.workspace_mode == "none"
                first_attempt = store.list_attempts(work_item_id)[-1]
                handle = ProviderSessionHandle(
                    provider="openclaw",
                    session_id="agent:main:dashboard:amadeus-ledger-test",
                    scope="work_item",
                )
                store.update_attempt(
                    first_attempt.attempt_id,
                    execution_status="succeeded",
                    metadata={"provider_session": handle.to_dict()},
                )

                # A terminal target must bypass the active steer/replacement
                # state machine. The ordinary amend path below then creates a
                # new Operation and lets the capability-driven Ledger attach
                # the typed Provider Session.
                route = asyncio.run(
                    route_active_amendment(
                        runtime=object(),
                        coordinator=coordinator,
                        work_item_id=work_item_id,
                        selected_provider="openclaw",
                        task_text="On that same page, inspect the first section.",
                        turn_id="turn-2",
                    )
                )
                assert route == {"handled": False}

                facts = coordinator.continuation_routing_facts(work_item_id)
                assert facts == {
                    "work_item_id": work_item_id,
                    "workspace_mode": "none",
                    "provider": "openclaw",
                }
                requirements, selection = _delegate_provider_selection(
                    "Click the first result on that page.",
                    {
                        "provider": "openclaw",
                        "intent": "amend",
                        "workspace_ref": work_item_id,
                    },
                    manifests=(
                        BROWSER_MANIFEST,
                        CODEX_APP_SERVER_MANIFEST,
                        OPENCLAW_MANIFEST,
                    ),
                )
                assert requirements.task_kind == "general"
                assert requirements.workspace_access == "none"
                assert selection.provider_id == "openclaw"

                browser_requirements, browser_selection = _delegate_provider_selection(
                    "Use a verified page reference to click the first result.",
                    {
                        "provider": "browser",
                        "intent": "amend",
                        "workspace_ref": work_item_id,
                        "action": "click_ref",
                        "branch": "continue",
                    },
                    manifests=(
                        BROWSER_MANIFEST,
                        CODEX_APP_SERVER_MANIFEST,
                        OPENCLAW_MANIFEST,
                    ),
                )
                assert browser_requirements.task_kind == "browser"
                assert browser_selection.provider_id == "browser"

                followup = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="openclaw",
                        task="On that same page, inspect the first section.",
                        metadata={
                            "session_id": "voice-session",
                            "intent": "amend",
                            "continuation": "amend",
                            "provider_manifest": OPENCLAW_MANIFEST.to_dict(),
                            "work": {"work_item_id": work_item_id},
                        },
                    )
                )
                assert followup.session == handle
                assert followup.cwd is None
                assert followup.metadata["work"]["work_item_id"] == work_item_id
                latest = store.list_attempts(work_item_id)[-1]
                assert latest.metadata["provider_session"] == handle.to_dict()
                assert latest.metadata["provider_session_attach"] == {
                    "state": "attached",
                    "provider": "openclaw",
                    "previous_attempt_id": first_attempt.attempt_id,
                }
                assert "replaces_attempt_id" not in latest.metadata
                operations = store.list_operations(work_item_id)
                assert len(operations) == 2
                assert operations[-1].metadata["previous_operation_id"] == (
                    first_attempt.operation_id
                )
            finally:
                coordinator.close()


def test_attach_capability_keeps_legacy_attempts_without_a_session_cold() -> None:
    with tempfile.TemporaryDirectory(prefix="provider-session-legacy-") as temp:
        with WorkLedgerStore(Path(temp) / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            coordinator.configure()
            try:
                first = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="openclaw",
                        task="Summarize the current page.",
                        metadata={
                            "session_id": "voice-session",
                            "intent": "execute",
                            "provider_manifest": OPENCLAW_MANIFEST.to_dict(),
                        },
                    )
                )
                work_item_id = str(first.metadata["work"]["work_item_id"])
                first_attempt = store.list_attempts(work_item_id)[-1]
                store.update_attempt(
                    first_attempt.attempt_id,
                    execution_status="succeeded",
                )

                followup = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="openclaw",
                        task="Now compare it with the previous section.",
                        metadata={
                            "session_id": "voice-session",
                            "intent": "amend",
                            "continuation": "amend",
                            "provider_manifest": OPENCLAW_MANIFEST.to_dict(),
                            "work": {"work_item_id": work_item_id},
                        },
                    )
                )

                assert followup.session is None
                latest = store.list_attempts(work_item_id)[-1]
                assert "provider_session" not in latest.metadata
                assert "provider_session_attach" not in latest.metadata
            finally:
                coordinator.close()


def test_attach_capability_rejects_a_malformed_stored_session() -> None:
    with tempfile.TemporaryDirectory(prefix="provider-session-malformed-") as temp:
        with WorkLedgerStore(Path(temp) / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            coordinator.configure()
            try:
                first = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="openclaw",
                        task="Summarize the current page.",
                        metadata={
                            "session_id": "voice-session",
                            "intent": "execute",
                            "provider_manifest": OPENCLAW_MANIFEST.to_dict(),
                        },
                    )
                )
                work_item_id = str(first.metadata["work"]["work_item_id"])
                first_attempt = store.list_attempts(work_item_id)[-1]
                store.update_attempt(
                    first_attempt.attempt_id,
                    execution_status="succeeded",
                    metadata={
                        "provider_session": {
                            "provider": "openclaw",
                            "session_id": "",
                            "scope": "work_item",
                            "version": 1,
                        }
                    },
                )

                try:
                    coordinator.prepare_request(
                        ProviderRunRequest(
                            provider="openclaw",
                            task="Continue from that result.",
                            metadata={
                                "session_id": "voice-session",
                                "intent": "amend",
                                "continuation": "amend",
                                "provider_manifest": OPENCLAW_MANIFEST.to_dict(),
                                "work": {"work_item_id": work_item_id},
                            },
                        )
                    )
                except WorkLedgerConflict as exc:
                    assert "stored provider session is invalid" in str(exc)
                else:
                    raise AssertionError("malformed session must fail closed")
            finally:
                coordinator.close()


def test_codex_claims_mid_run_steering_only_with_what_backs_it() -> None:
    """Codex now claims immediate steering, and the claim has preconditions.

    This previously asserted ``steering == "none"``, guarding against claiming
    a capability nothing implemented. The guard still matters, it just moved:
    steering here is abort-then-continue, so the claim is only honest while
    Codex can both stop a run for certain and carry its conversation into the
    next one. If either of those regresses, the claim has to come back down.
    """

    assert CODEX_APP_SERVER_MANIFEST.capabilities.steering == "immediate"
    assert CODEX_APP_SERVER_MANIFEST.capabilities.resume == "attach"
    assert CODEX_APP_SERVER_MANIFEST.capabilities.cancellation == "confirmed"


def test_intake_cannot_inject_a_session_without_work_item_lineage() -> None:
    with tempfile.TemporaryDirectory(prefix="provider-session-intake-") as temp:
        with WorkLedgerStore(Path(temp) / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            try:
                request = ProviderRunRequest(
                    provider="openclaw",
                    task="Start an unrelated lookup.",
                    metadata={
                        "session_id": "voice-session",
                        "intent": "execute",
                        "provider_manifest": OPENCLAW_MANIFEST.to_dict(),
                    },
                    session=ProviderSessionHandle(
                        provider="openclaw",
                        session_id="agent:main:dashboard:untrusted",
                        scope="work_item",
                    ),
                )
                prepared = coordinator.prepare_request(request)
                assert prepared.session is None
                attempt_id = str(prepared.metadata["work"]["attempt_id"])
                attempt = store.get_attempt(attempt_id)
                assert attempt is not None
                assert "provider_session" not in attempt.metadata
            finally:
                coordinator.close()


def test_workspace_less_delegate_keeps_the_work_item_operation_target() -> None:
    async def scenario() -> None:
        start = AsyncMock(
            return_value=SimpleNamespace(
                task_handle=None,
                result="continued",
                error="",
                metadata={"result_type": "ok"},
            )
        )
        requirements = ProviderRequirements(
            task_kind="general",
            workspace_access="none",
            preferred_provider="openclaw",
            preference_policy="require",
        )
        selection = ProviderSelection(
            provider_id="openclaw",
            reason="test",
            compatible_candidates=("openclaw",),
        )
        with (
            patch(
                "server.app._delegate_provider_selection",
                return_value=(requirements, selection),
            ),
            patch(
                "server.app._delegate_workspace_route",
                return_value={
                    "status": "resolved",
                    "cwd": None,
                    "projectId": "",
                    "source": "not_applicable",
                },
            ),
            patch(
                "agent_host.provider_runtime.runtime.get_manifest",
                return_value=OPENCLAW_MANIFEST,
            ),
            patch("agent_host.provider_runtime.runtime.start", new=start),
            patch(
                "server.work_ledger_coordinator.get_work_ledger_coordinator",
                return_value=None,
            ),
        ):
            result = await _handle_delegate(
                "Click the first result on that page.",
                {
                    "provider": "openclaw",
                    "intent": "amend",
                    "workspace_ref": "work-web",
                },
            )
        assert result == "continued"
        request = start.await_args.args[0]
        assert request.cwd is None
        assert request.metadata["continuation"] == "amend"
        assert request.metadata["work"] == {
            "workspace_ref": "work-web",
            "work_item_id": "work-web",
        }

    asyncio.run(scenario())


def test_session_provider_start_failure_does_not_fall_back_to_one_shot() -> None:
    async def scenario() -> None:
        requirements = ProviderRequirements(
            task_kind="general",
            workspace_access="none",
            preferred_provider="openclaw",
            preference_policy="require",
        )
        selection = ProviderSelection(
            provider_id="openclaw",
            reason="test",
            compatible_candidates=("openclaw",),
        )
        announce = AsyncMock()
        legacy_summary = AsyncMock()
        with (
            patch(
                "server.app._delegate_provider_selection",
                return_value=(requirements, selection),
            ),
            patch(
                "server.app._delegate_workspace_route",
                return_value={"status": "resolved", "cwd": None, "source": "not_applicable"},
            ),
            patch(
                "agent_host.provider_runtime.runtime.get_manifest",
                return_value=OPENCLAW_MANIFEST,
            ),
            patch(
                "agent_host.provider_runtime.runtime.start",
                new=AsyncMock(side_effect=RuntimeError("gateway unavailable")),
            ),
            patch("server.app._announce_provider_start_failure", new=announce),
            patch("server.app._speak_openclaw_delegate_result", new=legacy_summary),
        ):
            result = await _handle_delegate(
                "Find the official page.",
                {"provider": "openclaw", "intent": "execute"},
            )
        assert result == "[openclaw error] delegate execution failed"
        announce.assert_awaited_once()
        legacy_summary.assert_not_awaited()

    asyncio.run(scenario())


def _main() -> None:
    test_workspace_less_work_item_attaches_its_provider_session()
    test_attach_capability_keeps_legacy_attempts_without_a_session_cold()
    test_attach_capability_rejects_a_malformed_stored_session()
    test_codex_claims_mid_run_steering_only_with_what_backs_it()
    test_intake_cannot_inject_a_session_without_work_item_lineage()
    test_workspace_less_delegate_keeps_the_work_item_operation_target()
    test_session_provider_start_failure_does_not_fall_back_to_one_shot()
    print("ok: Provider Sessions attach only through WorkItem lineage")


if __name__ == "__main__":
    _main()
