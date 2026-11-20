"""Attach to an already-running Amadeus backend and record live evidence.

The default mode is passive: it sends no product command and changes no
runtime state.  ``--mode turn`` is an explicit active canary.  It creates or
loads a dedicated Chat Session, sends one normal ``chat.send`` request, records
the same events seen by Electron, and restores the previously selected Session.

Examples::

    # Observe a manual voice/GUI journey for two minutes.
    python tools/live_runtime_acceptance.py --seconds 120

    # One isolated no-side-effect control canary.
    python tools/live_runtime_acceptance.py --mode turn \
      --say "这不是新的任务吧，你理解错了吗" --expect no-work

    # Continue a named canary Session in a later invocation.
    python tools/live_runtime_acceptance.py --mode turn \
      --session-id live-canary-game --say "算了，改成4次吧" --expect amend

This is live integration evidence, not a visual or acoustic oracle.  TTS and
Canvas events prove delivery to those surfaces; they do not prove that a human
heard the speaker or that Electron rendered an unobstructed control.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.e2e_real_work_conversation import (  # noqa: E402
    EventRecord,
    WsProbe,
    _safe_excerpt,
)


DEFAULT_URL = "ws://127.0.0.1:17777/ws"
DEFAULT_REPORT_DIR = ROOT / "runtime" / "live_acceptance"
EVIDENCE_METHODS = {
    "chat.complete",
    "chat.error",
    "chat.interrupted",
    "chat.work_note",
    "chat.work_note_delivered",
    "chat.observer_decision",
    "provider.event",
    "provider.result",
    "work.updated",
    "attention.updated",
    "auip.launch.requested",
    "auip.surface.close.requested",
    "auip.updated",
    "auip.action.requested",
    "wallpaper.canvas",
    "tts.status",
    "tts.sentence_start",
    "tts.sentence_end",
    "tts.turn_complete",
    "session.changed",
}
PRESENTATION_ACTIVITY_METHODS = {
    "chat.complete",
    "chat.error",
    "chat.interrupted",
    "chat.work_note",
    "chat.work_note_delivered",
    "chat.observer_decision",
    "tts.status",
    "tts.sentence_start",
    "tts.sentence_end",
    "tts.turn_complete",
}
TERMINAL_PROVIDER_TYPES = {
    "run.finished",
    "run.failed",
    "run.cancelled",
    "run.canceled",
}
TERMINAL_PROVIDER_STATUSES = {
    "done",
    "succeeded",
    "completed",
    "failed",
    "error",
    "cancelled",
    "canceled",
    "denied",
}
SUCCESSFUL_PROVIDER_STATUSES = {"done", "succeeded", "completed"}


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _work_projection(response: dict[str, Any]) -> dict[str, Any]:
    value = response.get("work")
    if isinstance(value, dict):
        return value
    value = response.get("projection")
    return value if isinstance(value, dict) else {}


def _work_ids(response: dict[str, Any]) -> set[str]:
    projection = _work_projection(response)
    return {
        str(item.get("id") or item.get("workItemId") or "").strip()
        for item in projection.get("items") or []
        if isinstance(item, dict)
        and str(item.get("id") or item.get("workItemId") or "").strip()
    }


def _event_type(event: EventRecord) -> str:
    return str(event.params.get("type") or "").strip().lower()


def _event_run_id(event: EventRecord) -> str:
    payload = event.params.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    return str(
        event.params.get("run_id")
        or event.params.get("runId")
        or payload.get("run_id")
        or payload.get("runId")
        or ""
    ).strip()


def _event_provider(event: EventRecord) -> str:
    return str(event.params.get("provider") or "").strip().lower()


def _event_metadata(event: EventRecord) -> dict[str, Any]:
    metadata = event.params.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    payload = event.params.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("metadata"), dict):
        return payload["metadata"]
    return {}


def _run_created_events(events: Iterable[EventRecord]) -> list[EventRecord]:
    created: list[EventRecord] = []
    seen_run_ids: set[str] = set()
    for event in events:
        if event.method != "provider.event" or _event_type(event) != "run.created":
            continue
        run_id = _event_run_id(event)
        if run_id and run_id in seen_run_ids:
            continue
        created.append(event)
        if run_id:
            seen_run_ids.add(run_id)
    return created


def _event_work_binding(event: EventRecord) -> dict[str, Any]:
    payload = event.params.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    metadata = _event_metadata(event)
    for owner in (event.params, payload, metadata):
        work = owner.get("work")
        if isinstance(work, dict):
            return work
    return {}


def _progress_only_recovery_context(event: EventRecord) -> dict[str, Any]:
    value = _event_metadata(event).get("provider_recovery")
    return value if isinstance(value, dict) else {}


def is_bounded_progress_recovery_chain(created: list[EventRecord]) -> bool:
    """Accept one run or one Host-signed progress-only successor, never more."""

    if len(created) == 1:
        return True
    if len(created) != 2:
        return False
    predecessor, successor = created
    predecessor_work = _event_work_binding(predecessor)
    successor_work = _event_work_binding(successor)
    recovery = _progress_only_recovery_context(successor)
    try:
        recovery_ordinal = int(recovery.get("ordinal") or 0)
    except (TypeError, ValueError):
        return False
    predecessor_attempt_id = str(
        predecessor_work.get("attempt_id") or predecessor.params.get("attempt_id") or ""
    ).strip()
    successor_attempt_id = str(
        successor_work.get("attempt_id") or successor.params.get("attempt_id") or ""
    ).strip()
    predecessor_provider = _event_provider(predecessor)
    predecessor_work_item_id = str(
        predecessor_work.get("work_item_id") or ""
    ).strip()
    predecessor_operation_id = str(
        predecessor_work.get("operation_id") or ""
    ).strip()
    return bool(
        predecessor_attempt_id
        and successor_attempt_id
        and predecessor_provider
        and predecessor_work_item_id
        and predecessor_operation_id
        and predecessor_attempt_id != successor_attempt_id
        and recovery.get("reason") == "progress_only_completion"
        and recovery_ordinal == 1
        and recovery.get("root_attempt_id") == predecessor_attempt_id
        and recovery.get("predecessor_attempt_id") == predecessor_attempt_id
        and _event_run_id(predecessor) != _event_run_id(successor)
        and predecessor_provider == _event_provider(successor)
        and predecessor_work_item_id
        == str(successor_work.get("work_item_id") or "").strip()
        and predecessor_operation_id
        == str(successor_work.get("operation_id") or "").strip()
    )


def progress_recovery_successor(
    events: Iterable[EventRecord],
    predecessor: EventRecord,
) -> EventRecord | None:
    predecessor_attempt_id = str(
        _event_work_binding(predecessor).get("attempt_id") or ""
    ).strip()
    if not predecessor_attempt_id:
        return None
    for candidate in _run_created_events(events):
        recovery = _progress_only_recovery_context(candidate)
        try:
            recovery_ordinal = int(recovery.get("ordinal") or 0)
        except (TypeError, ValueError):
            continue
        if (
            recovery.get("reason") == "progress_only_completion"
            and recovery_ordinal == 1
            and recovery.get("predecessor_attempt_id") == predecessor_attempt_id
            and is_bounded_progress_recovery_chain([predecessor, candidate])
        ):
            return candidate
    return None


def _is_terminal_for_run(event: EventRecord, run_id: str) -> bool:
    if _event_run_id(event) != run_id:
        return False
    if event.method == "provider.result":
        return True
    if event.method != "provider.event":
        return False
    if _event_type(event) in TERMINAL_PROVIDER_TYPES:
        return True
    payload = event.params.get("payload")
    status = str(
        (payload.get("status") if isinstance(payload, dict) else "")
        or event.params.get("status")
        or ""
    ).strip().lower()
    return status in TERMINAL_PROVIDER_STATUSES


def _is_provider_result_for_run(event: EventRecord, run_id: str) -> bool:
    return event.method == "provider.result" and _event_run_id(event) == run_id


def _is_terminal_observer_decision_for_run(
    event: EventRecord,
    run_id: str,
) -> bool:
    return (
        event.method == "chat.observer_decision"
        and str(event.params.get("run_id") or "").strip() == run_id
        and bool(event.params.get("terminal"))
        and str(event.params.get("action") or "").strip().lower()
        == "final_report"
    )


def _related_work_ids(events: Iterable[EventRecord]) -> set[str]:
    result: set[str] = set()
    for event in events:
        payload = event.params.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        metadata = event.params.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        payload_metadata = payload.get("metadata")
        payload_metadata = (
            payload_metadata if isinstance(payload_metadata, dict) else {}
        )
        for owner in (event.params, payload, metadata, payload_metadata):
            work = owner.get("work")
            if not isinstance(work, dict):
                continue
            value = str(
                work.get("work_item_id") or work.get("workItemId") or ""
            ).strip()
            if value:
                result.add(value)
    return result


def active_canary_block_reason(
    runtime_status: dict[str, Any],
    *,
    allow_active_work: bool,
    allow_chat_busy: bool = False,
) -> str:
    """Why an active canary must not share the live product runtime now."""

    chat = runtime_status.get("chat")
    if isinstance(chat, dict) and chat.get("busy") and not allow_chat_busy:
        return "chat_busy"
    provider = runtime_status.get("provider")
    active_runs = provider.get("active_runs") if isinstance(provider, dict) else []
    if active_runs and not allow_active_work:
        return "provider_work_active"
    return ""


def evaluate_turn(
    *,
    expect: str,
    events: list[EventRecord],
    before_work: dict[str, Any],
    after_work: dict[str, Any],
    chat_completed: bool,
    require_terminal_success: bool = True,
    expected_work_item_id: str = "",
    output_idle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate only externally observable contracts for one attached turn."""

    created = _run_created_events(events)
    before_ids = _work_ids(before_work)
    after_ids = _work_ids(after_work)
    new_work_ids = sorted(after_ids - before_ids)
    providers = [_event_provider(event) for event in created]
    failures: list[str] = []
    if not chat_completed:
        failures.append("chat.complete was not observed")

    clean_expect = str(expect or "any").strip().lower()
    auip_expected = clean_expect in {"auip-launch", "auip-leave"} or clean_expect.startswith(
        "auip-mode:"
    )
    if clean_expect == "no-work" or auip_expected:
        if created:
            failures.append("a Provider run started on a no-work turn")
        if new_work_ids:
            failures.append("a WorkItem was created on a no-work turn")
    if clean_expect == "no-work":
        pass
    elif clean_expect == "work":
        if not created:
            failures.append("no Provider run started")
    elif clean_expect == "amend":
        if not created:
            failures.append("no Provider run started for the amendment")
        if new_work_ids:
            failures.append("the amendment forked a new WorkItem")
    elif clean_expect.startswith("provider:"):
        expected_provider = clean_expect.partition(":")[2].strip()
        if not expected_provider:
            failures.append("provider expectation is missing its provider id")
        elif expected_provider not in providers:
            failures.append(
                f"expected provider {expected_provider!r}, observed {providers!r}"
            )
    elif clean_expect in {"auip-launch", "auip-prepare"}:
        if clean_expect == "auip-prepare":
            if not created:
                failures.append("no Provider run started to prepare the AUIP application")
            elif not is_bounded_progress_recovery_chain(created):
                failures.append(
                    "AUIP preparation started an unbounded or unrelated Provider run"
                )
            if new_work_ids:
                failures.append("AUIP preparation forked a new WorkItem")
            if created and not any(
                str(_event_metadata(event).get("source") or "").strip()
                == "auip_prepare"
                for event in created
            ):
                failures.append(
                    "the Provider run was not attributed to AUIP preparation"
                )
        if not any(event.method == "auip.launch.requested" for event in events):
            failures.append("no AUIP launch request was observed")
        if not any(
            event.method == "auip.updated"
            and str(event.params.get("status") or "").strip().lower() == "active"
            for event in events
        ):
            failures.append("the AUIP application did not register an active AppSession")
        if any(
            event.method == "chat.work_note"
            and isinstance(event.params.get("metadata"), dict)
            and event.params["metadata"].get("auip_launch_failed") is True
            for event in events
        ):
            failures.append("the trusted desktop reported an AUIP launch failure")
    elif clean_expect.startswith("auip-mode:"):
        expected_mode = clean_expect.partition(":")[2].strip()
        if any(event.method == "auip.launch.requested" for event in events):
            failures.append("AUIP mode change relaunched the application")
        if expected_mode not in {"observe", "collaborate", "delegate"}:
            failures.append(f"unsupported AUIP mode expectation: {expected_mode!r}")
        elif not any(
            event.method == "auip.updated"
            and str(event.params.get("status") or "").strip().lower() == "active"
            and str(event.params.get("engagement_mode") or "").strip().lower()
            == expected_mode
            for event in events
        ):
            failures.append(f"AUIP mode did not become {expected_mode!r}")
    elif clean_expect == "auip-leave":
        closed_events = [
            event
            for event in events
            if event.method == "auip.updated"
            and str(event.params.get("status") or "").strip().lower() == "closed"
        ]
        if not closed_events:
            failures.append("the AUIP AppSession did not close")
        owned_surface = any(
            str(event.params.get("host_surface_id") or "").strip()
            for event in closed_events
        )
        if owned_surface and not any(
            event.method == "auip.surface.close.requested" for event in events
        ):
            failures.append("no close request reached the trusted desktop surface")
        if owned_surface and not any(
            event.method == "auip.updated"
            and (
                event.params.get("host_surface_closed") is True
                or str(event.params.get("surface_close_status") or "")
                .strip()
                .lower()
                == "closed"
            )
            for event in events
        ):
            failures.append("the trusted desktop surface did not confirm closure")
    elif clean_expect != "any":
        failures.append(f"unsupported expectation: {expect!r}")

    terminal_statuses: dict[str, str] = {}
    work_expected = clean_expect in {
        "work",
        "amend",
        "auip-prepare",
    } or clean_expect.startswith("provider:")
    if work_expected and require_terminal_success:
        results = {
            _event_run_id(event): event
            for event in events
            if event.method == "provider.result" and _event_run_id(event)
        }
        recovery_chain = is_bounded_progress_recovery_chain(created) and len(created) == 2
        recovered_predecessor_run = _event_run_id(created[0]) if recovery_chain else ""
        for event in created:
            run_id = _event_run_id(event)
            result = results.get(run_id)
            if result is None:
                failures.append(f"provider run {run_id or '<unknown>'} has no terminal result")
                continue
            status = str(result.params.get("status") or "").strip().lower()
            terminal_statuses[run_id] = status
            if status not in SUCCESSFUL_PROVIDER_STATUSES:
                result_metadata = _event_metadata(result)
                completion = (
                    result_metadata.get("provider_completion")
                    if isinstance(result_metadata.get("provider_completion"), dict)
                    else {}
                )
                if (
                    run_id == recovered_predecessor_run
                    and completion.get("classification")
                    == "progress_only_completion"
                ):
                    continue
                failures.append(
                    f"provider run {run_id} finished with non-success status {status!r}"
                )

    observed_work_item_ids = _related_work_ids(events)
    observed_work_item_ids.update(
        str(event.params.get("work_item_id") or "").strip()
        for event in events
        if event.method == "chat.complete"
        and str(event.params.get("work_item_id") or "").strip()
    )
    expected_id = str(expected_work_item_id or "").strip()
    if expected_id and expected_id not in observed_work_item_ids:
        failures.append(
            f"expected WorkItem {expected_id}, observed {sorted(observed_work_item_ids)!r}"
        )

    last_interrupt_index = max(
        (
            index
            for index, event in enumerate(events)
            if event.method == "chat.interrupted"
        ),
        default=-1,
    )
    presentation_events = events[last_interrupt_index + 1 :]
    started_sentences = {
        str(event.params.get("sentence_id") or "").strip()
        for event in presentation_events
        if event.method == "tts.sentence_start"
        and str(event.params.get("sentence_id") or "").strip()
    }
    ended_sentences = {
        str(event.params.get("sentence_id") or "").strip()
        for event in presentation_events
        if event.method == "tts.sentence_end"
        and str(event.params.get("sentence_id") or "").strip()
    }
    unclosed_sentences = sorted(started_sentences - ended_sentences)
    presentation_boundary_required = require_terminal_success or not work_expected
    if presentation_boundary_required and unclosed_sentences and any(
        event.method == "tts.turn_complete" for event in presentation_events
    ):
        failures.append(
            "TTS turn completed with unclosed sentence events: "
            + ", ".join(unclosed_sentences)
        )
    last_tts_start = max(
        (
            index
            for index, event in enumerate(presentation_events)
            if event.method == "tts.sentence_start"
        ),
        default=-1,
    )
    last_tts_complete = max(
        (
            index
            for index, event in enumerate(presentation_events)
            if event.method == "tts.turn_complete"
        ),
        default=-1,
    )
    latest_tts_closed = not unclosed_sentences and _runtime_output_is_idle(output_idle)
    if presentation_boundary_required and (
        last_tts_start >= 0
        and last_tts_complete < last_tts_start
        and not latest_tts_closed
    ):
        failures.append("the latest TTS utterance did not reach tts.turn_complete")

    return {
        "status": "passed" if not failures else "failed",
        "expect": clean_expect,
        "failures": failures,
        "provider_runs_started": len(created),
        "providers": providers,
        "run_ids": [_event_run_id(event) for event in created],
        "terminal_statuses": terminal_statuses,
        "new_work_item_ids": new_work_ids,
        "related_work_item_ids": sorted(observed_work_item_ids),
        "unclosed_tts_sentence_ids": unclosed_sentences,
        "latest_tts_utterance_completed": (
            last_tts_start < 0
            or last_tts_complete > last_tts_start
            or latest_tts_closed
        ),
    }


