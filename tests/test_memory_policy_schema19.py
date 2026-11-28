"""Synthetic schema18/19 metadata, binding and recovery boundary controls."""
import builtins
from contextlib import closing
from dataclasses import asdict
from importlib.resources import files
import json
from pathlib import Path
import sqlite3

import eidema
import pytest

from config.memory import MemoryConfig
from core.chat_history_projection import MEMORY_TEXT_PROJECTION_VERSION
from server import memory_policy_metadata as metadata
from server.memory_policy_recovery import PolicyRecoveryError, recover_source_policy
from server.memory_service import HostMemoryService
from server.memory_source_policy import (SourcePolicyBindingMismatch, SourcePolicyProjection,
                                         SourcePolicyUnavailable)

USER = "synthetic-A"
SOURCE = {"namespace": "amadeus-message-v1", "record_key": json.dumps(["A", "source", MEMORY_TEXT_PROJECTION_VERSION]),
          "revision": 1, "start_cp": 7, "end_cp": 14}
OUTPUT = {**SOURCE, "record_key": json.dumps(["A", "output", MEMORY_TEXT_PROJECTION_VERSION]), "start_cp": None, "end_cp": None}


def schema(path, version):
    # Reconstruct schema18 from the bundled canonical SQL by removing only the
    # two schema19 evidence_only columns. The copied reader verifies every
    # resulting physical column and initial row against its SDK-owned manifest.
    sql = files(eidema).joinpath("schema.sql").read_text(encoding="utf-8")
    if version == 18:
        line = "    evidence_only INTEGER NOT NULL DEFAULT 0 CHECK(evidence_only IN (0,1)),\n"
        assert sql.count(line) == 2
        sql = sql.replace(line, "").replace("VALUES(1,19)", "VALUES(1,18)")
    with closing(sqlite3.connect(path)) as db:
        db.executescript(sql)
        assert metadata.is_pristine_core(db)


