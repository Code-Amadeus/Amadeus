"""Synthetic recovery and selective source-use contracts at the live Host boundary."""
import asyncio
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from unittest.mock import Mock

import pytest
import eidema

from core import session_manager as sm
from core import chat_history_projection as projection
from server.reference_deadline import deadline_now
from server.memory_service import HostMemoryService
from server.memory_service import SOURCE_NAMESPACE
from server.memory_source_policy import SourcePolicyUnavailable
from server.protocol import Method
from test_memory_service import host as host, remember, forget, source_for


def model_text(service):
    return json.dumps(service.project_history("A", sm.conversation_history.recent()))


async def restart(host, *, config=None):
    service = HostMemoryService(config or host.config,
        project_for_session=lambda _sid:host.project["id"])
    service.start()
    await service._startup
    return service


@pytest.mark.parametrize("evidence", ["legacy", "sidecar", "corrupt_sidecar"])
async def test_runtime_import_failure_uses_available_policy_evidence(host, monkeypatch, evidence):
    assert sm.append_session_message("A", role="user", content="UNRELATED_HISTORY", turn_id="unrelated", message_id="unrelated")
    origin, _ = source_for("before PRIVATE_FORGET after", role="user")
    saved = await remember(host, "summary", source={**origin, "start_cp":7, "end_cp":21})
    await forget(host, saved["memory_id"])
    await host.service.close()
    policy_path = host.service._policy.path
    if evidence == "legacy":
        policy_path.unlink()
    elif evidence == "sidecar":
        host.config.db_path.unlink()
    else:
        policy_path.write_bytes(b"damaged derived sidecar")
    monkeypatch.setattr("server.memory_service.importlib.import_module", Mock(side_effect=ImportError("synthetic")))
    service = await restart(host)
    try:
        text = model_text(service)
        assert "UNRELATED_HISTORY" in text and "before [memory source omitted] after" in text
        assert "PRIVATE_FORGET" not in text
        status = await service.status(service.context("A"))
        assert status["source_policy_state"] == "ready" and status["state"] == "unavailable"
    finally:
        await service.close()


async def test_owner_lock_failure_preserves_forgotten_retired_and_unrelated_sources(host):
    origin, _ = source_for("before PRIVATE_OLD after", role="user")
    saved = await remember(host, source={**origin, "start_cp":7, "end_cp":18})
    assert (await host.request(Method.MEMORY_CORRECT, memory_id=saved["memory_id"], expected_version=1,
        operation_id="correct", item_id="correct-item", text="current", mode="error"))["ok"]
    assert sm.append_session_message("A", role="user", content="UNRELATED", turn_id="other", message_id="other")
    competing = await restart(host)  # The first service still owns the core lock.
    try:
        assert competing._store is None
        text = model_text(competing)
        assert "PRIVATE_OLD" not in text and "UNRELATED" in text
        assert "earlier assertion corrected" in text
    finally:
        await competing.close()


@pytest.mark.parametrize("after_commit", [False, True])
async def test_durable_prepare_survives_restart_at_both_mutation_crash_boundaries(host, after_commit):
    origin, _ = source_for("before PRIVATE_CRASH after", role="user")
    saved = await remember(host, source={**origin, "start_cp":7, "end_cp":20})
    assert sm.append_session_message("A", role="user", content="UNRELATED", turn_id="other", message_id="other")
    service, context = host.service, host.service.context("A")
    def interrupted_owner():
        access = service._access(context)
        preview = service._store.prepare_mutation(access, saved["memory_id"], 1,
            operation_id="crash-forget", item_id="crash-item", kind="forget")
        service._policy.prepare(context, "crash-forget", "crash-item", preview.source_spans, preview.memory_ids)
        if after_commit:
            service._store.forget(access, saved["memory_id"], 1,
                operation_id="crash-forget", item_id="crash-item", now_ms=100)
        # A process death here loses no needed policy: prepare is already FULL committed.
    await service._db.run(interrupted_owner)
    assert "PRIVATE_CRASH" not in model_text(service) and "UNRELATED" in model_text(service)
    with closing(sqlite3.connect(service._policy.path)) as db:
        stored = db.execute("SELECT value FROM source_policy").fetchone()[0]
    assert "PRIVATE_CRASH" not in stored and "summary" not in stored and saved["memory_id"] in stored
    await service.close()
    recovered = await restart(host)
    try:
        assert recovered._policy.value["pending"] == []
        assert ("PRIVATE_CRASH" in model_text(recovered)) is (not after_commit)
        assert "UNRELATED" in model_text(recovered)
    finally:
        await recovered.close()