def _runtime_output_is_idle(snapshot: dict[str, Any] | None) -> bool:
    if not isinstance(snapshot, dict) or not snapshot:
        return False
    chat = snapshot.get("chat") if isinstance(snapshot.get("chat"), dict) else {}
    tts = snapshot.get("tts") if isinstance(snapshot.get("tts"), dict) else {}
    playback = (
        snapshot.get("playback")
        if isinstance(snapshot.get("playback"), dict)
        else {}
    )
    return bool(
        chat.get("busy") is False
        and int(tts.get("pending_sentences") or 0) == 0
        and playback.get("is_playing") is False
        and int(playback.get("pending_audio") or 0) == 0
    )


def _compact_event(event: EventRecord) -> dict[str, Any]:
    return {
        "elapsed_s": round(event.elapsed_s, 3),
        "method": event.method,
        "params": _safe_excerpt(event.params, 2000),
    }


def _event_line(event: EventRecord) -> str:
    method = event.method
    params = event.params
    if method == "provider.event":
        return (
            f"{method} provider={_event_provider(event)} "
            f"type={_event_type(event)} run={_event_run_id(event)}"
        )
    if method == "provider.result":
        return (
            f"{method} provider={_event_provider(event)} "
            f"status={params.get('status', '')} run={_event_run_id(event)}"
        )
    if method == "chat.complete":
        text = " ".join(str(params.get("full_text") or "").split())
        return f"{method} turn={params.get('turn_id', '')} text={text[:120]}"
    if method.startswith("tts."):
        return f"{method} sentence={params.get('sentence_id', '')}"
    if method == "wallpaper.canvas":
        return (
            f"{method} mode={params.get('mode', '')} "
            f"work={params.get('workItemId') or params.get('work_item_id') or ''}"
        )
    return method


