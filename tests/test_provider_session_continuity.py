"""Provider Session attachment stays subordinate to durable WorkItem identity."""

from __future__ import annotations

import tempfile
import asyncio
import os
import sys
from dataclasses import fields, replace
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.provider_catalog import (
    CODEX_APP_SERVER_MANIFEST,
    OPENCLAW_MANIFEST,
)
from agent_host.provider_identity import (
    PARENT_CONTEXT_DELIVERY_METADATA_KEY,
    PARENT_CONTEXT_DELIVERED_EVENT,
    parent_context_delivery_receipt,
)
from agent_host.provider_types import ProviderEvent, ProviderRunRequest, ProviderSessionHandle
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from server.work_control import WorkAmendPayloadV4
from server.work_ledger_coordinator import WorkLedgerCoordinator
from test_work_effect_executor import _admission, _host, _payload


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
                            "turn_id": "turn-1",
                            "source_user_text": "Find and summarize the Amadeus page.",
                            "source_user_context": 'User: "We are researching Amadeus."',
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
                    metadata={
                        "provider_session": handle.to_dict(),
                        PARENT_CONTEXT_DELIVERY_METADATA_KEY: (
                            parent_context_delivery_receipt(
                                {
                                    "source_context_scope": "chat:voice-session",
                                    "turn_id": "turn-1",
                                    "source_user_text": (
                                        "Find and summarize the Amadeus page."
                                    ),
                                    "source_context_mode": "snapshot",
                                }
                            )
                        ),
                    },
                )

                facts = coordinator.continuation_routing_facts(work_item_id)
                assert facts == {
                    "work_item_id": work_item_id,
                    "workspace_mode": "none",
                    "provider": "openclaw",
                }
                followup = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="openclaw",
                        task="On that same page, inspect the first section.",
                        metadata={
                            "session_id": "voice-session",
                            "intent": "amend",
                            "turn_id": "turn-2",
                            "source_user_text": "Inspect the first section now.",
                            "source_user_context": "\n".join(
                                [
                                    'User: "We are researching Amadeus."',
                                    'User: "Find and summarize the Amadeus page."',
                                    'Main Chat: "I found the page and can continue."',
                                    'User: "Keep the comparison concise."',
                                ]
                            ),
                            "continuation": "amend",
                            "provider_manifest": OPENCLAW_MANIFEST.to_dict(),
                            "work": {"work_item_id": work_item_id},
                        },
                    )
                )
                assert followup.session == handle
                assert followup.cwd is None
                assert followup.metadata["source_context_mode"] == "delta"
                assert followup.metadata["source_context_base_turn_id"] == "turn-1"
                assert "We are researching Amadeus" not in followup.metadata[
                    "source_user_context"
                ]
                assert "Find and summarize" not in followup.metadata[
                    "source_user_context"
                ]
                assert "I found the page and can continue" in followup.metadata[
                    "source_user_context"
                ]
                assert "Keep the comparison concise" in followup.metadata[
                    "source_user_context"
                ]
                assert followup.metadata["work"]["work_item_id"] == work_item_id
                latest = store.list_attempts(work_item_id)[-1]
                assert latest.metadata["provider_session"] == handle.to_dict()
                assert latest.metadata["provider_session_attach"] == {
                    "state": "attached",
                    "provider": "openclaw",
                    "previous_attempt_id": first_attempt.attempt_id,
                }
                assert latest.metadata["source_context_mode"] == "delta"
                assert latest.metadata["source_context_base_turn_id"] == "turn-1"
                assert "replaces_attempt_id" not in latest.metadata
                operations = store.list_operations(work_item_id)
                assert len(operations) == 2
                assert operations[-1].metadata["previous_operation_id"] == (
                    first_attempt.operation_id
                )
            finally:
                coordinator.close()