async def test_failure_after_core_commit_before_host_publication_retains_exact_mask(host, monkeypatch):
    origin, _ = source_for("before PRIVATE_PUBLISH after", role="user")
    saved = await remember(host, source={**origin, "start_cp":7, "end_cp":22})
    assert sm.append_session_message("A", role="user", content="UNRELATED", turn_id="other", message_id="other")
    real = host.service._policy.reconcile
    monkeypatch.setattr(host.service._policy, "reconcile", Mock(side_effect=OSError("synthetic publication failure")))
    result = await host.request(Method.MEMORY_FORGET, memory_id=saved["memory_id"], expected_version=1,
        operation_id="failed-publication", item_id="failed-item")
    assert result["ok"] and result["operation"]["status"] == "forgotten"
    assert result["source_policy_pending"] and host.service._policy.value["pending"]
    assert "PRIVATE_PUBLISH" not in model_text(host.service) and "UNRELATED" in model_text(host.service)
    monkeypatch.setattr(host.service._policy, "reconcile", real)
    await host.service.close()
    recovered = await restart(host)
    try:
        assert not recovered._policy.value["pending"]
        assert "PRIVATE_PUBLISH" not in model_text(recovered)
    finally:
        await recovered.close()


@pytest.mark.parametrize("empty", [False, True])
async def test_no_readable_evidence_is_explicit_recovery_while_new_empty_store_is_safe(host, monkeypatch, empty):
    await host.service.close()
    host.service._policy.path.unlink()
    host.config.db_path.write_bytes(b"" if empty else b"damaged source store")
    monkeypatch.setattr("server.memory_service.importlib.import_module", Mock(side_effect=ImportError("synthetic")))
    service = await restart(host)
    try:
        if empty:
            assert service.project_history("A", [{"role":"user", "content":"ordinary"}])
        else:
            with pytest.raises(SourcePolicyUnavailable, match="recovery_required"):
                service.project_history("A", [{"role":"user", "content":"unverified"}])
            assert (await service.status(service.context("A")))["source_policy_state"] == "recovery_required"
    finally:
        await service.close()


async def test_sources_use_bounded_committed_snapshot_and_project_off_loop(host, monkeypatch):
    for index in range(4):
        assert sm.append_session_message("A", role="user", content=f"row {index}", turn_id=str(index), message_id=str(index))
    monkeypatch.setattr(sm, "_read_session_history", Mock(side_effect=AssertionError("source request reopened Session")))
    owner_thread, projected = threading.get_ident(), []
    real = projection.project_memory_source
    def checked(*args, **kwargs):
        projected.append(threading.get_ident())
        assert threading.get_ident() != owner_thread
        return real(*args, **kwargs)
    monkeypatch.setattr(projection, "project_memory_source", checked)
    result = await host.request(Method.MEMORY_SOURCES, limit=2)
    assert result["ok"] and [source["text"] for source in result["sources"]] == ["row 2", "row 3"]
    source = {key:result["sources"][0][key] for key in ("session_id", "message_id", "projection_version", "revision")}
    await remember(host, "selected summary", source={**source, "start_cp":0, "end_cp":5})
    assert len(projected) == 3