async def _snapshot(probe: WsProbe) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for method in (
        "runtime.status",
        "system.get_config",
        "provider.list",
        "session.list",
        "work.list",
        "attention.list",
    ):
        try:
            snapshot[method] = await probe.request(method, {}, timeout=20.0)
        except Exception as exc:
            snapshot[method] = {"error": f"{type(exc).__name__}: {exc}"}
    return snapshot


async def _watch_new_events(
    probe: WsProbe,
    *,
    after: int,
    seconds: float,
) -> None:
    cursor = after
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        while cursor < len(probe.state.events):
            event = probe.state.events[cursor]
            cursor += 1
            if event.method in EVIDENCE_METHODS:
                print(f"[live] +{event.elapsed_s:7.2f}s {_event_line(event)}", flush=True)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        async with probe.state.changed:
            try:
                await asyncio.wait_for(
                    probe.state.changed.wait(),
                    timeout=min(1.0, remaining),
                )
            except asyncio.TimeoutError:
                pass


async def _wait_chat_complete(
    probe: WsProbe,
    *,
    turn_id: str,
    after: int,
    timeout: float,
) -> EventRecord:
    return await probe.wait_event(
        lambda event: event.method == "chat.complete"
        and str(event.params.get("turn_id") or "") == turn_id,
        after=after,
        timeout=timeout,
        description=f"chat.complete for {turn_id}",
    )