def test_failed_prepared_attempt_does_not_advance_the_delivered_cursor() -> None:
    with tempfile.TemporaryDirectory(prefix="provider-context-failed-attempt-") as temp:
        with WorkLedgerStore(Path(temp) / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            coordinator.configure()
            try:
                first = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="openclaw",
                        task="Start the original research goal.",
                        metadata={
                            "session_id": "chat-A",
                            "intent": "execute",
                            "turn_id": "turn-1",
                            "source_user_text": "original goal",
                            "source_user_context": 'User: "earlier setup"',
                            "source_context_scope": "chat:chat-A",
                            "provider_manifest": OPENCLAW_MANIFEST.to_dict(),
                        },
                    )
                )
                work_item_id = str(first.metadata["work"]["work_item_id"])
                first_attempt = store.list_attempts(work_item_id)[-1]
                handle = ProviderSessionHandle(
                    provider="openclaw",
                    session_id="agent:main:dashboard:receipt-test",
                    scope="work_item",
                )
                store.update_attempt(
                    first_attempt.attempt_id,
                    execution_status="succeeded",
                    metadata={
                        "provider_session": handle.to_dict(),
                        PARENT_CONTEXT_DELIVERY_METADATA_KEY: (
                            parent_context_delivery_receipt(
                                {
                                    "source_context_scope": "chat:chat-A",
                                    "turn_id": "turn-1",
                                    "source_user_text": "original goal",
                                    "source_context_mode": "snapshot",
                                }
                            )
                        ),
                    },
                )

                second = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="openclaw",
                        task="Add the second constraint.",
                        metadata={
                            "session_id": "chat-A",
                            "intent": "amend",
                            "continuation": "amend",
                            "turn_id": "turn-2",
                            "source_user_text": "second constraint",
                            "source_user_context": "\n".join(
                                [
                                    'User: "original goal"',
                                    'Main Chat: "starting it"',
                                ]
                            ),
                            "source_context_scope": "chat:chat-A",
                            "provider_manifest": OPENCLAW_MANIFEST.to_dict(),
                            "work": {"work_item_id": work_item_id},
                        },
                    )
                )
                second_attempt = store.get_attempt(second.metadata["work"]["attempt_id"])
                assert second_attempt is not None
                assert second_attempt.metadata["provider_session"] == handle.to_dict()
                assert PARENT_CONTEXT_DELIVERY_METADATA_KEY not in second_attempt.metadata
                store.update_attempt(
                    second_attempt.attempt_id,
                    execution_status="failed",
                    error="provider failed before accepting the prompt",
                )

                third = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="openclaw",
                        task="Continue after the failed start.",
                        metadata={
                            "session_id": "chat-A",
                            "intent": "amend",
                            "continuation": "amend",
                            "turn_id": "turn-3",
                            "source_user_text": "continue now",
                            "source_user_context": "\n".join(
                                [
                                    'User: "original goal"',
                                    'Main Chat: "starting it"',
                                    'User: "second constraint"',
                                    'Main Chat: "provider start failed before delivery"',
                                ]
                            ),
                            "source_context_scope": "chat:chat-A",
                            "provider_manifest": OPENCLAW_MANIFEST.to_dict(),
                            "work": {"work_item_id": work_item_id},
                        },
                    )
                )

                assert third.metadata["source_context_mode"] == "delta"
                assert third.metadata["source_context_base_turn_id"] == "turn-1"
                delivered = third.metadata["source_user_context"]
                assert "original goal" not in delivered
                assert "second constraint" in delivered
                assert "provider start failed before delivery" in delivered
            finally:
                coordinator.close()


