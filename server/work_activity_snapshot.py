"""Host-owned projection of material provider activity.

Provider events are an execution protocol, not a query model.  This module
reduces the small subset that matters to a user asking "what is happening
now?" into a durable, provider-neutral Attempt snapshot.  It is deliberately
pure: narration, UI progress percentages, filesystem inspection and completion
judgement live elsewhere.
"""

from __future__ import annotations

from typing import Any

from server.work_semantic_progress import (
    SemanticProgressFact,
    consume_tool_call,
    remember_tool_call,
    semantic_progress_fact,
)


ACTIVITY_SNAPSHOT_VERSION = 1
ACTIVITY_METADATA_KEY = "activity_snapshot"
_MAX_SEMANTIC_SUMMARY = 360
_FINAL_PHASES = frozenset({"review", "terminal"})

MATERIAL_ACTIVITY_EVENTS = frozenset(
    {
        "run.created",
        "run.started",
        "run.status",
        "assistant.update",
        "semantic.progress",
        "tool.call",
        "tool.result",
        "artifact.created",
        "permission.requested",
        "permission.required",
        "input.requested",
        "question",
        "user.input.required",
        "run.failed",
        "run.cancelled",
    }
)


def is_material_activity_event(event_type: str) -> bool:
    return str(event_type or "").strip().lower() in MATERIAL_ACTIVITY_EVENTS


