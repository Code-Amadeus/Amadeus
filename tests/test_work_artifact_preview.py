"""Ordinary deliverables use ledger identity, current bytes and Work scope."""
import asyncio
import hashlib
from pathlib import Path

import pytest

from agent_host.work_ledger_store import WorkLedgerStore
from config import settings
from server.handlers.work_ledger_handler import WorkLedgerHandler
from server.protocol import Method
from server.work_artifact_source import TEXT_LIMIT_BYTES, read_registered_text
from server.work_ledger_coordinator import WorkLedgerCoordinator


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    scratch = tmp_path / "drafts"
    scratch.mkdir()
    monkeypatch.setattr(settings, "WORK_SCRATCH_ROOT", str(scratch))
    monkeypatch.setattr(settings, "WORK_PROJECT_ALLOWLIST", str(tmp_path))
    with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
        project = store.create_or_get_project(scratch)
        workspace = scratch / "audit"
        workspace.mkdir()
        item = store.create_work_item(project.project_id, title="Audit orders", workspace_path=workspace)
        attempt = store.create_attempt(item.work_item_id, provider="test", task="Audit")
        store.update_attempt(attempt.attempt_id, execution_status="succeeded")
        store.set_work_item_state(item.work_item_id, "review_ready")
        yield store, item, attempt


def register(store, item, attempt, name="audit.json", raw=b'{"rows":5}', **kwargs):
    path = Path(item.workspace_path) / name
    path.write_bytes(raw)
    return store.register_artifact(item.work_item_id, attempt_id=attempt.attempt_id,
        kind="business.file", title=name, path=path,
        sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw), **kwargs)


def request(store, method, params):
    return asyncio.run(WorkLedgerHandler(WorkLedgerCoordinator(store)).handle(method, params))


@pytest.mark.parametrize("name,raw", [
    ("audit.json", b'{"rows":5}'), ("orders.csv", b'id,amount\nA,10\n'),
    ("issues.txt", b'row 2: negative amount'),
    ("notes.md", '<script>window.attack()</script>\n# 日本語'.encode()),
], ids=["json", "csv", "text", "markdown"])
def test_preview_returns_exact_text_without_accepting_work(ledger, name, raw):
    store, item, attempt = ledger
    artifact = register(store, item, attempt, name, raw)
    result = request(store, Method.WORK_ARTIFACT_READ,
        {"work_item_id": item.work_item_id, "artifact_id": artifact.artifact_id})
    assert result["ok"] and result["content"] == raw.decode()
    assert result["sha256"] == artifact.sha256
    assert "path" not in result
    assert store.get_work_item(item.work_item_id).state == "review_ready"


def test_preview_requires_id_and_matching_work_not_a_path(ledger):
    store, item, attempt = ledger
    artifact = register(store, item, attempt)
    for params in ({"work_item_id": item.work_item_id, "path": artifact.path},
                   {"work_item_id": "another-work", "artifact_id": artifact.artifact_id}):
        result = request(store, Method.WORK_ARTIFACT_READ, params)
        assert result["ok"] is False and "content" not in result


def test_catalog_keeps_missing_and_changed_files_but_read_refuses(ledger):
    store, item, attempt = ledger
    missing = register(store, item, attempt, "missing.txt")
    changed = register(store, item, attempt, "changed.txt")
    Path(missing.path).unlink()
    Path(changed.path).write_text("different", encoding="utf-8")
    result = request(store, Method.WORK_ARTIFACTS_LIST, {"scope": "drafts"})
    assert {row["artifactId"] for row in result["files"]} == {missing.artifact_id, changed.artifact_id}
    assert all(row["execution"] == "succeeded" and row["workState"] == "review_ready" for row in result["files"])
    for artifact, error in ((missing, "missing"), (changed, "revision")):
        with pytest.raises(ValueError, match=error):
            read_registered_text(store, item.work_item_id, artifact.artifact_id)


@pytest.mark.parametrize("name,raw,error", [
    ("binary.txt", b'abc\0def', "UTF-8"), ("invalid.txt", b'\xff', "UTF-8"),
    ("large.txt", b'a' * (TEXT_LIMIT_BYTES + 1), "512 KiB"),
    ("app.html", b'<script>bad()</script>', "Preview supports"),
], ids=["nul", "encoding", "size", "html"])
def test_preview_is_bounded_text(ledger, name, raw, error):
    store, item, attempt = ledger
    artifact = register(store, item, attempt, name, raw)
    with pytest.raises(ValueError, match=error):
        read_registered_text(store, item.work_item_id, artifact.artifact_id)