def test_provider_delta_never_crosses_parent_chat_sessions() -> None:
    with tempfile.TemporaryDirectory(prefix="provider-context-cross-chat-") as temp:
        with WorkLedgerStore(Path(temp) / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            coordinator.configure()
            try:
                first = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="openclaw",
                        task="Start in chat A.",
                        metadata={
                            "session_id": "chat-A",
                            "intent": "execute",
                            "turn_id": "turn-A",
                            "source_user_text": "same old sentence",
                            "source_context_scope": "chat:chat-A",
                            "provider_manifest": OPENCLAW_MANIFEST.to_dict(),
                        },
                    )
                )
                work_item_id = str(first.metadata["work"]["work_item_id"])
                first_attempt = store.list_attempts(work_item_id)[-1]
                handle = ProviderSessionHandle(
                    provider="openclaw",
                    session_id="agent:main:dashboard:cross-chat-test",
                    scope="work_item",
                )
                store.update_attempt(
                    first_attempt.attempt_id,
                    execution_status="succeeded",
                    metadata={
                        "provider_session": handle.to_dict(),
                        PARENT_CONTEXT_DELIVERY_METADATA_KEY: (
                            parent_context_delivery_receipt(
                                {
                                    "source_context_scope": "chat:chat-A",
                                    "turn_id": "turn-A",
                                    "source_user_text": "same old sentence",
                                    "source_context_mode": "snapshot",
                                }
                            )
                        ),
                    },
                )

                second = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="openclaw",
                        task="Continue the WorkItem from chat B.",
                        metadata={
                            "session_id": "chat-B",
                            "intent": "amend",
                            "continuation": "amend",
                            "turn_id": "turn-B",
                            "source_user_text": "continue from chat B",
                            "source_user_context": "\n".join(
                                [
                                    'User: "chat-B goal"',
                                    'User: "same old sentence"',
                                    'Main Chat: "chat-B constraint"',
                                ]
                            ),
                            "source_context_scope": "chat:chat-B",
                            "provider_manifest": OPENCLAW_MANIFEST.to_dict(),
                            "work": {"work_item_id": work_item_id},
                        },
                    )
                )

                assert second.metadata["source_context_mode"] == "snapshot_fallback"
                assert second.session == handle
                delivered = second.metadata["source_user_context"]
                assert "chat-B goal" in delivered
                assert "same old sentence" in delivered
                assert "chat-B constraint" in delivered
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


def test_accepted_amend_receives_only_delivered_parent_context_delta() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="accepted-session-context-") as temp:
            async with _host(Path(temp), source="上一轮当前请求。", task="上一轮当前请求。") as host:
                handle = ProviderSessionHandle(provider=host.adapter.provider_id,
                    session_id="accepted-native-session", scope="work_item")
                host.adapter.manifest = replace(host.adapter.manifest,
                    capabilities=replace(host.adapter.manifest.capabilities, resume="attach"))
                host.runtime.register(host.adapter)
                original_run = host.adapter.run

                async def delivered_run(request, run_id, emit):
                    await emit(ProviderEvent(provider=host.adapter.provider_id, run_id=run_id,
                        type=PARENT_CONTEXT_DELIVERED_EVENT, metadata=dict(request.metadata)))
                    result = await original_run(request, run_id, emit)
                    result.session = handle
                    return result

                host.adapter.run = delivered_run
                first = await host.executor.execute(host.effect_id)
                work_id = first["binding"]["work_item_id"]
                previous = host.work.get_attempt(first["binding"]["attempt_id"])
                assert previous is not None
                assert previous.metadata[PARENT_CONTEXT_DELIVERY_METADATA_KEY][
                    "source_turn_id"] == "turn-one"

                source = "把新约束也加上。"
                admission = _admission(suffix="two", epoch=2, text=source)
                host.control.admit(admission, fence_scope="foreground-chat")
                base = replace(_payload(host.project.project_id, host.adapter.provider_id,
                    suffix="two", source=source, task=source), source_user_context="\n".join([
                        'User: "很早以前的目标。"',
                        'User: "上一轮当前请求。"',
                        'Main Chat: "上一轮结束后的回复。"',
                        'User: "两轮之间的新约束。"',
                    ]))
                payload = WorkAmendPayloadV4(
                    **{field.name: getattr(base, field.name) for field in fields(base)},
                    work_item_id=work_id)
                effect_id = host.control.seal(admission, payload)["effect_id"]
                outcome = await host.executor.execute(effect_id)
                request = host.adapter.requests[-1]["request"]
                assert request.session == handle
                assert outcome["binding"]["work_item_id"] == work_id
                assert outcome["receipt"]["outcome"] == "succeeded"
                assert request.metadata["source_context_mode"] == "delta"
                assert request.metadata["source_context_base_turn_id"] == "turn-one"
                assert "很早以前的目标" not in request.metadata["source_user_context"]
                assert "上一轮当前请求" not in request.metadata["source_user_context"]
                assert "上一轮结束后的回复" in request.metadata["source_user_context"]
                assert "两轮之间的新约束" in request.metadata["source_user_context"]
                assert len(host.work.list_attempts(work_id)) == 2

    asyncio.run(scenario())


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
