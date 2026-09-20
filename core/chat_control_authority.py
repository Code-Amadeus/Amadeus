"""Asynchronous handoff from role-model control proposals to host authority.

The inline transport commits a proposal as soon as its envelope closes.  This
module freezes that evidence, runs shadow or authority work off the role stream,
and replaces the history placeholder with the effective canonical control.
Identity grounding and work dispatch remain callbacks owned by ``ChatRuntime``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Callable
from typing import Any

from server.control_proposal import ControlProposalBatch, seal_control_proposals

logger = logging.getLogger("chat_control_authority")


def render_control_history_tag(action: dict[str, Any]) -> str:
    """Render only effective public control attributes for model history."""

    from llm.action_existence_protocol import (
        as_control_tag_text,
        control_envelope_enabled,
    )
    from llm.delegate_tool import as_tag_text

    attrs = action.get("attrs") if isinstance(action.get("attrs"), dict) else {}
    public_attrs = {
        key: value
        for key, value in attrs.items()
        if not str(key).startswith("_host_")
    }
    if control_envelope_enabled():
        return as_control_tag_text(public_attrs)
    return as_tag_text(public_attrs)


def publish_control_proposals(
    owner: Any,
    state: Any,
    actions: list[dict[str, Any]],
    *,
    transport: str,
    proposal_actions: list[dict[str, Any]] | None = None,
) -> ControlProposalBatch:
    """Seal a proposal and schedule its non-authoritative observer, if any."""

    snapshot = seal_control_proposals(
        proposal_actions if proposal_actions is not None else actions,
        turn_id=state.turn_id,
        session_id=state.session_id,
        user_text=state.question,
        transport=transport,
        prior_messages=state.control_prior_messages,
    )
    state.control_proposal_batches.append(snapshot)
    observer = getattr(owner, "_control_proposal_observer", None)

    # The optional compound shadow shares the immutable tag-closure snapshot
    # only while A owns authority. Compound authority captures the same
    # snapshot later through the one authority task, so never schedule a
    # duplicate B job in that mode.
    compound_capture = (
        None
        if bool(getattr(owner, "_compound_control_authority", False))
        else getattr(observer, "capture_compound_shadow", None)
    )
    compound_job = None
    if callable(compound_capture):
        try:
            compound_job = compound_capture(snapshot)
        except Exception:
            logger.exception("[COMPOUND-CONTROL-SHADOW] context capture failed")
    if inspect.isawaitable(compound_job):
        try:
            compound_task = asyncio.get_running_loop().create_task(compound_job)
            tasks = getattr(owner, "_control_proposal_observer_tasks", None)
            if tasks is None:
                tasks = set()
                owner._control_proposal_observer_tasks = tasks
            tasks.add(compound_task)
            compound_task.add_done_callback(tasks.discard)
        except RuntimeError:
            logger.warning(
                "[COMPOUND-CONTROL-SHADOW] proposal sealed without an event loop"
            )
            close = getattr(compound_job, "close", None)
            if callable(close):
                close()
    if bool(getattr(owner, "_control_proposal_authority", False)):
        return snapshot
    if observer is None:
        return snapshot
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "[CONTROL-SHADOW] proposal sealed without a running event loop; "
            "observer was not scheduled"
        )
        return snapshot

    capture = getattr(observer, "capture", None)
    try:
        captured_job = capture(snapshot) if callable(capture) else None
    except Exception:
        logger.exception("[CONTROL-SHADOW] proposal context capture failed")
        return snapshot

    async def run_observer() -> None:
        try:
            if captured_job is not None:
                result = captured_job
            elif inspect.iscoroutinefunction(observer):
                result = observer(snapshot)
            else:
                result = await asyncio.to_thread(observer, snapshot)
            if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[CONTROL-SHADOW] proposal observer failed")

    task = loop.create_task(run_observer())
    tasks = getattr(owner, "_control_proposal_observer_tasks", None)
    if tasks is None:
        tasks = set()
        owner._control_proposal_observer_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return snapshot


async def announce_control_authority_block(
    callback: Callable[..., Any] | None,
    resolution: Any,
    session_id: str,
) -> None:
    """Publish a visible fail-closed authority outcome without blocking speech."""

    if callback is None:
        return
    try:
        result = callback(resolution, session_id)
        if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
            await result
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[CONTROL-AUTHORITY] visible block callback failed")


def schedule_control_authority(
    owner: Any,
    state: Any,
    snapshot: ControlProposalBatch,
    fallback_actions: list[dict[str, Any]],
    *,
    record_actions_fn: Callable[[list[dict[str, Any]]], Any],
):
    """Adjudicate at envelope closure while role text continues streaming."""

    from server.control_authority import (
        adjudicate_control_authority,
        capture_control_authority,
        resolve_control_authority,
    )

    compound_authority = bool(
        getattr(owner, "_compound_control_authority", False)
    )
    authority_capture = capture_control_authority(
        owner._control_proposal_observer,
        snapshot,
        capture_method=(
            "capture_compound_shadow" if compound_authority else "capture"
        ),
    )
    placeholder = (
        f"\x00CONTROL_AUTHORITY:{state.turn_id}:{len(state.control_proposal_batches)}:"
        f"{time.monotonic_ns()}\x00"
    )
    state.history_response += placeholder
    fallback_controls = tuple(
        dict(action.get("attrs") or {})
        for action in fallback_actions
        if str(action.get("type") or "").upper() == "DELEGATE"
    )

    async def apply_resolution(resolution: Any) -> None:
        state.control_authority_resolved = True
        effective_actions = owner._prepare_authority_actions(
            state,
            resolution.actions,
        )
        # Canonical reconciliation may turn a role's non-Work proposal into
        # Work (for example report -> amend).  Start the orthogonal AUIP
        # decision from that effective shape before cross-axis composition;
        # starting it only after the guard leaves the guard with no owner to
        # consult and can let an application request escape as Provider Work.
        owner._start_auip_decision_for_work(state, effective_actions)
        effective_actions = await owner._guard_work_actions_against_auip(
            state,
            effective_actions,
            fallback_actions=fallback_actions,
        )
        state.control_effective_actions = effective_actions
        # History and dispatch must describe the same effective control.  The
        # authority/guard stages may add or remove public attrs after the first
        # proposal rendering, so regenerate the textual projection here rather
        # than retaining stale role/canonical evidence as the final tag.
        for action in effective_actions:
            action["raw"] = owner._control_history_tag(action)
        effective_tags = "".join(
            str(action.get("raw") or "") for action in effective_actions
        )
        state.history_response = state.history_response.replace(
            placeholder,
            effective_tags,
            1,
        )
        if effective_actions:
            dispatch_batch = record_actions_fn(effective_actions)
            if any(
                owner._delegate_action_starts_work(action)
                for action in effective_actions
            ):
                state.work_delegate_seen = True
                owner._schedule_auip_after_effective_work(state)
            owner._remember_taskless_focus(state, effective_actions, dispatch_batch)
            for action in effective_actions:
                attrs = (
                    action.get("attrs")
                    if isinstance(action.get("attrs"), dict)
                    else {}
                )
                if str(attrs.get("branch") or "").strip().lower() == "continue":
                    state.branch_continue_seen = True
        elif resolution.should_announce_block:
            await owner._announce_control_authority_block(resolution, state)

        logger.info(
            (
                "[CONTROL-AUTHORITY] turn_id=%s disposition=%s status=%s outcome=%s "
                "actions=%d notes=%d reason=%s"
            ),
            state.turn_id,
            resolution.disposition,
            resolution.decision_status,
            resolution.decision_outcome,
            len(effective_actions),
            len(resolution.notes),
            str(getattr(resolution, "reason", "") or "")[:400],
        )

    async def run_authority() -> None:
        try:
            resolution = await adjudicate_control_authority(
                authority_capture,
                fallback_actions=fallback_controls,
                timeout_s=owner._control_proposal_authority_timeout_s,
                allow_unavailable_fallback=not compound_authority,
            )
            await apply_resolution(resolution)
        except asyncio.CancelledError:
            state.history_response = state.history_response.replace(placeholder, "", 1)
            raise
        except Exception as exc:
            state.control_authority_resolved = True
            state.control_effective_actions = []
            state.history_response = state.history_response.replace(placeholder, "", 1)
            resolution = resolve_control_authority(
                decision_status="invalid",
                decision_outcome="invalid",
                reason=f"authority application failed: {type(exc).__name__}",
            )
            logger.exception("[CONTROL-AUTHORITY] application failed closed")
            await owner._announce_control_authority_block(resolution, state)

    loop = asyncio.get_running_loop()
    task = loop.create_task(run_authority())
    state.control_authority_tasks.append(task)
    owner._control_proposal_authority_tasks.add(task)
    task.add_done_callback(owner._control_proposal_authority_tasks.discard)
    return task


async def wait_for_control_authority(state: Any) -> None:
    """Finish this turn's bounded authority jobs before history projection."""

    tasks = tuple(getattr(state, "control_authority_tasks", ()) or ())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
