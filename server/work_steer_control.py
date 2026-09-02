"""Provider-neutral routing for amendments aimed at an active Attempt."""

from __future__ import annotations

import logging
import time
from typing import Any

from agent_host.provider_identity import (
    PARENT_CONTEXT_DELIVERY_METADATA_KEY,
    parent_conversation_context_delivery,
    validated_parent_context_delivery,
)
from agent_host.provider_types import ProviderSteerRequest
from server.ai_os_schema import work_note_payload, work_signal
from server.event_bus import bus
from server.protocol import Method
from server.work_context import add_work_note

logger = logging.getLogger(__name__)


async def route_active_amendment(
    *,
    runtime,
    coordinator,
    work_item_id: str,
    selected_provider: str,
    task_text: str,
    turn_id: str,
    source_user_text: str = "",
    source_user_context: str = "",
    source_context_scope: str = "",
) -> dict[str, Any]:
    """Steer an active target natively or prepare a confirmed replacement.

    A false ``handled`` with no replacement means the target is no longer
    active and the caller should preserve the established post-run amendment
    path.  A replacement is returned only after external cancellation was
    confirmed and the old writer lease was released by the Ledger event path.
    """

    active = coordinator.active_attempt_for_item(work_item_id)
    if active is None:
        return {"handled": False}
    record = runtime.get_run(active.provider_run_id)
    if record is None or str(record.status or "").strip().lower() not in {"queued", "running"}:
        await _announce_failure(
            active.provider,
            "The ledger still shows that task as active, but its live run is not attached, "
            "so I did not start a second writer. Check or resume the task first.",
            reason="active_runtime_missing",
        )
        return {"handled": True, "message": "[amend blocked] active runtime is unavailable"}
    if active.provider.strip().lower() != str(selected_provider or "").strip().lower():
        await _announce_failure(
            active.provider,
            "That task is already running with a different executor, so I did not switch "
            "executors in the middle of its workspace changes.",
            reason="provider_change_during_active_amendment",
        )
        return {"handled": True, "message": "[amend blocked] active provider differs"}
    manifest = runtime.get_manifest(active.provider)
    if manifest is None:
        await _announce_failure(
            active.provider,
            "The active executor is no longer registered, so the change could not be applied.",
            reason="active_provider_unregistered",
        )
        return {"handled": True, "message": "[amend blocked] provider unavailable"}
    if manifest.capabilities.steering == "immediate":
        steering = (
            record.metadata.get("steering")
            if isinstance(record.metadata.get("steering"), dict)
            else {}
        )
        revision = max(0, int(steering.get("revision") or 0)) + 1
        previous_delivery = validated_parent_context_delivery(
            record.metadata.get(PARENT_CONTEXT_DELIVERY_METADATA_KEY)
        )
        delivered_context, context_mode = parent_conversation_context_delivery(
            source_user_context,
            source_scope=source_context_scope,
            previous_delivery=previous_delivery,
            continuity_verified=True,
        )
        base_turn_id = str(
            previous_delivery.get("source_turn_id")
            or ""
        ).strip()
        outcome = await runtime.steer(
            active.provider_run_id,
            ProviderSteerRequest(
                task=str(task_text or ""),
                revision=revision,
                metadata={
                    "turn_id": str(turn_id or ""),
                    "work_item_id": active.work_item_id,
                    "attempt_id": active.attempt_id,
                    **(
                        {"source_user_text": str(source_user_text)[:4000]}
                        if str(source_user_text or "").strip()
                        else {}
                    ),
                    **(
                        {"source_user_context": delivered_context}
                        if delivered_context
                        else {}
                    ),
                    "source_context_mode": context_mode,
                    **(
                        {"source_context_scope": str(source_context_scope)[:800]}
                        if str(source_context_scope or "").strip()
                        else {}
                    ),
                    **(
                        {"source_context_base_turn_id": base_turn_id[:200]}
                        if base_turn_id
                        else {}
                    ),
                },
            ),
        )
        if outcome.get("accepted") is True:
            return {"handled": True, "message": "[amend] active run steered"}
        # A refusal that already stopped the run must not be reported as "still
        # running". Falling through to the replacement path is the tempting fix
        # but not a safe one here: that path re-cancels a run this refusal has
        # already ended, and reports an unconfirmed stop when it finds nothing
        # to cancel. Until that path can express "already stopped", the honest
        # move is to say what happened and leave the decision with the user.
        if outcome.get("aborted") is True:
            await _announce_failure(
                active.provider,
                "I stopped the active run, but could not carry it forward, so the "
                "change was not applied and nothing is running now.",
                reason=str(outcome.get("reason") or "steer_aborted_without_continuation"),
            )
            return {"handled": True, "message": "[amend blocked] steer aborted without continuation"}
        await _announce_failure(
            active.provider,
            "The active executor did not accept the change, so its current plan is still running.",
            reason=str(outcome.get("reason") or "native_steer_rejected"),
        )
        return {"handled": True, "message": "[amend blocked] native steer rejected"}
    if manifest.capabilities.cancellation != "confirmed":
        await _announce_failure(
            active.provider,
            "This executor cannot confirm that it has stopped, so I did not risk starting "
            "another writer in the same workspace.",
            reason="confirmed_cancellation_unavailable",
        )
        return {"handled": True, "message": "[amend blocked] cancellation is not confirmable"}
    try:
        replacement = coordinator.begin_steer_replacement(
            work_item_id,
            provider_run_id=active.provider_run_id,
            amendment_text=task_text,
        )
    except Exception as exc:
        await _announce_failure(
            active.provider,
            "I could not bind that change to the active task, so I left the current run alone.",
            reason=str(exc) or exc.__class__.__name__,
        )
        return {"handled": True, "message": "[amend blocked] replacement could not be prepared"}
    control = dict(replacement.get("control") or {})
    cancel_outcome = await runtime.cancel(
        active.provider_run_id,
        reason="steer_replacement",
        metadata={
            "work_item_id": active.work_item_id,
            "attempt_id": active.attempt_id,
            "replacement_revision": int(control.get("revision") or 0),
        },
    )
    if cancel_outcome.get("cancelled") is not True:
        reason = str(cancel_outcome.get("reason") or "cancel_unconfirmed")
        coordinator.reject_steer_replacement(active.attempt_id, reason=reason)
        await coordinator.publish_snapshot(reason="steer_replacement.rejected")
        await _announce_failure(
            active.provider,
            "The executor did not confirm that it stopped, so I did not start the revised run. "
            "The existing task remains the only writer.",
            reason=reason,
        )
        return {"handled": True, "message": "[amend blocked] cancellation unconfirmed"}
    return {"handled": False, "replacement": replacement}


async def _announce_failure(provider: str, summary: str, *, reason: str) -> None:
    """Correct the model's promise when an active amendment was not applied."""

    try:
        from core import session_manager as sm

        note = work_note_payload(
            source="work_control",
            provider=str(provider or "provider"),
            run_id=f"steer_{time.time_ns()}",
            session_id=sm.get_current_session_id() or "",
            phase="Result",
            title="Requested change was not applied",
            summary=summary,
            signals=[
                work_signal(
                    label="steer",
                    text="The active task was not replaced",
                    detail=reason,
                    kind="status",
                    importance="blocking",
                )
            ],
            importance="blocking",
            metadata={"steer_unresolved": True, "reason": reason},
            speak=True,
        )
        add_work_note(note)
        await bus.emit(Method.CHAT_WORK_NOTE, note)
    except Exception:
        logger.exception("failed to announce an unresolved steer")