def project_activity_event(
    current: dict[str, Any] | None,
    params: dict[str, Any],
    *,
    execution_status: str,
    now: float,
) -> dict[str, Any]:
    """Return the next durable activity snapshot for one material event.

    Positive provider sequence numbers are monotonic within a run.  Hand-made
    and legacy events may have no sequence; those remain projectable so the
    host does not lose useful facts merely because an adapter predates the
    runtime envelope.
    """

    event_type = str(params.get("type") or "").strip().lower()
    before = _normalise_snapshot(current)
    if event_type not in MATERIAL_ACTIVITY_EVENTS:
        return before

    sequence = _non_negative_int(params.get("sequence"))
    previous_sequence = _non_negative_int(before.get("eventSequence"))
    if sequence > 0 and previous_sequence > 0 and sequence <= previous_sequence:
        return before

    payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
    observed_at = _positive_float(params.get("observed_at")) or float(now)
    after = dict(before)
    after["version"] = ACTIVITY_SNAPSHOT_VERSION
    after["revision"] = _non_negative_int(before.get("revision")) + 1
    if sequence > 0:
        after["eventSequence"] = sequence
    after["lastEventAt"] = observed_at
    after["lastEventType"] = event_type

    tool_context: dict[str, Any] = {}
    if event_type == "tool.call":
        after["activeToolContexts"] = remember_tool_call(
            before.get("activeToolContexts"), payload
        )
        tool_context = payload
    elif event_type == "tool.result":
        contexts, tool_context = consume_tool_call(
            before.get("activeToolContexts"), payload
        )
        after["activeToolContexts"] = contexts

    if event_type == "run.created":
        after["phase"] = "queued"
        after["uncertainty"] = "provider_has_not_reported_semantic_progress"
        event_metadata = (
            params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
        )
        replacement = (
            event_metadata.get("steer_replacement")
            if isinstance(event_metadata.get("steer_replacement"), dict)
            else {}
        )
        if replacement:
            after["steering"] = {
                "state": "restarted",
                "revision": _non_negative_int(replacement.get("revision")),
                "safeBoundary": "confirmed_cancel_then_restart",
                "observedAt": observed_at,
                "predecessorAttemptId": str(
                    replacement.get("predecessor_attempt_id") or ""
                ),
                "successorAttemptId": str(
                    replacement.get("successor_attempt_id") or ""
                ),
                "reason": "",
            }
    elif event_type == "run.started":
        after["phase"] = "working"
        after["startedAt"] = _positive_float(after.get("startedAt")) or observed_at
        after.setdefault("uncertainty", "provider_has_not_reported_semantic_progress")
    elif event_type == "run.status":
        _project_status(after, payload, observed_at)
    elif event_type == "semantic.progress":
        if str(before.get("phase") or "") not in _FINAL_PHASES:
            after["phase"] = "working"
        if _semantic_summary(payload):
            after["uncertainty"] = (
                "provider_action_denied"
                if _non_negative_int(after.get("permissionDiagnosticCount"))
                else ""
            )
    elif event_type == "assistant.update":
        if str(before.get("phase") or "") not in _FINAL_PHASES:
            after["phase"] = "working"
        if _semantic_summary(payload):
            if _non_negative_int(after.get("permissionDiagnosticCount")):
                after["uncertainty"] = "provider_action_denied"
            else:
                after["uncertainty"] = ""
    elif event_type == "tool.call":
        if str(before.get("phase") or "") not in _FINAL_PHASES:
            after["phase"] = "working"
        after["toolCount"] = _non_negative_int(before.get("toolCount")) + 1
        tool = _compact_text(payload.get("tool") or payload.get("name"), limit=80)
        if tool:
            after["lastTool"] = tool
    elif event_type == "tool.result":
        if str(before.get("phase") or "") not in _FINAL_PHASES:
            after["phase"] = "working"
    elif event_type == "artifact.created":
        if str(before.get("phase") or "") not in _FINAL_PHASES:
            after["phase"] = "working"
        after["artifactCount"] = _non_negative_int(before.get("artifactCount")) + 1
    elif event_type in {
        "permission.requested",
        "permission.required",
        "input.requested",
        "question",
        "user.input.required",
    }:
        diagnostic_only = payload.get("diagnosticOnly") is True or payload.get(
            "diagnostic_only"
        ) is True
        if diagnostic_only:
            if str(before.get("phase") or "") not in _FINAL_PHASES:
                after["phase"] = "working"
            after["permissionDiagnosticCount"] = (
                _non_negative_int(before.get("permissionDiagnosticCount")) + 1
            )
            after["latestPermissionDiagnostic"] = {
                "capability": _compact_text(payload.get("capability"), limit=80),
                "action": _compact_text(
                    payload.get("action") or payload.get("toolName"),
                    limit=80,
                ),
                "reason": _compact_text(payload.get("reason"), limit=160),
                "retryRequired": payload.get("retryRequired") is True,
                "observedAt": observed_at,
            }
            after["uncertainty"] = "provider_action_denied"
        else:
            if str(before.get("phase") or "") not in _FINAL_PHASES:
                after["phase"] = "waiting_for_user"
            after["uncertainty"] = "waiting_for_user"
    elif event_type in {"run.failed", "run.cancelled"}:
        after["phase"] = "terminal"
        after["finishedAt"] = observed_at
        after["uncertainty"] = ""

    fact = semantic_progress_fact(event_type, payload, tool_context=tool_context)
    _apply_semantic_fact(after, fact, observed_at)

    # A late tool/progress event can be retained in logs, but never revive an
    # attempt after the provider has entered review or a terminal outcome.
    if str(before.get("phase") or "") in _FINAL_PHASES and event_type not in {
        "run.status",
        "run.failed",
        "run.cancelled",
    }:
        after["phase"] = str(before.get("phase"))
    if execution_status in {"failed", "cancelled"}:
        after["phase"] = "terminal"
    elif execution_status == "succeeded":
        after["phase"] = "review"
    return after


def project_activity_result(
    current: dict[str, Any] | None,
    *,
    status: str,
    observed_at: float,
) -> dict[str, Any]:
    """Project ProviderRuntime's terminal result, which is not an event."""

    before = _normalise_snapshot(current)
    after = dict(before)
    after["version"] = ACTIVITY_SNAPSHOT_VERSION
    after["revision"] = _non_negative_int(before.get("revision")) + 1
    after["lastEventAt"] = float(observed_at)
    after["lastMeaningfulEventAt"] = float(observed_at)
    after["lastSemanticProgressAt"] = float(observed_at)
    after["lastEventType"] = "provider.result"
    after["finishedAt"] = float(observed_at)
    after["phase"] = "review" if str(status or "").lower() == "succeeded" else "terminal"
    after["uncertainty"] = ""
    return after