async def _restore_session(probe: WsProbe, session_id: str) -> dict[str, Any]:
    if not session_id:
        return {"ok": False, "reason": "no_previous_session"}
    try:
        runtime = await probe.request("runtime.status", {}, timeout=20.0)
        chat = runtime.get("chat") if isinstance(runtime, dict) else {}
        if isinstance(chat, dict) and chat.get("busy"):
            return {
                "ok": False,
                "session_id": session_id,
                "reason": "chat_busy_not_restored",
            }
        result = await probe.request(
            "session.load", {"session_id": session_id}, timeout=20.0
        )
        return {"ok": bool(result.get("ok")), "session_id": session_id}
    except Exception as exc:
        return {
            "ok": False,
            "session_id": session_id,
            "error": f"{type(exc).__name__}: {exc}",
        }


async def _wait_output_idle(probe: WsProbe, *, timeout: float) -> dict[str, Any]:
    """Wait until accepted narration leaves a stable idle output lane.

    A momentary idle snapshot is not a turn boundary.  Host-owned follow-up
    narration (for example a Project/Draft focus confirmation) can be queued
    after the role reply has completed.  Require a short notification-free
    idle window so the live probe observes that follow-up instead of cutting
    the evidence at its first sentence start.
    """

    deadline = time.monotonic() + max(0.0, timeout)
    quiet_required_s = min(2.5, max(0.0, timeout))
    idle_since: float | None = None
    observed_event_count = sum(
        event.method in PRESENTATION_ACTIVITY_METHODS
        for event in probe.state.events
    )
    last_status: dict[str, Any] = {}
    while True:
        last_status = await probe.request("runtime.status", {}, timeout=20.0)
        chat = last_status.get("chat") if isinstance(last_status, dict) else {}
        tts = last_status.get("tts") if isinstance(last_status, dict) else {}
        playback = (
            last_status.get("playback") if isinstance(last_status, dict) else {}
        )
        idle = bool(
            not (chat.get("busy") if isinstance(chat, dict) else False)
            and int(tts.get("pending_sentences") or 0) == 0
            and not (
                playback.get("is_playing")
                if isinstance(playback, dict)
                else False
            )
            and int(
                playback.get("pending_audio")
                if isinstance(playback, dict)
                else 0
            ) == 0
        )
        current_event_count = sum(
            event.method in PRESENTATION_ACTIVITY_METHODS
            for event in probe.state.events
        )
        if current_event_count != observed_event_count:
            observed_event_count = current_event_count
            idle_since = None
        now = time.monotonic()
        if idle and idle_since is None:
            idle_since = now
        elif not idle:
            idle_since = None
        if idle_since is not None and now - idle_since >= quiet_required_s:
            return last_status
        if now >= deadline:
            raise TimeoutError("timed out waiting for Chat/TTS/Playback to become idle")
        await asyncio.sleep(0.5)