def downgrade18(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("ALTER TABLE source_states DROP COLUMN evidence_only")
        db.execute("ALTER TABLE source_changes DROP COLUMN evidence_only")
        db.execute("UPDATE store_state SET schema_version=18")


def seed_policy(path, version, users=(USER,)):
    schema(path, version)
    with closing(sqlite3.connect(path)) as db, db:
        for user in users:
            db.execute("INSERT INTO exclusions(user_id,namespace,record_key,revision,start_cp,end_cp) VALUES(?,?,?,?,?,?)",
                       (user, *SOURCE.values()))
            db.execute("INSERT INTO output_bindings(user_id,namespace,record_key,revision,invalid_reason) VALUES(?,?,?,?,?)",
                       (user, OUTPUT["namespace"], OUTPUT["record_key"], 1, "authorization_revoked"))
        db.execute("UPDATE store_state SET change_seq=3")


@pytest.mark.parametrize("version", [18, 19])
@pytest.mark.parametrize("sidecar", ["missing", "damaged", "bound"])
def test_metadata_bootstrap_is_read_only_sdk_independent_and_body_free(tmp_path, monkeypatch, version, sidecar):
    path = tmp_path / "core.sqlite3"
    seed_policy(path, version)
    projection = SourcePolicyProjection(path, USER)
    if sidecar == "bound":
        projection._publish(projection.value)
    elif sidecar == "damaged":
        projection.path.write_bytes(b"damaged synthetic sidecar")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    real_import, real_connect = builtins.__import__, sqlite3.connect
    reads = []
    def no_sdk(name, *args, **kwargs):
        if name == "eidema" or name.startswith("eidema."):
            raise AssertionError("SDK import at metadata fallback")
        return real_import(name, *args, **kwargs)
    def connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        def authorize(action, table, column, *_):
            if action == sqlite3.SQLITE_READ:
                reads.append((table, column))
                assert column not in {"text", "retained_text", "body", "vector", "condition_text", "lexical_text"}
            return sqlite3.SQLITE_OK
        db.set_authorizer(authorize)
        return db
    monkeypatch.setattr(builtins, "__import__", no_sdk)
    monkeypatch.setattr(sqlite3, "connect", connect)
    projection.bootstrap_v1()
    assert projection.ready and projection.value["change_seq"] == 3
    assert projection.value["exclusions"] == [SOURCE]
    assert projection.value["derived_sources"] == [OUTPUT]
    assert projection.value["retirements"] == [{"source": OUTPUT, "reason": "authorization_revoked"}]
    assert reads and {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


@pytest.mark.parametrize("version", [18, 19])
@pytest.mark.parametrize("sidecar", ["missing", "damaged", "bound"])
def test_mixed_core_requires_validated_binding_and_keeps_pending(tmp_path, version, sidecar):
    path = tmp_path / "core.sqlite3"
    seed_policy(path, version, (USER, "synthetic-B"))
    projection = SourcePolicyProjection(path, USER)
    pending = {"operation_id": "pending", "item_id": "item", "character_id": "kurisu",
               "project_id": None, "sources": [SOURCE], "memory_ids": ["mid"]}
    if sidecar == "bound":
        projection._publish({**projection.value, "pending": [pending]})
    elif sidecar == "damaged":
        projection.path.write_bytes(b"damaged binding")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    if sidecar == "bound":
        projection.bootstrap_v1()
        assert projection.ready and projection.value["pending"] == [pending]
        assert projection.value["exclusions"] == [SOURCE]
    else:
        with pytest.raises(SourcePolicyBindingMismatch):
            projection.bootstrap_v1()
        assert not projection.ready
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


@pytest.mark.parametrize("version", [18, 19])
def test_empty_constructor_cannot_bind_foreign_first_sdk_write(tmp_path, version):
    path = tmp_path / "core.sqlite3"
    projection = SourcePolicyProjection(path, USER)
    assert projection.ready and not projection._bound
    store = eidema.MemoryStore(path)
    try:
        store.remember(eidema.AccessScope("synthetic-B", "kurisu"),
                       eidema.MemoryScope("character", "synthetic-B", "kurisu"),
                       operation_id="save", item_id="item", text="foreign synthetic body", now_ms=1)
    finally:
        store.close()
    if version == 18:
        downgrade18(path)
    before = path.read_bytes()
    with pytest.raises(SourcePolicyBindingMismatch):
        projection.bootstrap_v1()
    assert not projection.ready and not projection._bound
    assert path.read_bytes() == before and not projection.path.exists()


@pytest.mark.parametrize("version", [18, 19])
async def test_host_open_foreign_materialization_refuses_history(tmp_path, version):
    path = tmp_path / "core.sqlite3"
    service = HostMemoryService(MemoryConfig(enabled=False, semantic_enabled=False, db_path=path, user_id=USER))
    assert service._policy.ready
    seed_policy(path, version, ("synthetic-B",))
    try:
        await service._open()
        assert service._store is None and not service._policy.ready
        assert service._source_policy_reason == "identity_mismatch"
        for _ in range(2):
            with pytest.raises(SourcePolicyUnavailable):
                service.project_history("A", [{"role": "user", "content": "unverified"}])
    finally:
        await service.close()


@pytest.mark.parametrize("version", [18, 19])
@pytest.mark.parametrize("sidecar", ["missing", "damaged", "bound"])
async def test_host_history_fallback_masks_sources_and_known_outputs_without_sdk(tmp_path, monkeypatch, version, sidecar):
    path = tmp_path / "core.sqlite3"
    seed_policy(path, version)
    service = HostMemoryService(MemoryConfig(enabled=False, semantic_enabled=False, db_path=path, user_id=USER))
    if sidecar == "damaged":
        service._policy.path.write_bytes(b"damaged derived metadata")
    elif sidecar == "bound":
        service._policy._publish(service._policy.value)
    import importlib
    original_import = importlib.import_module
    def unavailable(name, *args, **kwargs):
        if name == "eidema":
            raise ImportError("synthetic unavailable SDK")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(importlib, "import_module", unavailable)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    try:
        await service._open()
        assert service._store is None and service._policy.ready
        rows = [{"role": "user", "content": "before PRIVATE after", "message_id": "source", "revision": 1},
                {"role": "assistant", "content": "DERIVED_PRIVATE", "message_id": "output", "revision": 1},
                {"role": "user", "content": "UNRELATED", "message_id": "ordinary", "revision": 1}]
        rendered = json.dumps(service.project_history("A", rows))
        assert "PRIVATE" not in rendered and "UNRELATED" in rendered
        assert "before [memory source omitted] after" in rendered
        assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before
    finally:
        await service.close()


@pytest.mark.parametrize("version", [18, 19])
@pytest.mark.parametrize("users", [("synthetic-B",), (USER, "synthetic-B")], ids=["foreign", "mixed"])
def test_recovery_checks_new_source_stream_owners(tmp_path, version, users):
    path = tmp_path / "core.sqlite3"
    schema(path, version)
    with closing(sqlite3.connect(path)) as db, db:
        db.executemany("INSERT INTO source_streams(stream_id,scope_kind,user_id,character_id) VALUES(?,'character',?,'kurisu')",
                       [("stream-" + user, user) for user in users])
    sidecar = SourcePolicyProjection(path, USER).path
    sidecar.write_bytes(b"damaged binding")
    before = path.read_bytes(), sidecar.read_bytes()
    with pytest.raises(PolicyRecoveryError, match="identity_mismatch"):
        recover_source_policy(path, USER)
    assert (path.read_bytes(), sidecar.read_bytes()) == before


@pytest.mark.parametrize("version", [18, 19])
def test_later_live_prepare_and_rollback_keep_exact_policy(tmp_path, version):
    path = tmp_path / "core.sqlite3"
    seed_policy(path, version)
    old = SourcePolicyProjection(path, USER)
    old._publish({**old.value, "change_seq": 3})
    reader = SourcePolicyProjection(path, USER)
    reader.load()
    old.prepare({"character_id": "kurisu", "project_id": None}, "op", "item",
                [eidema.SourceSpan(**SOURCE)], ["mid"])
    reader.bootstrap_v1()
    assert reader.value["pending"] == old.value["pending"]
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE store_state SET change_seq=2")
    before = old.path.read_bytes()
    with pytest.raises(ValueError, match="source_policy_store_rollback"):
        reader.bootstrap_v1()
    assert old.path.read_bytes() == before and reader.value["pending"] == old.value["pending"]


@pytest.mark.parametrize("version", [18, 19])
@pytest.mark.parametrize("mutation", [
    "UPDATE maintenance_clock SET last_reservation_ms=1",
    "INSERT INTO origins(memory_id,namespace,record_key,revision,start_cp,end_cp) VALUES('orphan','synthetic','source',1,NULL,NULL)",
    "INSERT INTO receipts(memory_id,retained_text) VALUES('orphan','synthetic')",
], ids=["clock", "orphan-origin", "orphan-receipt"])
def test_recovery_requires_physical_pristine_proof(tmp_path, version, mutation):
    path = tmp_path / "core.sqlite3"
    schema(path, version)
    with closing(sqlite3.connect(path)) as db, db:
        # Explicit stable columns preserve the original negative fixture's
        # meaning across schema2 and schema19 additions.
        db.execute(mutation)
    sidecar = SourcePolicyProjection(path, USER).path
    sidecar.write_bytes(b"damaged original")
    before = path.read_bytes(), sidecar.read_bytes()
    with pytest.raises(PolicyRecoveryError, match="identity_unverifiable"):
        recover_source_policy(path, USER)
    assert (path.read_bytes(), sidecar.read_bytes()) == before
    assert not list(tmp_path.glob("*.policy-recovery-*"))


@pytest.mark.parametrize("version", [18, 19])
def test_pristine_recovery_uses_existing_sdk_owner_and_preserves_core(tmp_path, version):
    path = tmp_path / "core.sqlite3"
    schema(path, version)
    sidecar = SourcePolicyProjection(path, USER).path
    sidecar.write_bytes(b"damaged pristine")
    before = path.read_bytes()
    result = recover_source_policy(path, USER)
    assert result["state"] == "recovered" and result["change_seq"] == 0
    assert path.read_bytes() == before
    assert (Path(result["backup_directory"]) / sidecar.name).read_bytes() == b"damaged pristine"
    loaded = SourcePolicyProjection(path, USER)
    loaded.load()
    assert loaded.ready and loaded._bound and loaded.value["version"] == 2


@pytest.mark.parametrize("version", [18, 19])
def test_recovery_keeps_exclusive_owner_requirement_without_sdk(tmp_path, monkeypatch, version):
    path = tmp_path / "core.sqlite3"
    schema(path, version)
    real_import = builtins.__import__
    def denied(name, *args, **kwargs):
        if name == "eidema":
            raise ImportError("synthetic unavailable recovery authority")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", denied)
    before = path.read_bytes()
    with pytest.raises(ImportError, match="recovery authority"):
        recover_source_policy(path, USER)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.source-policy.sqlite3"))


@pytest.mark.parametrize("version", [18, 19])
def test_real_sdk_raw_output_revocation_survives_fallback(tmp_path, version):
    path = tmp_path / "core.sqlite3"
    store = eidema.MemoryStore(path)
    access, scope = eidema.AccessScope(USER, "kurisu"), eidema.MemoryScope("character", USER, "kurisu")
    source = eidema.SourceSpan(SOURCE["namespace"], SOURCE["record_key"], 1)
    output = eidema.SourceSpan(**OUTPUT)
    maintenance = eidema.Maintenance(store)
    try:
        maintenance.register_stream(access, "synthetic-stream", scope)
        maintenance.append_changes(access, "synthetic-stream", [eidema.SourceChange(
            1, source, "upsert", False, "policy-1", lookup_allowed=True)])
        item = maintenance.lookup_input(access, "synthetic-stream", source)
        reads = store.capture_read_set(access, sources=(item,), now_ms=1)
        store.register_output_use(access, reads, source=output, now_ms=1)
        maintenance.append_changes(access, "synthetic-stream", [eidema.SourceChange(
            2, source, "withdraw", False, "policy-2")])
        expected = [asdict(retired) for retired in store.source_policy(access).retirements]
        assert expected and expected[0]["source"] == OUTPUT
    finally:
        store.close()
    if version == 18:
        downgrade18(path)
    projection = SourcePolicyProjection(path, USER)
    projection.bootstrap_v1()
    assert projection.value["retirements"] == expected
    assert OUTPUT in projection.value["derived_sources"]
    projection._publish(projection.value)
    reload = SourcePolicyProjection(path, USER)
    reload.load()
    assert reload.value == projection.value


@pytest.mark.parametrize("reason", ["authorization_revoked", "source_changed", "unknown_future_reason"])
def test_sdk_lifecycle_slugs_remain_masking_metadata(tmp_path, reason):
    projection = SourcePolicyProjection(tmp_path / "core.sqlite3", USER)
    projection._publish({**projection.value, "retirements": [{"source": OUTPUT, "reason": reason}]})
    projection.load()
    assert projection.value["retirements"][0]["reason"] == reason


@pytest.mark.parametrize("reason", ["", "Bad", "contains spaces", "x" * 129, None])
def test_malformed_lifecycle_reason_is_not_binding_evidence(tmp_path, reason):
    projection = SourcePolicyProjection(tmp_path / "core.sqlite3", USER)
    projection._publish({**projection.value, "retirements": [{"source": OUTPUT, "reason": reason}]})
    reader = SourcePolicyProjection(projection.db_path, USER)
    with pytest.raises(ValueError, match="invalid_source_policy"):
        reader.load()
    assert not reader._bound and not reader.ready