def project_host_steering(
    current: dict[str, Any] | None,
    *,
    state: str,
    revision: int,
    observed_at: float,
    safe_boundary: str = "",
    predecessor_attempt_id: str = "",
    successor_attempt_id: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """Project a host control transition that has no native Provider event."""

    after = _normalise_snapshot(current)
    after["revision"] = _non_negative_int(after.get("revision")) + 1
    after["lastEventAt"] = float(observed_at)
    after["lastMeaningfulEventAt"] = float(observed_at)
    after["lastSemanticProgressAt"] = float(observed_at)
    after["lastEventType"] = f"host.steer.{str(state or 'unknown').strip().lower()}"
    after["steering"] = {
        "state": str(state or "unknown").strip().lower(),
        "revision": max(1, int(revision or 0)),
        "safeBoundary": str(safe_boundary or ""),
        "observedAt": float(observed_at),
        "predecessorAttemptId": str(predecessor_attempt_id or ""),
        "successorAttemptId": str(successor_attempt_id or ""),
        "reason": _compact_text(reason, limit=160),
    }
    if state == "cancel_pending":
        after["phase"] = "cancelling"
        after["uncertainty"] = "cancellation_not_yet_confirmed"
    elif state in {"rejected", "failed"}:
        if str(after.get("phase") or "") == "cancelling":
            after["phase"] = "working"
        previous_liveness = (
            after.get("liveness") if isinstance(after.get("liveness"), dict) else {}
        )
        after["liveness"] = {
            **previous_liveness,
            "state": "active",
            "stage": "steer_rejected",
            "reason": _compact_text(reason, limit=160),
            "observedAt": float(observed_at),
        }
        after["uncertainty"] = "steer_not_applied"
    return after


def activity_report_fields(
    snapshot: dict[str, Any] | None,
    *,
    execution_status: str,
    created_at: float,
    started_at: float | None,
    finished_at: float | None,
    now: float,
) -> dict[str, Any]:
    """Materialise dynamic durations without turning the ledger into a clock."""

    raw_activity = dict(snapshot) if isinstance(snapshot, dict) else {}
    activity = _normalise_snapshot(snapshot)
    phase = str(raw_activity.get("phase") or _phase_for_execution(execution_status))
    start = (
        _positive_float(started_at)
        or _positive_float(activity.get("startedAt"))
        or _positive_float(created_at)
        or float(now)
    )
    end = _positive_float(finished_at) or float(now)
    elapsed = max(0.0, end - start)
    last_semantic = (
        _positive_float(activity.get("lastSemanticProgressAt"))
        or _positive_float(activity.get("lastMeaningfulEventAt"))
        or start
    )
    last_directional = _positive_float(activity.get("lastDirectionalUpdateAt"))
    last_useful_update = max(last_semantic, last_directional)
    silence = max(0.0, float(now) - last_useful_update)
    liveness = activity.get("liveness") if isinstance(activity.get("liveness"), dict) else {}
    reported_silence = _non_negative_float(liveness.get("silenceSeconds"))
    if str(liveness.get("state") or "") in {"stalled", "cancel_pending"}:
        silence = max(silence, reported_silence)
    uncertainty = str(raw_activity.get("uncertainty") or "")
    if (
        execution_status in {"queued", "running"}
        and not str(activity.get("latestSemanticSummary") or "")
        and not str(activity.get("latestCandidateSummary") or "")
    ):
        uncertainty = uncertainty or "provider_has_not_reported_semantic_progress"
    return {
        "activity_phase": phase,
        "activity_elapsed_seconds": round(elapsed, 1),
        "activity_silent_seconds": round(silence, 1),
        "activity_last_event_at": _positive_float(activity.get("lastEventAt")),
        "activity_last_provider_event_at": _positive_float(activity.get("lastEventAt")),
        "activity_last_semantic_progress_at": _positive_float(last_semantic),
        "activity_last_directional_update_at": last_directional,
        "activity_direction_summary": str(activity.get("latestCandidateSummary") or ""),
        "activity_direction_source": str(activity.get("candidateSource") or ""),
        "activity_last_event_type": str(activity.get("lastEventType") or ""),
        "activity_semantic_summary": str(activity.get("latestSemanticSummary") or ""),
        "activity_semantic_source": str(activity.get("semanticSource") or ""),
        "activity_semantic_verified": activity.get("semanticVerified") is True,
        "activity_semantic_milestone": str(activity.get("semanticMilestone") or ""),
        "activity_milestones": dict(
            activity.get("milestones")
            if isinstance(activity.get("milestones"), dict)
            else {}
        ),
        "activity_last_tool": str(activity.get("lastTool") or ""),
        "activity_tool_count": _non_negative_int(activity.get("toolCount")),
        "activity_artifact_count": _non_negative_int(activity.get("artifactCount")),
        "activity_liveness": dict(liveness),
        "activity_steering": (
            dict(activity.get("steering"))
            if isinstance(activity.get("steering"), dict)
            else {}
        ),
        "activity_uncertainty": uncertainty,
    }


def _project_status(after: dict[str, Any], payload: dict[str, Any], observed_at: float) -> None:
    status = str(payload.get("status") or "").strip().lower()
    liveness_state = str(payload.get("liveness") or "").strip().lower()
    stage = str(payload.get("stage") or "").strip().lower()
    if liveness_state:
        liveness = {
            "state": liveness_state,
            "stage": str(payload.get("stage") or ""),
            "silenceSeconds": _non_negative_float(payload.get("silence_s")),
            "elapsedSeconds": _non_negative_float(payload.get("elapsed_s")),
            "probeStatus": str(payload.get("probe_status") or ""),
            "probeReachable": payload.get("probe_reachable"),
            "lastProviderEventAt": _positive_float(payload.get("last_provider_event_at")),
            "recovered": payload.get("recovered") is True,
            "stallDurationSeconds": _non_negative_float(payload.get("stall_duration_s")),
            "reason": _compact_text(payload.get("reason"), limit=160),
            "observedAt": _positive_float(payload.get("observed_at")) or observed_at,
        }
        after["liveness"] = liveness
    if stage in {"steer_queued", "steer_applied"}:
        after["steering"] = {
            "state": "queued" if stage == "steer_queued" else "applied",
            "revision": _non_negative_int(payload.get("revision")),
            "replacesRevision": _non_negative_int(payload.get("replaces_revision")),
            "safeBoundary": str(payload.get("safe_boundary") or ""),
            "observedAt": observed_at,
        }
    if liveness_state == "stalled" or status == "stalled":
        after["phase"] = "stalled"
        after["uncertainty"] = "provider_silent"
    elif liveness_state == "cancel_pending" or status in {"cancelling", "cancel_pending"}:
        after["phase"] = "cancelling"
        after["uncertainty"] = "cancellation_not_yet_confirmed"
    elif status in {"done", "succeeded", "success", "completed"}:
        after["phase"] = "review"
        after["finishedAt"] = observed_at
        after["uncertainty"] = ""
    elif status in {"error", "failed", "cancelled", "canceled"}:
        after["phase"] = "terminal"
        after["finishedAt"] = observed_at
        after["uncertainty"] = ""
    elif status == "queued":
        after["phase"] = "queued"
    elif status in {"running", "active", "started"}:
        if str(after.get("phase") or "") not in _FINAL_PHASES and liveness_state != "stalled":
            after["phase"] = "working"
            after["startedAt"] = _positive_float(after.get("startedAt")) or observed_at
        if liveness_state == "active" and payload.get("recovered") is True:
            if str(after.get("latestSemanticSummary") or ""):
                after["uncertainty"] = ""
            else:
                after["uncertainty"] = "provider_has_not_reported_semantic_progress"


def _normalise_snapshot(value: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(value) if isinstance(value, dict) else {}
    source["version"] = ACTIVITY_SNAPSHOT_VERSION
    source["revision"] = _non_negative_int(source.get("revision"))
    source["eventSequence"] = _non_negative_int(source.get("eventSequence"))
    source["toolCount"] = _non_negative_int(source.get("toolCount"))
    source["artifactCount"] = _non_negative_int(source.get("artifactCount"))
    source["permissionDiagnosticCount"] = _non_negative_int(
        source.get("permissionDiagnosticCount")
    )
    source["activeToolContexts"] = {
        str(key): dict(item)
        for key, item in (
            source.get("activeToolContexts")
            if isinstance(source.get("activeToolContexts"), dict)
            else {}
        ).items()
        if str(key) and isinstance(item, dict)
    }
    source["recentSemanticFactKeys"] = [
        str(item)
        for item in (
            source.get("recentSemanticFactKeys")
            if isinstance(source.get("recentSemanticFactKeys"), list)
            else []
        )[-24:]
        if str(item)
    ]
    source["recentSemanticCandidateKeys"] = [
        str(item)
        for item in (
            source.get("recentSemanticCandidateKeys")
            if isinstance(source.get("recentSemanticCandidateKeys"), list)
            else []
        )[-24:]
        if str(item)
    ]
    source["milestones"] = {
        str(key): dict(item)
        for key, item in (
            source.get("milestones")
            if isinstance(source.get("milestones"), dict)
            else {}
        ).items()
        if str(key) in {"design", "diagnostic", "capability", "validation"}
        and isinstance(item, dict)
    }
    source.setdefault("phase", "queued")
    source.setdefault("uncertainty", "provider_has_not_reported_semantic_progress")
    return source


def _apply_semantic_fact(
    snapshot: dict[str, Any],
    fact: SemanticProgressFact | None,
    observed_at: float,
) -> bool:
    """Apply a signal without letting candidates or mechanics reset fact time."""

    if fact is not None and fact.evidence == "candidate":
        seen_candidates = snapshot.get("recentSemanticCandidateKeys")
        if not isinstance(seen_candidates, list):
            seen_candidates = []
        if fact.key in seen_candidates:
            return False
        seen_candidates.append(fact.key)
        snapshot["recentSemanticCandidateKeys"] = seen_candidates[-24:]
        snapshot["latestCandidateAt"] = float(observed_at)
        snapshot["lastDirectionalUpdateAt"] = float(observed_at)
        snapshot["latestCandidateSummary"] = fact.summary
        snapshot["candidateSource"] = fact.source
        return True

    seen = snapshot.get("recentSemanticFactKeys")
    if not isinstance(seen, list):
        seen = []
    if fact is None or fact.key in seen:
        return False
    seen.append(fact.key)
    snapshot["recentSemanticFactKeys"] = seen[-24:]
    snapshot["lastSemanticFactKey"] = fact.key
    snapshot["lastSemanticProgressAt"] = float(observed_at)
    # Kept for readers of the v1 snapshot that predate the two-clock model.
    snapshot["lastMeaningfulEventAt"] = float(observed_at)
    snapshot["latestSemanticAt"] = float(observed_at)
    snapshot["latestSemanticSummary"] = fact.summary
    snapshot["semanticExplicit"] = fact.explicit
    snapshot["semanticVerified"] = fact.verified
    snapshot["semanticSource"] = fact.source
    snapshot["semanticMilestone"] = fact.milestone
    if fact.milestone:
        milestones = (
            dict(snapshot.get("milestones"))
            if isinstance(snapshot.get("milestones"), dict)
            else {}
        )
        milestones[fact.milestone] = {
            "summary": fact.summary,
            "source": fact.source,
            "verified": fact.verified,
            "observedAt": float(observed_at),
        }
        snapshot["milestones"] = milestones
    return True


def _phase_for_execution(execution_status: str) -> str:
    status = str(execution_status or "").strip().lower()
    if status == "queued":
        return "queued"
    if status == "running":
        return "working"
    if status == "succeeded":
        return "review"
    if status in {"failed", "cancelled", "orphaned"}:
        return "terminal"
    return "queued"


def _semantic_summary(payload: dict[str, Any]) -> str:
    for key in ("summary", "message", "text", "detail"):
        text = _compact_text(payload.get(key), limit=_MAX_SEMANTIC_SUMMARY)
        if text:
            return text
    return ""


def _compact_text(value: Any, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def _positive_float(value: Any) -> float:
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0.0 else 0.0


def _non_negative_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