async def run_observe(args: argparse.Namespace) -> dict[str, Any]:
    async with WsProbe(args.url) as probe:
        initial = await _snapshot(probe)
        print(
            f"[live] attached read-only to {args.url} for {args.seconds:.1f}s",
            flush=True,
        )
        await _watch_new_events(probe, after=0, seconds=args.seconds)
        final = await _snapshot(probe)
        events = [
            event for event in probe.state.events if event.method in EVIDENCE_METHODS
        ]
        return {
            "schema": "amadeus.live_runtime_acceptance.v1",
            "mode": "observe",
            "status": "observed",
            "url": args.url,
            "started_at_utc": args.started_at,
            "duration_s": args.seconds,
            "initial": _safe_excerpt(initial, 4000),
            "final": _safe_excerpt(final, 4000),
            "events": [_compact_event(event) for event in events],
            "limitations": [
                "TTS events do not prove audible playback.",
                "Canvas events do not prove unobstructed Electron rendering.",
                "Passive observation does not inject or assert a user journey.",
            ],
        }


async def run_turn(args: argparse.Namespace) -> dict[str, Any]:
    async with WsProbe(args.url) as probe:
        initial = await _snapshot(probe)
        runtime = initial.get("runtime.status") or {}
        block_reason = active_canary_block_reason(
            runtime if isinstance(runtime, dict) else {},
            allow_active_work=bool(args.allow_active_work),
            allow_chat_busy=bool(args.barge_in),
        )
        if block_reason:
            raise RuntimeError(
                "active canary refused to overlap live product activity: "
                f"{block_reason}"
            )
        sessions = initial.get("session.list") or {}
        previous_session_id = str(
            sessions.get("current_session_id")
            if isinstance(sessions, dict)
            else ""
        ).strip()
        known_sessions = {
            str(item.get("id") or "")
            for item in (sessions.get("sessions") if isinstance(sessions, dict) else [])
            if isinstance(item, dict)
        }
        session_id = str(args.session_id or "").strip() or (
            f"live-canary-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        )
        if session_id in known_sessions:
            session_result = await probe.request(
                "session.load", {"session_id": session_id}, timeout=20.0
            )
            session_action = "loaded"
        else:
            session_result = await probe.request(
                "session.create",
                {"session_id": session_id, "title": "Live acceptance canary"},
                timeout=20.0,
            )
            session_action = "created"

        before_work = await probe.request("work.list", {}, timeout=20.0)
        event_start = len(probe.state.events)
        turn_id = f"live-canary-turn-{uuid.uuid4().hex}"
        config = initial.get("system.get_config") or {}
        chat_provider = str(args.chat_provider or "").strip() or str(
            config.get("llm_provider") if isinstance(config, dict) else ""
        ).strip() or "deepseek"
        complete: EventRecord | None = None
        terminal: EventRecord | None = None
        terminal_decision: EventRecord | None = None
        output_idle: dict[str, Any] = {}
        restore: dict[str, Any] = {}
        runtime_failures: list[str] = []
        watcher: asyncio.Task | None = None
        canary_final: dict[str, Any] = {}
        after_work = before_work
        barge_in: dict[str, Any] = {}
        try:
            print(
                f"[live] active canary session={session_id} expect={args.expect}",
                flush=True,
            )
            if args.barge_in:
                abort_result = await probe.request("chat.abort", {}, timeout=20.0)
                abort_payload = (
                    abort_result if isinstance(abort_result, dict) else {}
                )
                interrupt_result = await probe.request(
                    "tts.interrupt",
                    {
                        "annotate_history": True,
                        "source": "codex_live_runtime_acceptance",
                        "turn_id": str(abort_payload.get("turn_id") or ""),
                        "accumulated_text": str(
                            abort_payload.get("accumulated_text") or ""
                        ),
                    },
                    timeout=20.0,
                )
                barge_in = {
                    "chat_abort": _safe_excerpt(abort_result, 1000),
                    "tts_interrupt": _safe_excerpt(interrupt_result, 1000),
                }
            await probe.request(
                "chat.send",
                {
                    "text": args.say,
                    "provider": chat_provider,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "source": "codex_live_runtime_acceptance",
                },
                timeout=20.0,
            )
            watch_seconds = args.chat_timeout + args.dispatch_window
            if args.wait_terminal:
                watch_seconds += args.provider_timeout
            watcher = asyncio.create_task(
                _watch_new_events(
                    probe,
                    after=event_start,
                    seconds=watch_seconds,
                )
            )
            try:
                try:
                    complete = await _wait_chat_complete(
                        probe,
                        turn_id=turn_id,
                        after=event_start,
                        timeout=args.chat_timeout,
                    )
                except Exception as exc:
                    runtime_failures.append(
                        f"chat completion: {type(exc).__name__}: {exc}"
                    )
                await asyncio.sleep(max(0.0, args.dispatch_window))

                turn_events = list(probe.state.events[event_start:])
                created = _run_created_events(turn_events)
                if created and args.wait_terminal:
                    run_id = _event_run_id(created[-1])
                    if run_id:
                        try:
                            terminal = await probe.wait_event(
                                lambda event: _is_provider_result_for_run(event, run_id),
                                after=event_start,
                                timeout=args.provider_timeout,
                                description=f"terminal Provider result for {run_id}",
                            )
                        except Exception as exc:
                            # Some adapters may only publish the canonical
                            # terminal event. Preserve that bounded fallback,
                            # but never treat run.finished as the narration
                            # boundary when provider.result is still coming.
                            try:
                                terminal = await probe.wait_event(
                                    lambda event: _is_terminal_for_run(event, run_id),
                                    after=event_start,
                                    timeout=2.0,
                                    description=f"terminal Provider event for {run_id}",
                                )
                            except Exception:
                                runtime_failures.append(
                                    "provider terminal: "
                                    f"{type(exc).__name__}: {exc}"
                                )
                        if terminal is not None:
                            terminal_metadata = _event_metadata(terminal)
                            completion = (
                                terminal_metadata.get("provider_completion")
                                if isinstance(
                                    terminal_metadata.get("provider_completion"), dict
                                )
                                else {}
                            )
                            if (
                                completion.get("classification")
                                == "progress_only_completion"
                                and created
                            ):
                                predecessor = created[-1]
                                try:
                                    successor = await probe.wait_event(
                                        lambda event: event.method == "provider.event"
                                        and _event_type(event) == "run.created"
                                        and progress_recovery_successor(
                                            [event], predecessor
                                        )
                                        is event,
                                        after=event_start,
                                        timeout=min(5.0, args.provider_timeout),
                                        description="bounded progress-only Provider recovery",
                                    )
                                    if is_bounded_progress_recovery_chain(
                                        [predecessor, successor]
                                    ):
                                        created.append(successor)
                                        run_id = _event_run_id(successor)
                                        terminal = await probe.wait_event(
                                            lambda event: _is_provider_result_for_run(
                                                event, run_id
                                            ),
                                            after=event_start,
                                            timeout=args.provider_timeout,
                                            description=(
                                                "terminal Provider result for recovery "
                                                f"{run_id}"
                                            ),
                                        )
                                except Exception as exc:
                                    runtime_failures.append(
                                        "provider recovery: "
                                        f"{type(exc).__name__}: {exc}"
                                    )
                            try:
                                terminal_decision = await probe.wait_event(
                                    lambda event: _is_terminal_observer_decision_for_run(
                                        event,
                                        run_id,
                                    ),
                                    after=event_start,
                                    timeout=args.settle_timeout,
                                    description=(
                                        "terminal WorkObserver decision for "
                                        f"{run_id}"
                                    ),
                                )
                                output_idle = await _wait_output_idle(
                                    probe,
                                    timeout=args.settle_timeout,
                                )
                            except Exception as exc:
                                runtime_failures.append(
                                    "terminal presentation: "
                                    f"{type(exc).__name__}: {exc}"
                                )
                should_wait_presentation = args.wait_terminal or not created
                if complete is not None and not output_idle and should_wait_presentation:
                    try:
                        output_idle = await _wait_output_idle(
                            probe,
                            timeout=args.settle_timeout,
                        )
                    except Exception as exc:
                        runtime_failures.append(
                            "turn presentation: "
                            f"{type(exc).__name__}: {exc}"
                        )
                clean_expect = str(args.expect or "any").strip().lower()
                if clean_expect in {"auip-launch", "auip-prepare"}:
                    try:
                        await probe.wait_event(
                            lambda event: event.method == "auip.launch.requested",
                            after=event_start,
                            timeout=args.settle_timeout,
                            description="AUIP launch request",
                        )
                        await probe.wait_event(
                            lambda event: event.method == "auip.updated"
                            and str(event.params.get("status") or "")
                            .strip()
                            .lower()
                            == "active",
                            after=event_start,
                            timeout=args.settle_timeout,
                            description="active AUIP AppSession",
                        )
                    except Exception as exc:
                        runtime_failures.append(
                            "AUIP activation: "
                            f"{type(exc).__name__}: {exc}"
                        )
                # Playback completion is emitted asynchronously after the
                # runtime becomes idle.  Keep the evidence window open long
                # enough to pair every observed sentence boundary.
                if any(
                    event.method == "tts.sentence_start"
                    for event in probe.state.events[event_start:]
                ) and not any(
                    event.method == "tts.turn_complete"
                    for event in probe.state.events[event_start:]
                ):
                    try:
                        await probe.wait_event(
                            lambda event: event.method == "tts.turn_complete",
                            after=event_start,
                            timeout=2.0,
                            description="TTS turn completion",
                        )
                    except Exception as exc:
                        recent = probe.state.events[event_start:]
                        starts = {
                            str(event.params.get("sentence_id") or "")
                            for event in recent
                            if event.method == "tts.sentence_start"
                        }
                        ends = {
                            str(event.params.get("sentence_id") or "")
                            for event in recent
                            if event.method == "tts.sentence_end"
                        }
                        if starts - ends or not _runtime_output_is_idle(output_idle):
                            runtime_failures.append(
                                "TTS completion event: "
                                f"{type(exc).__name__}: {exc}"
                            )
                canary_final = await _snapshot(probe)
                after_work = canary_final.get("work.list") or {}
            finally:
                if watcher is not None:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
        finally:
            if not canary_final:
                try:
                    canary_final = await _snapshot(probe)
                    after_work = canary_final.get("work.list") or {}
                except Exception as exc:
                    runtime_failures.append(
                        f"final canary snapshot: {type(exc).__name__}: {exc}"
                    )
            restore = await _restore_session(probe, previous_session_id)

        final_after_restore = await _snapshot(probe)
        turn_events = [
            event
            for event in probe.state.events[event_start:]
            if event.method in EVIDENCE_METHODS
        ]
        evaluation = evaluate_turn(
            expect=args.expect,
            events=turn_events,
            before_work=before_work,
            after_work=after_work,
            chat_completed=complete is not None,
            require_terminal_success=bool(args.wait_terminal),
            expected_work_item_id=args.expect_work_item,
            output_idle=output_idle,
        )
        if runtime_failures:
            evaluation["failures"].extend(runtime_failures)
            evaluation["status"] = "failed"
        details: dict[str, Any] = {}
        for work_item_id in evaluation["related_work_item_ids"]:
            try:
                details[work_item_id] = await probe.request(
                    "work.get",
                    {"work_item_id": work_item_id},
                    timeout=20.0,
                )
            except Exception as exc:
                details[work_item_id] = {
                    "error": f"{type(exc).__name__}: {exc}"
                }
        return {
            "schema": "amadeus.live_runtime_acceptance.v1",
            "mode": "turn",
            "status": evaluation["status"],
            "url": args.url,
            "started_at_utc": args.started_at,
            "session": {
                "id": session_id,
                "action": session_action,
                "result": _safe_excerpt(session_result, 1000),
                "previous_session_id": previous_session_id,
                "restored": restore,
            },
            "turn": {
                "id": turn_id,
                "text": args.say,
                "chat_provider": chat_provider,
                "chat_complete": _compact_event(complete) if complete else None,
                "provider_terminal": _compact_event(terminal) if terminal else None,
                "terminal_decision": (
                    _compact_event(terminal_decision)
                    if terminal_decision
                    else None
                ),
                "output_idle": _safe_excerpt(output_idle, 1200),
                "barge_in": barge_in,
            },
            "evaluation": evaluation,
            "initial": _safe_excerpt(initial, 4000),
            "canary_final": _safe_excerpt(canary_final, 4000),
            "final_after_restore": _safe_excerpt(final_after_restore, 4000),
            "work_details": _safe_excerpt(details, 4000),
            "events": [_compact_event(event) for event in turn_events],
            "limitations": [
                "TTS events do not prove audible playback or natural delivery.",
                "Canvas events do not prove Electron layout, z-order, or clickability.",
                "The canary Session is retained for audit; the prior Session is restored.",
            ],
        }


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Amadeus live runtime acceptance",
        "",
        f"- Mode: `{report.get('mode', '')}`",
        f"- Status: `{report.get('status', '')}`",
        f"- Started: `{report.get('started_at_utc', '')}`",
        f"- Runtime: `{report.get('url', '')}`",
    ]
    evaluation = report.get("evaluation")
    if isinstance(evaluation, dict):
        lines.extend(
            [
                f"- Expectation: `{evaluation.get('expect', '')}`",
                f"- Providers: `{', '.join(evaluation.get('providers') or [])}`",
                f"- New WorkItems: `{len(evaluation.get('new_work_item_ids') or [])}`",
                "",
                "## Assertions",
                "",
            ]
        )
        failures = evaluation.get("failures") or []
        lines.extend(
            [f"- {'FAIL' if failures else 'PASS'}: {failure}" for failure in failures]
            if failures
            else ["- PASS: observed behavior matched the declared expectation."]
        )
    lines.extend(["", "## Event timeline", ""])
    for event in report.get("events") or []:
        lines.append(
            f"- `+{float(event.get('elapsed_s') or 0):.3f}s` "
            f"`{event.get('method', '')}`"
        )
    lines.extend(["", "## Limits", ""])
    lines.extend(f"- {item}" for item in report.get("limitations") or [])
    lines.append("")
    return "\n".join(lines)