async def test_inactive_session_write_cannot_replace_current_committed_source_snapshot(host):
    assert sm.append_session_message("A", role="user", content="A original", turn_id="A-turn", message_id="A-message")
    sm.create_session("B")
    assert sm.append_session_message("B", role="user", content="B original", turn_id="B-turn", message_id="B-message")
    assert sm.append_session_message("A", role="assistant", content="late A", turn_id="A-turn", message_id="A-late")
    assert sm.read_committed_session_source("B", "B-message", revision=1)["text"] == "B original"
    assert [row["content"] for row in sm.committed_session_recent("A")] == ["A original", "late A"]
    assert sm.delete_session("A")
    with pytest.raises(LookupError):
        sm.committed_session_recent("A")
    assert sm.read_committed_session_source("B", "B-message", revision=1)["text"] == "B original"


async def test_configured_user_change_cannot_erase_another_namespace_policy(host):
    origin, _ = source_for("before PRIVATE_OWNER after", role="user")
    saved = await remember(host, source={**origin, "start_cp":7, "end_cp":20})
    await forget(host, saved["memory_id"])
    await host.service.close()
    before = host.service._policy.path.read_bytes()
    other = await restart(host, config=replace(host.config, user_id="other-user"))
    try:
        assert other._store is None and host.service._policy.path.read_bytes() == before
        with pytest.raises(SourcePolicyUnavailable):
            model_text(other)
    finally:
        await other.close()
    original = await restart(host)
    try:
        assert "PRIVATE_OWNER" not in model_text(original)
    finally:
        await original.close()


