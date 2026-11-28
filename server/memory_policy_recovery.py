"""Explicit offline rebuild of a derived policy, never repair of core authority."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4

from config.durable_io import write_bytes, write_text
from core import session_source_journal as journal
from server.memory_source_policy import SourcePolicyProjection, SourcePolicyBindingMismatch
from server import memory_policy_metadata


class PolicyRecoveryError(ValueError):
    pass


def _check_core_integrity(db):
    findings = db.execute("PRAGMA quick_check").fetchall()
    if findings == [("ok",)]:
        return
    # SQLite 3.44 through 3.46.0 can report a malformed inverted index after
    # an ordinary FTS5 secure-delete. The sidecar is rebuilt from relational
    # authority. Require the full page check to report no other problem.
    # https://www.sqlite.org/releaselog/3_46_1.html
    legacy_fts_findings = {
        ("malformed inverted index for FTS5 table main.memory_fts",),
        ("malformed inverted index for FTS5 table main.retrieval_fts",),
    }
    if ((3, 44, 0) <= sqlite3.sqlite_version_info < (3, 46, 1)
            and findings and all(row in legacy_fts_findings for row in findings)):
        return
    raise PolicyRecoveryError("core_unreadable")


def _pristine_v1_core(db):
    """Prove no prior durable work, including records without a user column."""
    states = db.execute("SELECT singleton,schema_version,change_seq,cleanup_pending FROM store_state").fetchall()
    if len(states) == 1 and states[0][1] in memory_policy_metadata.SUPPORTED_SCHEMA_VERSIONS:
        return memory_policy_metadata.is_pristine_core(db)
    if states not in ([(1, 1, 0, 0)], [(1, 2, 0, 0)]):
        return False
    # FTS reads its external content from search_documents; its SQLite-owned
    # shadow tables contain initialization records even in a pristine store.
    tables = ["memories", "receipts", "origins", "operations", "forgotten",
        "exclusions", "search_documents", "embeddings", "memory_fts"]
    if states[0][1] == 2:
        tables.append("recall_uses")
    return all(db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is None for table in tables)


def recover_source_policy(db_path, user_id, *, api=None):
    """Rebuild under core's exclusive owner after verified identity and integrity.

    A damaged sidecar cannot prove its former binding. Therefore the readable
    core must contain exactly one nonempty user namespace, equal to the explicit
    configured identity, or be provably pristine schema-v1 state without any
    known sidecar policy. Other unknown or mixed namespaces require recovery
    from matching evidence.
    """
    if not isinstance(user_id, str) or not user_id.strip():
        raise PolicyRecoveryError("identity_required")
    path = Path(db_path).resolve()
    if not path.is_file():
        raise PolicyRecoveryError("core_missing")
    if api is None:
        import eidema as api
    projection = SourcePolicyProjection(path, user_id)
    with api.source_policy_recovery_owner(path):
        try:
            projection.load()
        except SourcePolicyBindingMismatch:
            raise PolicyRecoveryError("identity_mismatch") from None
        except (ValueError, OSError, sqlite3.Error):
            pass  # Authority is checked below; damaged evidence is preserved.
        known_policy = any(projection.value[key]
            for key in ("exclusions", "retirements", "pending", "derived_sources"))
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.1)) as db:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                _check_core_integrity(db)
                projection.bootstrap_v1()
                schema_version = db.execute("SELECT schema_version FROM store_state WHERE singleton=1").fetchone()[0]
                namespaces = projection._core_namespaces(db, schema_version)
                if namespaces:
                    if namespaces != {user_id}:
                        raise PolicyRecoveryError("identity_mismatch")
                elif not _pristine_v1_core(db) or known_policy:
                    raise PolicyRecoveryError("identity_unverifiable")
            # Validate Host coordinates as well as the generic core schema.
            for source in [*projection.value["exclusions"],
                    *(item["source"] for item in projection.value["retirements"]),
                    *projection.value["derived_sources"]]:
                if source["namespace"] in (journal.V1, journal.V2):
                    try:
                        journal.decode_record_key(source["namespace"], source["record_key"])
                    except (TypeError, ValueError):
                        raise PolicyRecoveryError("invalid_coordinates") from None
        except PolicyRecoveryError:
            raise
        except SourcePolicyBindingMismatch:
            raise PolicyRecoveryError("identity_mismatch") from None
        except (ValueError, OSError, sqlite3.Error) as exc:
            raise PolicyRecoveryError("store_rollback" if str(exc) == "source_policy_store_rollback"
                else "core_unreadable") from None

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(path.name + ".policy-recovery-" + stamp + "-" + uuid4().hex[:8])
        backup.mkdir()
        originals = []
        for base in (path, projection.path):
            for suffix in ("", "-wal", "-shm", "-journal"):
                original = base.with_name(base.name + suffix)
                if original.is_file():
                    copy = backup / original.name
                    shutil.copy2(original, copy)
                    originals.append({"name":copy.name,
                        "sha256":hashlib.sha256(copy.read_bytes()).hexdigest()})
        write_text(backup / "manifest.json",json.dumps({"version":1,
            "user_key":projection.user_key, "originals":originals}, indent=2))
        # The same exclusive owner proves no prepared mutation remains live.
        # Every completed exclusion is in the verified atomic core snapshot.
        value = {**projection.value, "pending":[]}
        temporary = projection.path.with_name(projection.path.name + ".rebuild-" + uuid4().hex)
        target = projection.path
        projection.path = temporary
        try:
            projection._publish(value)
            # Any old SQLite auxiliary file belongs to the preserved original.
            # Remove it before replacement so a crash cannot replay an old
            # journal over the new database. Copies are already preserved.
            for suffix in ("-wal", "-shm", "-journal"):
                auxiliary = target.with_name(target.name + suffix)
                if auxiliary.exists():
                    auxiliary.unlink()
            # _publish closed its SQLite connection. Publish those complete
            # bytes through the shared atomic/durable commit boundary.
            write_bytes(target,temporary.read_bytes())
        finally:
            if temporary.exists():
                temporary.unlink()
        return {"state":"recovered", "backup_directory":str(backup),
            "change_seq":value["change_seq"], "exclusions":len(value["exclusions"]),
            "retirements":len(value["retirements"])}
