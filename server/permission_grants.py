"""Projection and audit of Host-approved exact-call permission grants."""

from __future__ import annotations

from agent_host.work_ledger_store import WorkLedgerConflict


def permission_projection(request):
    metadata = request.metadata
    result = {
        "id": request.request_id, "ownerKind": request.owner_kind,
        "providerRequestId": str(metadata.get("provider_request_id") or ""),
        "sessionId": request.session_id or str(metadata.get("session_id") or ""),
        "runId": request.provider_run_id or str(metadata.get("provider_run_id") or ""),
        "capability": request.capability, "action": request.action,
        "scope": list(request.scope_paths), "reason": request.reason,
        "reversibility": request.reversibility, "status": request.status,
        "options": list(request.options),
    }
    if request.owner_kind == "work_attempt":
        result.update(workItemId=request.work_item_id, attemptId=request.attempt_id)
    if isinstance(metadata.get("mcp_call"), dict):
        result["mcpCall"] = dict(metadata["mcp_call"])
    if metadata.get("resolution") == "user_allowed_work":
        result.update(grantStatus=metadata.get("grant_status"), useCount=int(metadata.get("use_count") or 0))
    return result


def active_grants(requests):
    return [permission_projection(request) for request in requests
        if request.status == "allowed" and request.metadata.get("resolution") == "user_allowed_work"
        and request.metadata.get("grant_status") == "active"]


def expire_failed_dispatch_grant(store, request_id):
    request = store.get_permission_request(request_id)
    if request is not None and request.metadata.get("grant_status") == "active":
        try:
            return store.update_permission_grant(request_id, status="expired")
        except WorkLedgerConflict:
            return store.get_permission_request(request_id)
    return request


def apply_grant_event(store, requests, event_type, payload):
    request_id = str(payload.get("request_id") or payload.get("requestId") or "")
    matches = [request for request in requests
        if request.metadata.get("provider_request_id") == request_id
        and request.metadata.get("grant_status") == "active"]
    if len(matches) != 1:
        return None
    status = {"permission.used": "active", "permission.revoked": "revoked",
        "permission.expired": "expired"}.get(event_type)
    if status is None:
        return None
    if event_type == "permission.used" and not str(payload.get("call_request_id") or "").strip():
        return None
    try:
        return store.update_permission_grant(matches[0].request_id, status=status,
            call_request_id=str(payload.get("call_request_id") or ""))
    except WorkLedgerConflict:
        return None