def write_report(report: dict[str, Any], directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"live_{report.get('mode', 'observe')}_{_utc_stamp()}_{uuid.uuid4().hex[:6]}"
    json_path = directory / f"{stem}.json"
    md_path = directory / f"{stem}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_path.write_text(_render_markdown(report), encoding="utf-8")
    return json_path, md_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("observe", "turn"), default="observe")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--say", default="")
    parser.add_argument("--expect", default="any")
    parser.add_argument("--session-id", default="")
    parser.add_argument(
        "--expect-work-item",
        default="",
        help="optional exact WorkItem id expected to own the observed turn",
    )
    parser.add_argument("--chat-provider", default="")
    parser.add_argument("--chat-timeout", type=float, default=90.0)
    parser.add_argument("--dispatch-window", type=float, default=3.0)
    parser.add_argument("--provider-timeout", type=float, default=600.0)
    parser.add_argument(
        "--settle-timeout",
        type=float,
        default=90.0,
        help=(
            "maximum wait for terminal WorkObserver delivery and output idle "
            "before restoring the prior Session"
        ),
    )
    parser.add_argument("--no-wait-terminal", dest="wait_terminal", action="store_false")
    parser.add_argument(
        "--allow-active-work",
        action="store_true",
        help=(
            "allow a turn canary while a Provider run is active; intended only "
            "for an explicitly fenced mid-run continuation test"
        ),
    )
    parser.add_argument(
        "--barge-in",
        action="store_true",
        help=(
            "simulate the product's spoken interruption boundary by aborting "
            "the active Chat turn and interrupting TTS before sending --say"
        ),
    )
    parser.set_defaults(wait_terminal=True)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.seconds < 0:
        raise ValueError("--seconds must be non-negative")
    if args.settle_timeout < 0:
        raise ValueError("--settle-timeout must be non-negative")
    if args.mode == "turn" and not str(args.say or "").strip():
        raise ValueError("--mode turn requires --say")
    if args.mode == "observe" and str(args.say or "").strip():
        raise ValueError("--say requires --mode turn")
    if args.mode != "turn" and args.barge_in:
        raise ValueError("--barge-in requires --mode turn")
    expectation = str(args.expect or "any").strip().lower()
    if (
        expectation not in {
            "any",
            "no-work",
            "work",
            "amend",
            "auip-launch",
            "auip-prepare",
            "auip-leave",
        }
        and not expectation.startswith("provider:")
        and not expectation.startswith("auip-mode:")
    ):
        raise ValueError(
            "--expect must be any, no-work, work, amend, provider:<id>, "
            "auip-launch, auip-prepare, auip-mode:<mode>, or auip-leave"
        )


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    args.started_at = datetime.now(timezone.utc).isoformat()
    try:
        _validate_args(args)
        report = asyncio.run(run_turn(args) if args.mode == "turn" else run_observe(args))
        json_path, md_path = write_report(report, args.report_dir)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"[live] JSON report: {json_path}")
    print(f"[live] Markdown report: {md_path}")
    return 0 if report.get("status") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
