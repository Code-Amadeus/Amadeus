"""Durable, content-free projection of core source-use policy.

This file is not a memory store. Only authoritative policy snapshots and exact
prepared operation coordinates are accepted; it contains no remembered text.
"""
from __future__ import annotations

from dataclasses import asdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import re

from server import memory_policy_metadata


class SourcePolicyUnavailable(RuntimeError):
    """History requires recovery of lost source-policy evidence."""


class SourcePolicyBindingMismatch(ValueError):
    """One Host projection is bound to one configured user namespace."""


class SourcePolicyProjection:
    def __init__(self, db_path: Path, user_id: str):
        self.db_path = db_path
        self.path = db_path.with_name(db_path.name + ".source-policy.sqlite3")
        self.user_key = hashlib.sha256(user_id.encode()).hexdigest()
        self.user_id = user_id
        self.value = {"version":2, "user_key":self.user_key, "change_seq":0,
            "exclusions":[], "retirements":[], "pending":[], "derived_sources":[]}
        self.ready = not db_path.exists() and not self.path.exists()
        self._bound = False

    @staticmethod
    def _source(value):
        if (not isinstance(value, dict) or set(value) != {"namespace", "record_key", "revision", "start_cp", "end_cp"}
                or not isinstance(value["namespace"], str) or not isinstance(value["record_key"], str)
                or type(value["revision"]) is not int or value["revision"] < 1
                or any(point is not None and (type(point) is not int or point < 0)
                    for point in (value["start_cp"], value["end_cp"]))
                or (value["start_cp"] is None) != (value["end_cp"] is None)
                or (value["start_cp"] is not None and value["start_cp"] >= value["end_cp"])):
            raise ValueError("invalid_source_policy")
        return value

    @classmethod
    def _retirement(cls, value):
        # Lifecycle reasons are SDK-owned slugs. They only mask coordinates;
        # accepting a new reason never grants source or execution authority.
        if (not isinstance(value, dict) or set(value) != {"source", "reason"}
                or not isinstance(value["reason"], str)
                or re.fullmatch(r"[a-z][a-z0-9_]{0,127}", value["reason"]) is None):
            raise ValueError("invalid_source_policy")
        cls._source(value["source"])
        return value

    @staticmethod
    def _core_namespaces(db, schema_version):
        if schema_version in memory_policy_metadata.SUPPORTED_SCHEMA_VERSIONS:
            return memory_policy_metadata._owner_namespaces(db)
        tables = ["memories", "operations", "exclusions", "forgotten"]
        if schema_version == 2:
            tables.append("recall_uses")
        return frozenset(row[0] for row in db.execute(" UNION ".join(
            "SELECT user_id FROM " + table for table in tables)))

    def load(self):
        if not self.path.exists():
            return
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.1)) as db:
            row = db.execute("SELECT value FROM source_policy WHERE singleton=1").fetchone()
            if row is None:
                raise ValueError("invalid_source_policy")
            value = json.loads(row[0])
        if isinstance(value, dict) and value.get("version") == 1 and set(value) == set(self.value) - {"derived_sources"}:
            value = {**value, "version":2, "derived_sources":[]}
        if (isinstance(value, dict) and set(value) == set(self.value) and value.get("version") == 2
                and isinstance(value.get("user_key"), str) and len(value["user_key"]) == 64
                and value["user_key"] != self.user_key):
            self.ready = False
            self._bound = False
            raise SourcePolicyBindingMismatch("source_policy_user_binding_changed")
        if (not isinstance(value, dict) or set(value) != set(self.value) or value["version"] != 2
                or value["user_key"] != self.user_key or type(value["change_seq"]) is not int
                or any(not isinstance(value[key], list) for key in ("exclusions", "retirements", "pending", "derived_sources"))):
            raise ValueError("invalid_source_policy")
        for source in value["exclusions"]:
            self._source(source)
        for source in value["derived_sources"]:
            self._source(source)
            if source["start_cp"] is not None:
                raise ValueError("invalid_source_policy")
        for retired in value["retirements"]:
            self._retirement(retired)
        for pending in value["pending"]:
            if (set(pending) != {"operation_id", "item_id", "character_id", "project_id", "sources", "memory_ids"}
                    or any(not isinstance(pending[key], str) or not pending[key]
                        for key in ("operation_id", "item_id", "character_id"))
                    or pending["project_id"] is not None and not isinstance(pending["project_id"], str)
                    or not isinstance(pending["sources"], list)
                    or not isinstance(pending["memory_ids"], list)
                    or any(not isinstance(mid, str) or not mid for mid in pending["memory_ids"])):
                raise ValueError("invalid_source_policy")
            for source in pending["sources"]:
                self._source(source)
        self.value, self.ready = value, True
        self._bound = True

    def _publish(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One serialized Host owner, rollback journal and FULL durability. The
        # core mutation is submitted only after this prepare transaction commits.
        with closing(sqlite3.connect(self.path, timeout=.1)) as db, db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS source_policy(singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL)")
            db.execute("INSERT OR REPLACE INTO source_policy(singleton,value) VALUES(1,?)",
                (json.dumps(value, separators=(",", ":")),))
        self.value, self.ready = value, True
        self._bound = True

    def prepare(self, context, operation_id, item_id, sources, memory_ids):
        previous = next((item for item in self.value["pending"]
            if (item["operation_id"], item["item_id"]) == (operation_id, item_id)), None)
        pending = {"operation_id":operation_id, "item_id":item_id,
            "character_id":context["character_id"], "project_id":context["project_id"],
            "sources":self._union_sources(previous["sources"] if previous else [],
                [asdict(source) for source in sources]),
            "memory_ids":list(dict.fromkeys([*(previous["memory_ids"] if previous else []), *memory_ids]))}
        others = [item for item in self.value["pending"]
            if (item["operation_id"], item["item_id"]) != (operation_id, item_id)]
        self._publish({**self.value, "pending":[*others, pending]})

    def register_derived(self, source):
        """A content-free marker, never a substitute for core dependency checks."""
        value = self._source(asdict(source))
        if value["start_cp"] is not None:
            raise ValueError("invalid_source_policy")
        self._publish({**self.value, "derived_sources":self._union_sources(
            self.value["derived_sources"], [value])})

    @staticmethod
    def _union_sources(*groups):
        return list({(source["namespace"], source["record_key"], source["revision"],
            source["start_cp"], source["end_cp"]):source for group in groups for source in group}.values())

    def reconcile(self, snapshot, *, clear=()):
        keys = set(clear)
        self._publish({**self.value, "change_seq":snapshot.change_seq,
            # Forget coordinates are irreversible authorised operation facts;
            # a later empty/replaced core cannot revoke previously known ones.
            "exclusions":self._union_sources(self.value["exclusions"],
                [asdict(source) for source in snapshot.exclusions]),
            "retirements":[self._retirement(asdict(item)) for item in snapshot.retirements],
            "pending":[item for item in self.value["pending"]
                if (item["operation_id"], item["item_id"]) not in keys]})

    def bootstrap_v1(self):
        """Read supported published policy schemas without importing/opening core.

        SQLite mode=ro still observes committed WAL. No migration, cleanup,
        checkpoint, text, vector or operation payload is read or written.
        """
        # Constructor readiness proves only that both paths were absent. Once
        # a core exists, either a validated binding or this snapshot is needed.
        if not self._bound:
            self.ready = False
        with closing(sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.1)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                if self.value["change_seq"] or self.value["exclusions"] or self.value["retirements"] or self.value["pending"] or self.value["derived_sources"]:
                    raise ValueError("source_policy_store_rollback")
                self.ready = True
                return
            state = db.execute("SELECT schema_version,change_seq FROM store_state WHERE singleton=1").fetchone()
            if state is None or state[0] not in (1, 2, *memory_policy_metadata.SUPPORTED_SCHEMA_VERSIONS):
                raise ValueError("unsupported_source_policy_schema")
            if state[1] < self.value["change_seq"]:
                raise ValueError("source_policy_store_rollback")
            owners = None
            if state[0] in memory_policy_metadata.SUPPORTED_SCHEMA_VERSIONS:
                metadata = memory_policy_metadata.read_source_policy_metadata(db, self.user_id)
                rows, retired = metadata["exclusions"], metadata["retirements"]
                derived = [(*row, None, None) for row in metadata["outputs"]]
                owners = metadata["owner_namespaces"]
            else:
                rows = db.execute("SELECT DISTINCT namespace,record_key,revision,start_cp,end_cp FROM exclusions WHERE user_id=?", (self.user_id,)).fetchall()
                retired = db.execute("SELECT DISTINCT o.namespace,o.record_key,o.revision,o.start_cp,o.end_cp,m.status_reason FROM origins o JOIN memories m ON m.memory_id=o.memory_id WHERE m.user_id=? AND m.state IN ('superseded','retracted')", (self.user_id,)).fetchall()
                derived = []
                if state[0] == 2:
                    retired.extend(db.execute("SELECT DISTINCT u.namespace,u.record_key,u.revision,NULL,NULL,m.status_reason FROM recall_uses u JOIN memories m ON m.memory_id=u.memory_id WHERE u.user_id=? AND m.user_id=? AND m.state IN ('superseded','retracted')", (self.user_id, self.user_id)).fetchall())
                    derived = db.execute("SELECT DISTINCT namespace,record_key,revision,NULL,NULL FROM recall_uses WHERE user_id=?", (self.user_id,)).fetchall()
            fields = ("namespace", "record_key", "revision", "start_cp", "end_cp")
            excluded = [self._source(dict(zip(fields, row))) for row in rows]
            retirements = [self._retirement({"source":dict(zip(fields, row[:5])), "reason":row[5]}) for row in retired]
        # A still-running owner may be between prepare and commit. A read-only
        # bootstrap refresh never clears pending masks; only an acquired core
        # owner can resolve their stable outcomes after a restart.
        latest = SourcePolicyProjection(self.db_path, self.user_id)
        try:
            latest.load()
        except SourcePolicyBindingMismatch:
            self.ready = False
            self._bound = False
            raise
        except (OSError, ValueError, sqlite3.Error):
            pass
        # Only an already verified bound sidecar can disambiguate a mixed
        # schema 18/19 core during SDK-unavailable startup.
        if not (self._bound or latest._bound) and owners and owners != {self.user_id}:
            raise SourcePolicyBindingMismatch("source_policy_user_binding_changed")
        pending = {(item["operation_id"], item["item_id"]):item
            for item in [*self.value["pending"], *latest.value["pending"]]}
        value = {**self.value, "change_seq":max(state[1], latest.value["change_seq"]),
            "exclusions":self._union_sources(self.value["exclusions"], excluded, latest.value["exclusions"]),
            "retirements":latest.value["retirements"] if latest.value["change_seq"] > state[1] else retirements,
            "derived_sources":self._union_sources(self.value["derived_sources"], latest.value["derived_sources"],
                [self._source(dict(zip(fields, row))) for row in derived]),
            "pending":list(pending.values())}
        # A bootstrap reader has not acquired core's singular owner. Publish
        # only in memory here: writing an old read snapshot could overwrite a
        # live owner's freshly prepared pending mask in another process.
        # The acquired core owner persists the reconciled snapshot below.
        self.value, self.ready = value, True