async def test_completed_forget_retry_keeps_pending_mask_until_snapshot_publication(host, monkeypatch):
    origin, _ = source_for("before PRIVATE_RETRY after", role="user")
    saved = await remember(host, source={**origin, "start_cp":7, "end_cp":20})
    real_reconcile = host.service._policy.reconcile
    monkeypatch.setattr(host.service._policy, "reconcile", Mock(side_effect=OSError("synthetic")))
    result = await host.request(Method.MEMORY_FORGET, memory_id=saved["memory_id"], expected_version=1,
        operation_id="replay-mask", item_id="replay-item")
    assert result["ok"] and result["source_policy_pending"] and host.service._policy.value["pending"]
    monkeypatch.setattr(host.service._policy, "reconcile", real_reconcile)
    started, release = threading.Event(), threading.Event()
    real_forget = host.service._store.forget
    def delayed_replay(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return real_forget(*args, **kwargs)
    monkeypatch.setattr(host.service._store, "forget", delayed_replay)
    retry = asyncio.create_task(host.request(Method.MEMORY_FORGET, memory_id=saved["memory_id"], expected_version=1,
        operation_id="replay-mask", item_id="replay-item"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        assert "PRIVATE_RETRY" not in model_text(host.service)
        release.set()
        result = await retry
        assert result["ok"] and result["operation"]["status"] == "forgotten"
    finally:
        release.set()


async def test_rejection_after_durable_prepare_restores_only_target_and_keeps_unrelated_history(host, monkeypatch):
    origin, _ = source_for("before PRIVATE_REJECT after", role="user")
    saved = await remember(host, source={**origin, "start_cp":7, "end_cp":21})
    assert sm.append_session_message("A", role="user", content="UNRELATED", turn_id="other", message_id="other")
    started, release = threading.Event(), threading.Event()
    def rejected(*_args, **_kwargs):
        started.set()
        assert release.wait(3)
        raise eidema.MemoryConflict("synthetic rejection")
    monkeypatch.setattr(host.service._store, "forget", rejected)
    task = asyncio.create_task(host.request(Method.MEMORY_FORGET, memory_id=saved["memory_id"], expected_version=1,
        operation_id="reject-after-prepare", item_id="reject-after-item"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        assert "PRIVATE_REJECT" not in model_text(host.service) and "UNRELATED" in model_text(host.service)
        release.set()
        assert (await task)["error"] == "conflict"
        assert "PRIVATE_REJECT" in model_text(host.service) and "UNRELATED" in model_text(host.service)
        assert not host.service._policy.value["pending"]
    finally:
        release.set()


def test_malformed_host_coordinates_remain_unavailable_across_repeated_projection_reads(tmp_path):
    from config.memory import MemoryConfig
    service = HostMemoryService(MemoryConfig(enabled=False, db_path=tmp_path / "new.sqlite3"))
    service._policy.value = {**service._policy.value, "exclusions":[{
        "namespace":SOURCE_NAMESPACE, "record_key":"not valid JSON",
        "revision":1, "start_cp":0, "end_cp":3}]}
    try:
        for _ in range(2):
            with pytest.raises(SourcePolicyUnavailable, match="recovery_required"):
                service.project_history("A", [{"role":"user", "content":"unverified", "message_id":"source", "revision":1}])
            assert not service._exclusions_ready
    finally:
        for lane in (service._db, service._query, service._model):
            lane.close()


async def test_secondary_publication_failure_preserves_original_rejection_and_exact_mask(host, monkeypatch):
    origin, _ = source_for("before PRIVATE_PRIMARY after", role="user")
    saved = await remember(host,source={**origin,"start_cp":7,"end_cp":22})
    monkeypatch.setattr(host.service._store,"forget",Mock(side_effect=eidema.MemoryConflict("primary")))
    monkeypatch.setattr(host.service._policy,"reconcile",Mock(side_effect=OSError("secondary")))
    result = await host.request(Method.MEMORY_FORGET,memory_id=saved["memory_id"],expected_version=1,
        operation_id="primary",item_id="primary-item")
    assert result["error"] == "conflict"
    assert host.service._policy.value["pending"]
    assert "PRIVATE_PRIMARY" not in model_text(host.service)
    assert (await host.service.status(host.service.context("A")))["source_policy_reason"] == "publication_pending"


async def test_single_owner_atomic_snapshot_needs_no_discarded_operation_lookup(host, monkeypatch):
    monkeypatch.setattr(eidema.MemoryStore,"operation_status",Mock(side_effect=AssertionError("unused lookup")))
    saved = await remember(host)
    await forget(host,saved["memory_id"])
    await host.service.close()
    service = await restart(host)
    try:
        assert service._store is not None and not service._policy.value["pending"]
    finally:
        await service.close()


@pytest.mark.parametrize(("version", "indexes", "core_error", "allowed"), [
    ((3, 45, 1), ("memory_fts", "retrieval_fts"), False, True),
    ((3, 45, 1), ("memory_fts",), True, False),
    ((3, 45, 1), ("unknown_fts",), False, False),
    ((3, 46, 1), ("memory_fts",), False, False),
])
def test_recovery_validates_core_pages_despite_legacy_fts_reports(
    monkeypatch, version, indexes, core_error, allowed,
):
    from server.memory_policy_recovery import PolicyRecoveryError, _check_core_integrity

    monkeypatch.setattr(sqlite3, "sqlite_version_info", version)
    db = Mock()
    findings = [(f"malformed inverted index for FTS5 table main.{name}",) for name in indexes]
    if core_error:
        findings.append(("database disk image is malformed",))
    db.execute.return_value.fetchall.return_value = findings

    if allowed:
        _check_core_integrity(db)
    else:
        with pytest.raises(PolicyRecoveryError, match="core_unreadable"):
            _check_core_integrity(db)
    db.execute.assert_called_once_with("PRAGMA quick_check")


async def test_recovery_cli_rebuilds_damaged_sidecar_preserving_originals_and_exclusions(host):
    from server.memory_policy_recovery import recover_source_policy
    from server.memory_source_policy import SourcePolicyProjection
    origin, _ = source_for("before PRIVATE_RECOVER after",role="user")
    saved = await remember(host,source={**origin,"start_cp":7,"end_cp":22})
    await forget(host,saved["memory_id"])
    await host.service.close()
    durable_tables = ("memories", "operations", "forgotten", "exclusions")
    with closing(sqlite3.connect(host.config.db_path)) as db:
        durable_before = {table:db.execute(f"SELECT * FROM {table}").fetchall()
            for table in durable_tables}
    assert not durable_before["memories"]
    assert all(durable_before[table] for table in ("operations", "forgotten", "exclusions"))
    policy_path = host.service._policy.path
    policy_path.write_bytes(b"synthetic damaged derived policy")
    core_before = host.config.db_path.read_bytes()
    result = await asyncio.to_thread(recover_source_policy,host.config.db_path,host.config.user_id)
    backup = Path(result["backup_directory"])
    assert (backup/policy_path.name).read_bytes() == b"synthetic damaged derived policy"
    assert (backup/host.config.db_path.name).read_bytes() == core_before
    assert host.config.db_path.read_bytes() == core_before
    recovered = SourcePolicyProjection(host.config.db_path,host.config.user_id)
    recovered.load()
    assert recovered.ready and recovered.value["exclusions"] and not recovered.value["pending"]
    assert recovered.value["change_seq"] > 0
    with closing(sqlite3.connect(host.config.db_path)) as db:
        assert {table:db.execute(f"SELECT * FROM {table}").fetchall()
            for table in durable_tables} == durable_before
    service = await restart(host)
    try:
        assert service._store is not None and "PRIVATE_RECOVER" not in model_text(service)
        replay = await service._db.run(lambda:service._store.operation_status(
            service._access(service.context("A")), operation_id="save", item_id="item"))
        assert replay.status == "retired" and replay.replayed
    finally:
        await service.close()


async def test_recovery_cli_restores_pristine_core_for_panel_lifecycle(host):
    from server.handlers.memory_handler import MemoryHandler
    from server.memory_source_policy import SourcePolicyProjection
    await host.service.close()
    policy_path = host.service._policy.path
    policy_path.write_bytes(b"damaged pristine derived policy")
    core_before = host.config.db_path.read_bytes()
    completed = await asyncio.to_thread(subprocess.run, [sys.executable,
        str(Path(__file__).resolve().parents[1] / "tools" / "recover_memory_policy.py"),
        "--db", str(host.config.db_path), "--user-id", host.config.user_id],
        capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["state"] == "recovered" and result["change_seq"] == 0
    assert result["exclusions"] == result["retirements"] == 0
    backup = Path(result["backup_directory"])
    assert (backup / policy_path.name).read_bytes() == b"damaged pristine derived policy"
    assert (backup / host.config.db_path.name).read_bytes() == core_before
    assert host.config.db_path.read_bytes() == core_before
    policy = SourcePolicyProjection(host.config.db_path, host.config.user_id)
    policy.load()
    assert policy.ready and policy.value["user_key"] == host.service._policy.user_key

    service = await restart(host)
    try:
        assert service._store is not None
        handler, context = MemoryHandler(service), service.context("A")
        async def request(method, **params):
            return await handler.handle(method, {"session_id":"A",
                "context_token":context["context_token"], **params})
        origin, text = source_for("before PRIVATE_PRISTINE after", role="user")
        saved = await request(Method.MEMORY_REMEMBER, operation_id="save", item_id="item",
            text="pristine memory", scope="character", source={**origin,
                "start_cp":7, "end_cp":text.index(" after")})
        assert saved["ok"]
        corrected = await request(Method.MEMORY_CORRECT, memory_id=saved["operation"]["memory_id"],
            expected_version=1, operation_id="correct", item_id="correct-item",
            text="corrected memory", mode="change")
        assert corrected["ok"]
        successor = corrected["operation"]["memory_id"]
        detail = await request(Method.MEMORY_GET, memory_id=successor)
        assert detail["memory"]["text"] == "corrected memory"
        preview = await request(Method.MEMORY_FORGET_PREVIEW, memory_id=successor, expected_version=1)
        forgotten = await request(Method.MEMORY_FORGET, memory_id=successor, expected_version=1,
            operation_id="forget", item_id="forget-item", confirmed_targets=preview["preview"]["targets"])
        assert forgotten["ok"] and (await request(Method.MEMORY_LIST))["items"] == []
        assert "PRIVATE_PRISTINE" not in model_text(service)
    finally:
        await service.close()


@pytest.mark.parametrize("mutation", [
    "UPDATE store_state SET change_seq=1",
    "UPDATE store_state SET change_seq=-1",
    "UPDATE store_state SET cleanup_pending=1",
    "INSERT INTO receipts VALUES('orphan', '{}')",
    "INSERT INTO origins(memory_id,namespace,record_key,revision,start_cp,end_cp) VALUES('orphan', 'synthetic', 'source', 1, NULL, NULL)",
    "INSERT INTO search_documents(memory_id,row_version,text) VALUES('orphan',1,'synthetic')",
    "INSERT INTO embeddings VALUES('orphan',1,'synthetic',0,1,X'00000000')",
], ids=["noninitial-sequence", "negative-sequence", "cleanup", "receipt", "origin", "search", "embedding"])
async def test_recovery_rejects_unbound_durable_state_without_replacing_evidence(host, mutation):
    from server.memory_policy_recovery import PolicyRecoveryError, recover_source_policy
    await host.service.close()
    with closing(sqlite3.connect(host.config.db_path)) as db, db:
        db.execute(mutation)
    host.service._policy.path.write_bytes(b"damaged unbound policy")
    originals = (host.config.db_path.read_bytes(), host.service._policy.path.read_bytes())
    reason = "store_rollback" if "change_seq=-1" in mutation else "identity_unverifiable"
    with pytest.raises(PolicyRecoveryError, match=reason):
        await asyncio.to_thread(recover_source_policy, host.config.db_path, host.config.user_id)
    assert (host.config.db_path.read_bytes(), host.service._policy.path.read_bytes()) == originals
    assert not list(host.config.db_path.parent.glob(host.config.db_path.name + ".policy-recovery-*"))


@pytest.mark.parametrize("evidence,reason", [("binding", "identity_mismatch"),
    ("rollback", "store_rollback"), ("exclusions", "identity_unverifiable"),
    ("retirements", "identity_unverifiable"), ("pending", "identity_unverifiable")])
async def test_pristine_recovery_refuses_known_sidecar_contradictions(host, evidence, reason):
    from server.memory_policy_recovery import PolicyRecoveryError, recover_source_policy
    await host.service.close()
    policy, user_id = host.service._policy, host.config.user_id
    source = {"namespace":SOURCE_NAMESPACE, "record_key":json.dumps(["A", "source", "v1"]),
        "revision":1, "start_cp":None, "end_cp":None}
    if evidence == "binding":
        user_id = "other-user"
    elif evidence == "rollback":
        policy._publish({**policy.value, "change_seq":1})
    elif evidence == "exclusions":
        policy._publish({**policy.value, "exclusions":[source]})
    elif evidence == "retirements":
        policy._publish({**policy.value, "retirements":[{"source":source, "reason":"corrected_by_user"}]})
    else:
        policy._publish({**policy.value, "pending":[{"operation_id":"prepared", "item_id":"item",
            "character_id":"synthetic-character", "project_id":None, "sources":[source],
            "memory_ids":["synthetic-memory"]}]})
    originals = (host.config.db_path.read_bytes(), policy.path.read_bytes())
    with pytest.raises(PolicyRecoveryError, match=reason):
        await asyncio.to_thread(recover_source_policy, host.config.db_path, user_id)
    assert (host.config.db_path.read_bytes(), policy.path.read_bytes()) == originals
    assert not list(host.config.db_path.parent.glob(host.config.db_path.name + ".policy-recovery-*"))


@pytest.mark.parametrize("namespaces", [("other-user",), ("synthetic-user", "other-user")],
    ids=["foreign", "mixed"])
async def test_recovery_rejects_foreign_and_mixed_core_namespaces_with_damaged_sidecar(host, namespaces):
    from server.memory_policy_recovery import PolicyRecoveryError, recover_source_policy
    await host.service.close()
    with closing(sqlite3.connect(host.config.db_path)) as db, db:
        db.executemany("INSERT INTO operations(user_id,operation_id,item_id,operation_kind,memory_id,outcome,fingerprint) VALUES(?,'operation','item','remember',NULL,'retired',NULL)",
            [(user_id,) for user_id in namespaces])
        db.execute("UPDATE store_state SET change_seq=1")
    host.service._policy.path.write_bytes(b"damaged foreign derived policy")
    originals = (host.config.db_path.read_bytes(), host.service._policy.path.read_bytes())
    with pytest.raises(PolicyRecoveryError, match="identity_mismatch"):
        await asyncio.to_thread(recover_source_policy, host.config.db_path, host.config.user_id)
    assert (host.config.db_path.read_bytes(), host.service._policy.path.read_bytes()) == originals
    assert not list(host.config.db_path.parent.glob(host.config.db_path.name + ".policy-recovery-*"))


@pytest.mark.parametrize("mutation", ["UPDATE store_state SET schema_version=3",
    "DROP TABLE embeddings", "DROP TABLE store_state"])
async def test_pristine_recovery_requires_supported_complete_core_schema(host, mutation):
    from server.memory_policy_recovery import PolicyRecoveryError, recover_source_policy
    await host.service.close()
    with closing(sqlite3.connect(host.config.db_path)) as db, db:
        db.execute(mutation)
    host.service._policy.path.write_bytes(b"damaged derived policy")
    originals = (host.config.db_path.read_bytes(), host.service._policy.path.read_bytes())
    with pytest.raises(PolicyRecoveryError, match="core_unreadable"):
        await asyncio.to_thread(recover_source_policy, host.config.db_path, host.config.user_id)
    assert (host.config.db_path.read_bytes(), host.service._policy.path.read_bytes()) == originals


def test_recovery_refuses_missing_core_without_creating_authority(tmp_path):
    from server.memory_policy_recovery import PolicyRecoveryError, recover_source_policy
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(PolicyRecoveryError, match="core_missing"):
        recover_source_policy(path, "synthetic-user")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failed_commit", ["manifest","sidecar"])
async def test_recovery_commit_failure_preserves_originals_backups_and_cleans_temporary_files(host, monkeypatch,
                                                                                           failed_commit):
    from config import durable_io
    from server.memory_policy_recovery import recover_source_policy
    origin, _ = source_for("before PRIVATE_COMMIT after",role="user")
    saved = await remember(host,source={**origin,"start_cp":7,"end_cp":21})
    await forget(host,saved["memory_id"])
    await host.service.close()
    policy_path = host.service._policy.path
    policy_path.write_bytes(b"synthetic damaged policy before repair")
    originals = (host.config.db_path.read_bytes(),policy_path.read_bytes())
    real_replace = durable_io._replace
    failed_name = "manifest.json" if failed_commit == "manifest" else policy_path.name
    def fail_commit(source,destination):
        if destination.name == failed_name:
            raise OSError("synthetic commit failure")
        return real_replace(source,destination)
    with monkeypatch.context() as local:
        local.setattr(durable_io,"_replace",fail_commit)
        with pytest.raises(OSError,match="synthetic commit failure"):
            await asyncio.to_thread(recover_source_policy,host.config.db_path,host.config.user_id)
    assert (host.config.db_path.read_bytes(),policy_path.read_bytes()) == originals
    backups = list(host.config.db_path.parent.glob(host.config.db_path.name+".policy-recovery-*"))
    assert len(backups) == 1
    assert (backups[0]/host.config.db_path.name).read_bytes() == originals[0]
    assert (backups[0]/policy_path.name).read_bytes() == originals[1]
    assert (backups[0]/"manifest.json").exists() == (failed_commit == "sidecar")
    assert not list(host.config.db_path.parent.glob("*.rebuild-*"))
    assert not list(host.config.db_path.parent.rglob("*.tmp"))


@pytest.mark.parametrize("populated", [False, True])
async def test_recovery_refuses_live_owner_wrong_identity_and_unreadable_core_without_changes(host, populated):
    from server.memory_policy_recovery import PolicyRecoveryError, recover_source_policy
    if populated:
        await remember(host)
    with pytest.raises(eidema.MemoryConflict,match="store_already_owned"):
        await asyncio.to_thread(recover_source_policy,host.config.db_path,host.config.user_id)
    await host.service.close()
    originals = (host.config.db_path.read_bytes(),host.service._policy.path.read_bytes())
    with pytest.raises(PolicyRecoveryError,match="identity_mismatch"):
        await asyncio.to_thread(recover_source_policy,host.config.db_path,"other-user")
    assert (host.config.db_path.read_bytes(),host.service._policy.path.read_bytes()) == originals
    host.config.db_path.write_bytes(b"synthetic damaged authority")
    with pytest.raises(PolicyRecoveryError,match="core_unreadable"):
        await asyncio.to_thread(recover_source_policy,host.config.db_path,host.config.user_id)
    assert host.config.db_path.read_bytes() == b"synthetic damaged authority"
    assert host.service._policy.path.read_bytes() == originals[1]


async def test_malformed_first_projection_and_source_link_fail_before_source_read(host, monkeypatch):
    from server.memory_service import MemoryRequestError
    host.service._policy.value = {**host.service._policy.value,"pending":[{
        "operation_id":"bad","item_id":"bad-item","memory_ids":[],
        "sources":[{"namespace":SOURCE_NAMESPACE,"record_key":"invalid json",
            "revision":1,"start_cp":0,"end_cp":1}]}]}
    monkeypatch.setattr(sm,"read_committed_session_source",Mock(side_effect=AssertionError("premature source read")))
    for session_id in ("A",""):
        with pytest.raises(SourcePolicyUnavailable):
            host.service.project_history(session_id,[{"role":"user","content":"unverified"}])
    with pytest.raises(MemoryRequestError,match="conflict"):
        await host.service._source(host.service.context("A"),{"session_id":"A"},"summary")
    assert host.service._pending_sources == {}


async def test_memory_recall_and_final_validation_respect_explicit_stage_deadlines(host, monkeypatch):
    from server.memory_service import MemoryRequestError
    host.service._automatic_threshold = .84
    now = deadline_now()
    reference = await host.service.recall_for_session("A","synthetic",deadline=now+1)
    assert reference.deadline == now+1
    with monkeypatch.context() as local:
        local.setattr(host.service._db,"run",Mock(side_effect=AssertionError("submitted after deadline")))
        with pytest.raises(MemoryRequestError,match="busy"):
            await host.service.validate_reference(reference,deadline=now-1)


async def test_source_link_rechecks_policy_readiness_after_offloop_source_lookup(host, monkeypatch):
    from server.memory_service import MemoryRequestError
    origin, text = source_for("stable source")
    original = sm.read_committed_session_source
    def source_then_invalid_policy(*args,**kwargs):
        result = original(*args,**kwargs)
        host.service._policy.value = {**host.service._policy.value,"exclusions":[{
            "namespace":SOURCE_NAMESPACE,"record_key":"invalid coordinates",
            "revision":1,"start_cp":0,"end_cp":1}]}
        return result
    monkeypatch.setattr(sm,"read_committed_session_source",source_then_invalid_policy)
    with pytest.raises(MemoryRequestError,match="conflict"):
        await host.service._source(host.service.context("A"),{**origin,"start_cp":0,"end_cp":len(text)},"summary")


@pytest.mark.parametrize("damage,reason", [("coordinates","invalid_coordinates"),("rollback","store_rollback")])
async def test_recovery_does_not_discard_known_irreversible_rules_to_make_repair_pass(host, damage, reason):
    from server.memory_policy_recovery import PolicyRecoveryError, recover_source_policy
    origin, _ = source_for("before PRIVATE_COORDS after",role="user")
    saved = await remember(host,source={**origin,"start_cp":7,"end_cp":21})
    await forget(host,saved["memory_id"])
    await host.service.close()
    policy = host.service._policy
    if damage == "coordinates":
        value = {**policy.value,"exclusions":[{**policy.value["exclusions"][0],"record_key":"unknown coordinates"}]}
    else:
        value = {**policy.value,"change_seq":policy.value["change_seq"]+1}
    policy._publish(value)
    originals = (host.config.db_path.read_bytes(),policy.path.read_bytes())
    with pytest.raises(PolicyRecoveryError,match=reason):
        await asyncio.to_thread(recover_source_policy,host.config.db_path,host.config.user_id)
    assert (host.config.db_path.read_bytes(),policy.path.read_bytes()) == originals
