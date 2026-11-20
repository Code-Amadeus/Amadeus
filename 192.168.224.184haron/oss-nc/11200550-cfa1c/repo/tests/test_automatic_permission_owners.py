"""自动决定必须由确切、活跃的持久权限拥有者提交。"""

import sqlite3

import pytest

from test_work_scoped_permissions import _pending


async def test_work_owner_commits_policy_decision_before_returning(tmp_path):
    store, owner, _, permission = await _pending(tmp_path)
    try:
        assert owner.authorize_automatic_permission("run-scope", "approval-original") is True
        stored = store.get_permission_request(permission.request_id)
        assert stored.status == "allowed"
        assert stored.metadata["resolution"] == "policy_allowed"
        assert stored.metadata["resolved_automatically"] is True
        assert stored.metadata["reason"] == "work_permission_auto_approve"
        assert "grant_status" not in stored.metadata
        assert owner.authorize_automatic_permission("run-scope", "approval-original") is False
    finally:
        owner.close()
        store.close()


@pytest.mark.parametrize("invalid", ["missing-run", "missing-request", "terminal", "user-decision"])
async def test_work_owner_rejects_missing_inactive_or_resolved_permission(tmp_path, invalid):
    store, owner, _, permission = await _pending(tmp_path)
    try:
        run_id, native_id = "run-scope", "approval-original"
        if invalid == "missing-run":
            run_id = "other-run"
        elif invalid == "missing-request":
            native_id = "other-request"
        elif invalid == "terminal":
            store.update_attempt(permission.attempt_id, execution_status="cancelled")
        else:
            store.resolve_permission_request(permission.request_id, "denied",
                metadata={"resolution": "user_denied"})
        before = store.get_permission_request(permission.request_id).to_dict()
        assert owner.authorize_automatic_permission(run_id, native_id) is False
        assert store.get_permission_request(permission.request_id).to_dict() == before
    finally:
        owner.close()
        store.close()


@pytest.mark.parametrize("invalid", ["provider", "run", "kind", "diagnostic", "retry", "option"])
async def test_work_owner_uses_durable_permission_authority(tmp_path, invalid):
    store, owner, _, original = await _pending(tmp_path)
    metadata = {**original.metadata, "provider_request_id": "bounded-request"}
    options = ["allow_once", "deny"]
    if invalid == "provider":
        metadata["provider"] = "another-provider"
    elif invalid == "run":
        metadata["provider_run_id"] = "another-run"
    elif invalid == "kind":
        metadata["kind"] = "desktop_export"
    elif invalid == "diagnostic":
        metadata["diagnostic_only"] = True
    elif invalid == "retry":
        metadata["retry_required"] = True
    else:
        options = ["deny"]
    try:
        permission = store.create_permission_request(original.work_item_id,
            attempt_id=original.attempt_id, capability="mcp.tool.call", action="call_tool",
            options=options, metadata=metadata, idempotency_key="bounded-request")
        assert owner.authorize_automatic_permission("run-scope", "bounded-request") is False
        assert store.get_permission_request(permission.request_id).status == "pending"
        assert store.get_permission_request(original.request_id).status == "pending"
    finally:
        owner.close()
        store.close()


async def test_work_owner_propagates_decision_commit_failure(tmp_path, monkeypatch):
    store, owner, _, permission = await _pending(tmp_path)
    try:
        def failed_commit(*args, **kwargs):
            raise sqlite3.OperationalError("synthetic policy commit failure")

        monkeypatch.setattr(store, "resolve_permission_request", failed_commit)
        with pytest.raises(sqlite3.OperationalError, match="policy commit failure"):
            owner.authorize_automatic_permission("run-scope", "approval-original")
        assert store.get_permission_request(permission.request_id).status == "pending"
    finally:
        owner.close()
        store.close()