def test_current_revision_wins_even_when_new_revision_is_pending(ledger):
    store, item, attempt = ledger
    old = register(store, item, attempt)
    newer = store.create_attempt(item.work_item_id, provider="test", task="Amend")
    current = register(store, item, newer, status="pending")
    result = request(store, Method.WORK_ARTIFACTS_LIST, {"scope": "drafts"})
    assert [row["artifactId"] for row in result["files"]] == [current.artifact_id]
    assert result["files"][0]["previewable"] is False
    with pytest.raises(ValueError, match="newer"):
        read_registered_text(store, item.work_item_id, old.artifact_id)
    with pytest.raises(ValueError, match="not a registered"):
        read_registered_text(store, item.work_item_id, current.artifact_id)


def test_outside_workspace_and_changed_symlink_target_refused(ledger, tmp_path):
    store, item, attempt = ledger
    outside = tmp_path / "private.txt"
    outside.write_text("outside", encoding="utf-8")
    record = store.register_artifact(item.work_item_id, attempt_id=attempt.attempt_id,
        kind="business.file", path=outside, sha256=hashlib.sha256(b'outside').hexdigest())
    with pytest.raises(ValueError, match="workspace"):
        read_registered_text(store, item.work_item_id, record.artifact_id)
    inside = register(store, item, attempt, "inside.txt", b'outside')
    Path(inside.path).unlink()
    try:
        Path(inside.path).symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this Windows host")
    with pytest.raises(ValueError, match="workspace"):
        read_registered_text(store, item.work_item_id, inside.artifact_id)


def test_draft_limit_counts_deliveries_and_project_scope_is_explicit(ledger, tmp_path):
    store, item, attempt = ledger
    register(store, item, attempt)
    register(store, item, attempt, "issues.txt")
    # Non-artifact history cannot hide a delivery from the indexed shelf.
    for index in range(501):
        store.create_work_item(item.project_id, title=f"No output {index}", workspace_path=item.workspace_path)
    draft = request(store, Method.WORK_ARTIFACTS_LIST, {"scope": "drafts", "limit": 1})
    assert len(draft["files"]) == 2 and draft["complete"]
    kept_root = tmp_path / "kept"
    kept_root.mkdir()
    kept = store.create_or_get_project(kept_root)
    other = store.create_work_item(kept.project_id, title="Kept", workspace_path=kept_root)
    other_attempt = store.create_attempt(other.work_item_id, provider="test", task="Kept")
    other_file = register(store, other, other_attempt)
    result = request(store, Method.WORK_ARTIFACTS_LIST, {"scope": "project", "project_id": kept.project_id})
    assert [row["artifactId"] for row in result["files"]] == [other_file.artifact_id]
    assert request(store, Method.WORK_ARTIFACTS_LIST, {"scope": "project", "project_id": item.project_id})["ok"] is False
    assert request(store, Method.WORK_ARTIFACTS_LIST, {})["ok"] is False
    # Promoting a Draft files all its Work under the retained directory; no copies.
    promoted = store.create_or_get_project(item.workspace_path)
    store.reassign_workspace_to_project(item.workspace_path, promoted.project_id)
    assert request(store, Method.WORK_ARTIFACTS_LIST, {"scope": "drafts"})["files"] == []
    assert len(request(store, Method.WORK_ARTIFACTS_LIST, {"scope": "project", "project_id": promoted.project_id})["files"]) == 2


def test_artifact_preview_survives_ledger_reopen(ledger, tmp_path):
    store, item, attempt = ledger
    artifact = register(store, item, attempt)
    with WorkLedgerStore(tmp_path / "ledger.sqlite3") as reopened:
        result = request(reopened, Method.WORK_ARTIFACTS_LIST, {"scope": "drafts"})
        assert result["files"][0]["artifactId"] == artifact.artifact_id
        assert read_registered_text(reopened, item.work_item_id, artifact.artifact_id)["content"] == '{"rows":5}'
