"""Host acceptance/outbox infrastructure shared by Control effect domains.

The Host supplies source/root/cohort identity and grounded effects. Cooperative
Provider execution uses the provider kind; other live domains retain their
explicit rollout boundaries. This store owns acceptance and receipts, not
semantic routing, domain execution, permission or external exactly-once guarantees.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, Literal, Mapping
import uuid


class ControlLedgerConflict(RuntimeError):
    """An identity, fence, claim, or immutable receipt did not match."""


@dataclass(frozen=True)
class ControlEffect:
    effect_id: str
    kind: Literal["focus", "attention", "work", "provider"]
    # Exact Host conflict-resource identity, not an inferred semantic goal.
    target_key: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class ReconciliationPolicy:
    owner: str
    max_probes: int = 3
    interval_seconds: float = 5.0
    ttl_seconds: float = 60.0


_SCHEMA_VERSION = 3
_CONSTRUCTION_LOCK = threading.Lock()


_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS control_ledger_meta (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    version INTEGER NOT NULL,
    accepting INTEGER NOT NULL CHECK(accepting IN (0,1))
);
INSERT OR IGNORE INTO control_ledger_meta VALUES (1,3,1);
CREATE TABLE IF NOT EXISTS control_epoch_fences (
    fence_scope TEXT PRIMARY KEY,
    chat_epoch INTEGER NOT NULL,
    root_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS control_admissions (
    root_id TEXT PRIMARY KEY,
    source_scope TEXT NOT NULL,
    fence_scope TEXT NOT NULL,
    utterance_id TEXT NOT NULL,
    chat_epoch INTEGER NOT NULL,
    authority_mode TEXT NOT NULL CHECK(authority_mode IN ('legacy','turn_decision')),
    transcript_hash TEXT NOT NULL,
    lifecycle TEXT NOT NULL CHECK(lifecycle IN ('current','superseded','discarded')),
    plan_id TEXT UNIQUE,
    plan_json TEXT,
    accepted_at REAL,
    created_at REAL NOT NULL,
    UNIQUE(source_scope, utterance_id),
    UNIQUE(fence_scope, chat_epoch)
);
CREATE TABLE IF NOT EXISTS control_effect_outbox (
    effect_id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL REFERENCES control_admissions(root_id),
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('focus','attention','work','provider')),
    target_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'pending','dispatching','running','terminal','cancelled',
        'unknown_reconciling','needs_user_decision'
    )),
    claim_token TEXT NOT NULL DEFAULT '',
    claim_owner TEXT NOT NULL DEFAULT '',
    claim_expires_at REAL,
    external_id TEXT NOT NULL DEFAULT '',
    probe_owner TEXT NOT NULL DEFAULT '',
    max_probes INTEGER NOT NULL DEFAULT 0,
    probe_count INTEGER NOT NULL DEFAULT 0,
    probe_interval REAL NOT NULL DEFAULT 0,
    unknown_ttl REAL NOT NULL DEFAULT 0,
    next_probe_at REAL,
    unknown_expires_at REAL,
    reason TEXT NOT NULL DEFAULT '',
    UNIQUE(root_id, ordinal)
);
CREATE UNIQUE INDEX IF NOT EXISTS control_one_unconfirmed_target
    ON control_effect_outbox(kind,target_key)
    WHERE state IN ('dispatching','unknown_reconciling','needs_user_decision');
CREATE UNIQUE INDEX IF NOT EXISTS control_external_identity
    ON control_effect_outbox(kind,external_id) WHERE external_id<>'';
CREATE TABLE IF NOT EXISTS control_effect_receipts (
    effect_id TEXT PRIMARY KEY REFERENCES control_effect_outbox(effect_id),
    receipt_json TEXT NOT NULL,
    recorded_at REAL NOT NULL
);
COMMIT;
"""


_MIGRATE_V1_TO_V2 = (
    """CREATE TABLE control_effect_outbox_v2 (
    effect_id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL REFERENCES control_admissions(root_id),
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('focus','attention','work')),
    target_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'pending','dispatching','running','terminal','cancelled',
        'unknown_reconciling','needs_user_decision'
    )),
    claim_token TEXT NOT NULL DEFAULT '',
    claim_owner TEXT NOT NULL DEFAULT '',
    claim_expires_at REAL,
    external_id TEXT NOT NULL DEFAULT '',
    probe_owner TEXT NOT NULL DEFAULT '',
    max_probes INTEGER NOT NULL DEFAULT 0,
    probe_count INTEGER NOT NULL DEFAULT 0,
    probe_interval REAL NOT NULL DEFAULT 0,
    unknown_ttl REAL NOT NULL DEFAULT 0,
    next_probe_at REAL,
    unknown_expires_at REAL,
    reason TEXT NOT NULL DEFAULT '',
    UNIQUE(root_id, ordinal)
)""",
    "INSERT INTO control_effect_outbox_v2 SELECT * FROM control_effect_outbox",
    "DROP TABLE control_effect_outbox",
    "ALTER TABLE control_effect_outbox_v2 RENAME TO control_effect_outbox",
    """CREATE UNIQUE INDEX control_one_unconfirmed_target
    ON control_effect_outbox(kind,target_key)
    WHERE state IN ('dispatching','unknown_reconciling','needs_user_decision')""",
    """CREATE UNIQUE INDEX control_external_identity
    ON control_effect_outbox(kind,external_id) WHERE external_id<>''""",
    "UPDATE control_ledger_meta SET version=2 WHERE singleton=1 AND version=1",
)


_MIGRATE_V2_TO_V3 = (
    """CREATE TABLE control_effect_outbox_v3 (
    effect_id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL REFERENCES control_admissions(root_id),
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('focus','attention','work','provider')),
    target_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'pending','dispatching','running','terminal','cancelled',
        'unknown_reconciling','needs_user_decision'
    )),
    claim_token TEXT NOT NULL DEFAULT '',
    claim_owner TEXT NOT NULL DEFAULT '',
    claim_expires_at REAL,
    external_id TEXT NOT NULL DEFAULT '',
    probe_owner TEXT NOT NULL DEFAULT '',
    max_probes INTEGER NOT NULL DEFAULT 0,
    probe_count INTEGER NOT NULL DEFAULT 0,
    probe_interval REAL NOT NULL DEFAULT 0,
    unknown_ttl REAL NOT NULL DEFAULT 0,
    next_probe_at REAL,
    unknown_expires_at REAL,
    reason TEXT NOT NULL DEFAULT '',
    UNIQUE(root_id, ordinal)
)""",
    "INSERT INTO control_effect_outbox_v3 SELECT * FROM control_effect_outbox",
    "DROP TABLE control_effect_outbox",
    "ALTER TABLE control_effect_outbox_v3 RENAME TO control_effect_outbox",
    """CREATE UNIQUE INDEX control_one_unconfirmed_target
    ON control_effect_outbox(kind,target_key)
    WHERE state IN ('dispatching','unknown_reconciling','needs_user_decision')""",
    """CREATE UNIQUE INDEX control_external_identity
    ON control_effect_outbox(kind,external_id) WHERE external_id<>''""",
    "UPDATE control_ledger_meta SET version=3 WHERE singleton=1 AND version=2",
)


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _required(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a nonempty Host identity is required")
    return value


def _positive(value: float) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError("a finite positive duration is required")
    return value


def _enable_wal(db: sqlite3.Connection, *, timeout_seconds: float = 5.0) -> None:
    """Converge concurrent constructors on WAL without changing authority state."""

    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    while True:
        try:
            row = db.execute("PRAGMA journal_mode=WAL").fetchone()
        except sqlite3.OperationalError as exc:
            reason = str(exc).lower()
            if (
                "locked" not in reason
                and "busy" not in reason
            ) or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
            continue
        if row is None or str(row[0]).lower() != "wal":
            raise ControlLedgerConflict("Control Ledger could not enable SQLite WAL")
        return


def _epoch(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("a Host chat_epoch is required")
    return value


class ControlLedgerStore:
    """Explicit-path SQLite store; construction alone never runs an effect."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        try:
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA busy_timeout=5000")
            # SQLite's journal-mode transition may reject concurrent fresh
            # constructors before BEGIN IMMEDIATE/busy_timeout can serialize
            # them.  The process lock removes that avoidable local race; the
            # bounded WAL convergence also covers another Host process.
            with _CONSTRUCTION_LOCK:
                meta = self._db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='control_ledger_meta'"
                ).fetchone()
                version = None
                if meta is not None:
                    try:
                        rows = self._db.execute(
                            "SELECT version FROM control_ledger_meta"
                        ).fetchall()
                    except sqlite3.DatabaseError as exc:
                        raise ControlLedgerConflict(
                            "unsupported Control Ledger schema"
                        ) from exc
                    if len(rows) != 1 or rows[0][0] not in {1, 2, _SCHEMA_VERSION}:
                        raise ControlLedgerConflict("unsupported Control Ledger schema")
                    version = int(rows[0][0])
                _enable_wal(self._db)
                self._db.execute("PRAGMA synchronous=FULL")
                if version == 1:
                    self._upgrade_v1_to_v2()
                    version = 2
                if version == 2:
                    self._upgrade_v2_to_v3()
                else:
                    self._db.executescript(_SCHEMA)
        except BaseException:
            self._db.close()
            raise

    def _upgrade_v1_to_v2(self) -> None:
        """Widen the effect algebra under one restart/concurrency-safe lock."""

        # Rebuilding a table referenced by the receipt table requires
        # foreign-key enforcement to be suspended for this one transaction.
        # The original table name is restored before commit and the complete
        # graph is checked before use.
        self._db.execute("PRAGMA foreign_keys=OFF")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT version FROM control_ledger_meta WHERE singleton=1"
            ).fetchone()
            if row is None or row[0] not in {1, 2, _SCHEMA_VERSION}:
                raise ControlLedgerConflict("unsupported Control Ledger schema")
            if int(row[0]) == 1:
                for statement in _MIGRATE_V1_TO_V2:
                    self._db.execute(statement)
                if self._db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ControlLedgerConflict(
                        "Control Ledger migration broke a foreign key"
                    )
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise
        finally:
            self._db.execute("PRAGMA foreign_keys=ON")

    def _upgrade_v2_to_v3(self) -> None:
        """Add Provider effects without changing existing effect or receipt identity."""

        self._db.execute("PRAGMA foreign_keys=OFF")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT version FROM control_ledger_meta WHERE singleton=1"
            ).fetchone()
            if row is None or row[0] not in {2, _SCHEMA_VERSION}:
                raise ControlLedgerConflict("unsupported Control Ledger schema")
            if int(row[0]) == 2:
                for statement in _MIGRATE_V2_TO_V3:
                    self._db.execute(statement)
                if self._db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ControlLedgerConflict(
                        "Control Ledger migration broke a foreign key"
                    )
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise
        finally:
            self._db.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.commit()
            except sqlite3.IntegrityError as exc:
                self._db.rollback()
                raise ControlLedgerConflict(str(exc)) from exc
            except BaseException:
                self._db.rollback()
                raise

    @staticmethod
    def _row(db, table, key, identity):
        # Table/key are fixed internal call-site constants, never model input.
        row = db.execute(f"SELECT * FROM {table} WHERE {key}=?", (identity,)).fetchone()
        if row is None:
            raise ControlLedgerConflict(f"unknown {table} identity")
        return dict(row)

    @staticmethod
    def _current(db, admission):
        fence = db.execute(
            "SELECT * FROM control_epoch_fences WHERE fence_scope=?", (admission["fence_scope"],)
        ).fetchone()
        return (
            admission["lifecycle"] == "current"
            and fence is not None
            and fence["root_id"] == admission["root_id"]
            and fence["chat_epoch"] == admission["chat_epoch"]
        )

    @staticmethod
    def _unknown(db, effect_id, now, reason):
        db.execute(
            """UPDATE control_effect_outbox SET state='unknown_reconciling',
                   next_probe_at=?, unknown_expires_at=MIN(unknown_expires_at,?+unknown_ttl), reason=?
                   WHERE effect_id=? AND state='dispatching'""",
            (now, now, reason, effect_id),
        )

    def _retire(self, db, root_id, lifecycle, now):
        db.execute(
            "UPDATE control_admissions SET lifecycle=? WHERE root_id=? AND lifecycle='current'",
            (lifecycle, root_id),
        )
        db.execute(
            "UPDATE control_effect_outbox SET state='cancelled',reason=? WHERE root_id=? AND state='pending'",
            (lifecycle, root_id),
        )
        for row in db.execute(
            "SELECT effect_id FROM control_effect_outbox WHERE root_id=? AND state='dispatching'",
            (root_id,),
        ).fetchall():
            self._unknown(db, row["effect_id"], now, lifecycle)
        # Known running effects belong to their domain. No implicit cancellation
        # or rewriting of a prior run/terminal receipt occurs here.

    def admit(
        self,
        *,
        root_id: str,
        source_scope: str,
        fence_scope: str,
        utterance_id: str,
        chat_epoch: int,
        authority_mode: Literal["legacy", "turn_decision"],
        transcript_hash: str,
    ):
        """Bind an already-issued Host epoch; unknown is not permission."""
        _epoch(chat_epoch)
        admission, _replayed = self._admit(
            root_id=root_id, source_scope=source_scope, fence_scope=fence_scope,
            utterance_id=utterance_id, chat_epoch=chat_epoch,
            authority_mode=authority_mode, transcript_hash=transcript_hash,
            minimum_epoch=0,
        )
        return admission

    def open_admission(
        self,
        *,
        root_id: str,
        source_scope: str,
        fence_scope: str,
        utterance_id: str,
        authority_mode: Literal["legacy", "turn_decision"],
        transcript_hash: str,
        minimum_epoch: int = 1,
    ):
        """Explicit durable issuance: replay lookup, epoch and source commit together.

        Only a new source advances the fence. The floor carries an existing
        Host cache forward; it cannot alter an earlier source's epoch/mode.
        This grants ingress identity, not a semantic plan or a domain effect.
        """
        admission, replayed = self._admit(
            root_id=root_id, source_scope=source_scope, fence_scope=fence_scope,
            utterance_id=utterance_id, chat_epoch=None,
            authority_mode=authority_mode, transcript_hash=transcript_hash,
            minimum_epoch=minimum_epoch,
        )
        return {"admission": admission, "replayed": replayed}

    def _admit(
        self,
        *,
        root_id: str,
        source_scope: str,
        fence_scope: str,
        utterance_id: str,
        chat_epoch: int | None,
        authority_mode: Literal["legacy", "turn_decision"],
        transcript_hash: str,
        minimum_epoch: int,
    ):
        _epoch(minimum_epoch)
        for value in (root_id, source_scope, fence_scope, utterance_id, transcript_hash):
            _required(value)
        if authority_mode not in {"legacy", "turn_decision"}:
            raise ValueError("unsupported authority mode")
        with self._transaction() as db:
            existing = db.execute(
                "SELECT * FROM control_admissions WHERE source_scope=? AND utterance_id=?",
                (source_scope, utterance_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["root_id"],
                    existing["fence_scope"],
                    existing["authority_mode"],
                    existing["transcript_hash"],
                ) != (root_id, fence_scope, authority_mode, transcript_hash):
                    raise ControlLedgerConflict("admission identity/mode/transcript changed")
                # A transport replay keeps the original epoch; it cannot move
                # the source fence forward or revive a superseded turn.
                return dict(existing), True
            if (
                authority_mode == "turn_decision"
                and not db.execute("SELECT accepting FROM control_ledger_meta").fetchone()[0]
            ):
                raise ControlLedgerConflict("TurnDecision admission is paused")
            previous = db.execute(
                "SELECT * FROM control_epoch_fences WHERE fence_scope=?", (fence_scope,)
            ).fetchone()
            if chat_epoch is None:
                chat_epoch = max(minimum_epoch, (previous["chat_epoch"] if previous else 0) + 1)
            if previous is not None and chat_epoch <= previous["chat_epoch"]:
                raise ControlLedgerConflict("chat_epoch did not advance")
            now = self._clock()
            if previous is not None:
                self._retire(db, previous["root_id"], "superseded", now)
            db.execute(
                """INSERT INTO control_admissions
                       (root_id,source_scope,fence_scope,utterance_id,chat_epoch,authority_mode,transcript_hash,lifecycle,created_at)
                       VALUES (?,?,?,?,?,?,?,'current',?)""",
                (
                    root_id,
                    source_scope,
                    fence_scope,
                    utterance_id,
                    chat_epoch,
                    authority_mode,
                    transcript_hash,
                    now,
                ),
            )
            db.execute(
                """INSERT INTO control_epoch_fences VALUES (?,?,?) ON CONFLICT(fence_scope)
                       DO UPDATE SET chat_epoch=excluded.chat_epoch,root_id=excluded.root_id""",
                (fence_scope, chat_epoch, root_id),
            )
            return self._row(db, "control_admissions", "root_id", root_id), False

    def advance_epoch(
        self,
        *,
        fence_scope: str,
        expected_epoch: int,
        minimum_epoch: int = 0,
    ):
        """CAS a non-input invalidation without inventing a root or reviving one.

        Keep the last real root as a tombstone. Its original admission epoch
        stays immutable; the fence watermark may now be higher. A late event
        carrying an older watermark must not retire the replacement root.
        """
        _required(fence_scope)
        _epoch(expected_epoch)
        _epoch(minimum_epoch)
        with self._transaction() as db:
            fence = self._row(db, "control_epoch_fences", "fence_scope", fence_scope)
            if fence["chat_epoch"] != expected_epoch:
                raise ControlLedgerConflict("control epoch fence changed")
            admission = self._row(db, "control_admissions", "root_id", fence["root_id"])
            if admission["fence_scope"] != fence_scope:
                raise ControlLedgerConflict("control epoch fence belongs to another owner")
            issued = max(expected_epoch + 1, minimum_epoch)
            self._retire(db, fence["root_id"], "discarded", self._clock())
            db.execute(
                "UPDATE control_epoch_fences SET chat_epoch=? WHERE fence_scope=?",
                (issued, fence_scope),
            )
            return self._row(db, "control_epoch_fences", "fence_scope", fence_scope)

    def discard(self, root_id: str) -> None:
        with self._transaction() as db:
            self._row(db, "control_admissions", "root_id", root_id)
            self._retire(db, root_id, "discarded", self._clock())

    def accept(
        self,
        root_id: str,
        *,
        chat_epoch: int,
        plan_id: str,
        effects: tuple[ControlEffect, ...],
        evidence: Mapping[str, Any],
        local_apply: Callable[[sqlite3.Cursor], Mapping[str, Any]] | None = None,
    ):
        _required(plan_id)
        shaped = []
        for effect in effects:
            _required(effect.effect_id)
            _required(effect.target_key)
            if effect.kind not in {"focus", "attention", "work", "provider"}:
                raise ValueError("unsupported Control effect kind")
            shaped.append(
                {
                    "effect_id": effect.effect_id,
                    "kind": effect.kind,
                    "target_key": effect.target_key,
                    "payload": dict(effect.payload),
                }
            )
        if len({item["effect_id"] for item in shaped}) != len(shaped):
            raise ControlLedgerConflict("duplicate effect identity within plan")
        encoded = _json({"effects": shaped, "evidence": dict(evidence)})
        # One canonical snapshot owns both the decision and its outbox. A frozen
        # dataclass does not freeze a caller's nested payload mapping while we
        # wait for the transaction lock.
        frozen_effects = json.loads(encoded)["effects"]
        with self._transaction() as db:
            admission = self._row(db, "control_admissions", "root_id", root_id)
            if admission["plan_id"] is not None:
                if admission["plan_id"] != plan_id or admission["plan_json"] != encoded:
                    raise ControlLedgerConflict("accepted decision is immutable")
                return {
                    "plan_id": plan_id,
                    "replayed": True,
                    "effect_count": len(frozen_effects),
                    "disposition": "effects_accepted" if frozen_effects else "no_effect_accepted",
                }
            if (
                admission["authority_mode"] != "turn_decision"
                or admission["chat_epoch"] != chat_epoch
                or not self._current(db, admission)
                or not db.execute("SELECT accepting FROM control_ledger_meta").fetchone()[0]
            ):
                raise ControlLedgerConflict("acceptance fence/mode is not current")
            for item in frozen_effects:
                blocked = db.execute(
                    """SELECT 1 FROM control_effect_outbox WHERE kind=? AND target_key=?
                                     AND state IN ('unknown_reconciling','needs_user_decision')""",
                    (item["kind"], item["target_key"]),
                ).fetchone()
                if blocked:
                    raise ControlLedgerConflict(
                        "unresolved same-target effect forbids sibling acceptance"
                    )
            db.execute(
                "UPDATE control_admissions SET plan_id=?,plan_json=?,accepted_at=? WHERE root_id=?",
                (plan_id, encoded, self._clock(), root_id),
            )
            for ordinal, item in enumerate(frozen_effects):
                db.execute(
                    """INSERT INTO control_effect_outbox
                           (effect_id,root_id,ordinal,kind,target_key,payload_json,state)
                           VALUES (?,?,?,?,?,?,'pending')""",
                    (
                        item["effect_id"],
                        root_id,
                        ordinal,
                        item["kind"],
                        item["target_key"],
                        _json(item["payload"]),
                    ),
                )
            local = {}
            if local_apply is not None:
                # The existing domain transaction seam also covers local input
                # receipts at acceptance. External I/O stays after this commit.
                with self._domain_cursor(db) as cursor:
                    local = dict(local_apply(cursor))
            return {
                "plan_id": plan_id,
                "replayed": False,
                "effect_count": len(frozen_effects),
                "disposition": "effects_accepted" if frozen_effects else "no_effect_accepted",
                **({"local":local} if local_apply is not None else {}),
            }

    @staticmethod
    @contextmanager
    def _domain_cursor(db):
        """Keep trusted local domain SQL inside its caller's transaction."""
        cursor = db.cursor()
        db.set_authorizer(
            lambda action, *_args: (
                sqlite3.SQLITE_DENY
                if action in {sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT}
                else sqlite3.SQLITE_OK
            )
        )
        try:
            yield cursor
        finally:
            db.set_authorizer(None)
            cursor.close()

    def apply_legacy_local(
        self,
        root_id: str,
        *,
        apply: Callable[[sqlite3.Cursor], Mapping[str, Any]],
    ):
        """Check the legacy fence and apply SQL without a check/write gap.

        This does not grant legacy replay idempotency or create a new-mode
        receipt. It is the integration seam for an existing local domain write;
        live dispatch still has to carry the root to this boundary.
        """
        with self._transaction() as db:
            admission = self._row(db, "control_admissions", "root_id", root_id)
            if admission["authority_mode"] != "legacy" or not self._current(db, admission):
                raise ControlLedgerConflict("legacy commit cannot bypass this admission")
            with self._domain_cursor(db) as cursor:
                return dict(apply(cursor))

    def claim(
        self,
        effect_id: str,
        *,
        owner: str,
        lease_seconds: float,
        reconciliation: ReconciliationPolicy,
    ):
        _required(owner)
        _required(reconciliation.owner)
        _positive(lease_seconds)
        _positive(reconciliation.interval_seconds)
        _positive(reconciliation.ttl_seconds)
        if (
            isinstance(reconciliation.max_probes, bool)
            or not isinstance(reconciliation.max_probes, int)
            or reconciliation.max_probes < 1
        ):
            raise ValueError("a bounded positive probe budget is required")
        with self._transaction() as db:
            effect = self._row(db, "control_effect_outbox", "effect_id", effect_id)
            admission = self._row(db, "control_admissions", "root_id", effect["root_id"])
            if (
                effect["state"] != "pending"
                or not self._current(db, admission)
                or not db.execute("SELECT accepting FROM control_ledger_meta").fetchone()[0]
            ):
                raise ControlLedgerConflict("outbox effect is not claimable")
            token = uuid.uuid4().hex
            now = self._clock()
            db.execute(
                """UPDATE control_effect_outbox SET state='dispatching',claim_token=?,claim_owner=?,
                       claim_expires_at=?,probe_owner=?,max_probes=?,probe_interval=?,unknown_ttl=?,unknown_expires_at=? WHERE effect_id=?""",
                (
                    token,
                    owner,
                    now + lease_seconds,
                    reconciliation.owner,
                    reconciliation.max_probes,
                    reconciliation.interval_seconds,
                    reconciliation.ttl_seconds,
                    now + lease_seconds + reconciliation.ttl_seconds,
                    effect_id,
                ),
            )
            return self._row(db, "control_effect_outbox", "effect_id", effect_id)

    def claim_with_local_intent(
        self,
        effect_id: str,
        *,
        owner: str,
        lease_seconds: float,
        reconciliation: ReconciliationPolicy,
        apply: Callable[[sqlite3.Cursor, Mapping[str, Any]], Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Persist one external claim and its local domain intent atomically.

        The trusted callback is SQL-only and receives the accepted, detached
        effect payload.  Unlike :meth:`apply_local`, this method deliberately
        writes no terminal receipt: the separately scheduled Provider still
        owns an unknown/running/terminal lifecycle.  Provider or filesystem
        I/O must happen only after this transaction returns.
        """

        _required(owner)
        _required(reconciliation.owner)
        _positive(lease_seconds)
        _positive(reconciliation.interval_seconds)
        _positive(reconciliation.ttl_seconds)
        if (
            isinstance(reconciliation.max_probes, bool)
            or not isinstance(reconciliation.max_probes, int)
            or reconciliation.max_probes < 1
        ):
            raise ValueError("a bounded positive probe budget is required")
        if not callable(apply):
            raise TypeError("a trusted local intent writer is required")
        with self._transaction() as db:
            effect = self._row(db, "control_effect_outbox", "effect_id", effect_id)
            admission = self._row(db, "control_admissions", "root_id", effect["root_id"])
            if (
                effect["state"] != "pending"
                or not self._current(db, admission)
                or not db.execute("SELECT accepting FROM control_ledger_meta").fetchone()[0]
            ):
                raise ControlLedgerConflict("outbox effect is not claimable")
            token = uuid.uuid4().hex
            now = self._clock()
            db.execute(
                """UPDATE control_effect_outbox SET state='dispatching',claim_token=?,claim_owner=?,
                       claim_expires_at=?,probe_owner=?,max_probes=?,probe_interval=?,unknown_ttl=?,unknown_expires_at=? WHERE effect_id=?""",
                (
                    token,
                    owner,
                    now + lease_seconds,
                    reconciliation.owner,
                    reconciliation.max_probes,
                    reconciliation.interval_seconds,
                    reconciliation.ttl_seconds,
                    now + lease_seconds + reconciliation.ttl_seconds,
                    effect_id,
                ),
            )
            with self._domain_cursor(db) as cursor:
                details = dict(apply(cursor, json.loads(effect["payload_json"])))
            return {
                "effect": self._row(db, "control_effect_outbox", "effect_id", effect_id),
                "details": details,
                "replayed": False,
            }

    def _claimed(self, db, effect_id, claim_token):
        row = self._row(db, "control_effect_outbox", "effect_id", effect_id)
        if not claim_token or row["claim_token"] != claim_token:
            raise ControlLedgerConflict("claim token does not own effect")
        return row

    def bind_external(self, effect_id: str, *, claim_token: str, external_id: str):
        _required(external_id)
        with self._transaction() as db:
            effect = self._claimed(db, effect_id, claim_token)
            if effect["external_id"] and effect["external_id"] != external_id:
                raise ControlLedgerConflict("external identity cannot change")
            if effect["state"] not in {
                "dispatching",
                "unknown_reconciling",
                "needs_user_decision",
                "running",
            }:
                raise ControlLedgerConflict("effect cannot acquire a running identity")
            db.execute(
                "UPDATE control_effect_outbox SET state='running',external_id=? WHERE effect_id=?",
                (external_id, effect_id),
            )

    def bind_external_with_local(
        self,
        effect_id: str,
        *,
        claim_token: str,
        external_id: str,
        apply: Callable[[sqlite3.Cursor, Mapping[str, Any]], Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Bind a submitted identity and its local domain checkpoint atomically.

        The callback is the same trusted SQL-only seam used by local intent
        claiming. External I/O must happen only after this transaction returns.
        """

        _required(external_id)
        if not callable(apply):
            raise TypeError("a trusted local external binding writer is required")
        with self._transaction() as db:
            effect = self._claimed(db, effect_id, claim_token)
            if effect["external_id"] and effect["external_id"] != external_id:
                raise ControlLedgerConflict("external identity cannot change")
            if effect["state"] not in {
                "dispatching",
                "unknown_reconciling",
                "needs_user_decision",
                "running",
            }:
                raise ControlLedgerConflict("effect cannot acquire a running identity")
            with self._domain_cursor(db) as cursor:
                details = dict(apply(cursor, json.loads(effect["payload_json"])))
            db.execute(
                "UPDATE control_effect_outbox SET state='running',external_id=? WHERE effect_id=?",
                (external_id, effect_id),
            )
            return {
                "effect": self._row(db, "control_effect_outbox", "effect_id", effect_id),
                "details": details,
            }

    def _receipt(self, db, effect_id, claim_token, external_id, outcome, details):
        if outcome not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("a terminal domain outcome is required")
        encoded = _json({"external_id": external_id, "outcome": outcome, "details": dict(details)})
        effect = self._claimed(db, effect_id, claim_token)
        existing = db.execute(
            "SELECT receipt_json FROM control_effect_receipts WHERE effect_id=?", (effect_id,)
        ).fetchone()
        if existing is not None:
            if existing[0] != encoded:
                raise ControlLedgerConflict("terminal receipt is immutable")
            return {"effect_id": effect_id, "replayed": True, "receipt": json.loads(existing[0])}
        if effect["state"] not in {
            "dispatching",
            "running",
            "unknown_reconciling",
            "needs_user_decision",
        }:
            raise ControlLedgerConflict("effect has no submitted outcome to receive")
        if effect["external_id"] and effect["external_id"] != external_id:
            raise ControlLedgerConflict("receipt belongs to another external identity")
        db.execute(
            "INSERT INTO control_effect_receipts VALUES (?,?,?)",
            (effect_id, encoded, self._clock()),
        )
        db.execute(
            "UPDATE control_effect_outbox SET state='terminal',external_id=? WHERE effect_id=?",
            (external_id, effect_id),
        )
        return {"effect_id": effect_id, "replayed": False, "receipt": json.loads(encoded)}

    def record_receipt(
        self,
        effect_id: str,
        *,
        claim_token: str,
        external_id: str,
        outcome: str,
        details: Mapping[str, Any],
    ):
        _required(external_id)
        with self._transaction() as db:
            return self._receipt(db, effect_id, claim_token, external_id, outcome, details)

    def record_receipt_with_local(
        self,
        effect_id: str,
        *,
        claim_token: str,
        external_id: str,
        outcome: str,
        details: Mapping[str, Any],
        apply: Callable[[sqlite3.Cursor, Mapping[str, Any]], Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Commit one terminal external receipt with its local checkpoint."""

        _required(external_id)
        if not callable(apply):
            raise TypeError("a trusted local terminal writer is required")
        with self._transaction() as db:
            effect = self._claimed(db, effect_id, claim_token)
            existing = db.execute(
                "SELECT receipt_json FROM control_effect_receipts WHERE effect_id=?", (effect_id,)
            ).fetchone()
            if existing is not None:
                expected = _json({
                    "external_id": external_id,
                    "outcome": outcome,
                    "details": dict(details),
                })
                if existing[0] != expected:
                    raise ControlLedgerConflict("terminal receipt is immutable")
                return {
                    "effect_id": effect_id,
                    "replayed": True,
                    "receipt": json.loads(existing[0]),
                }
            with self._domain_cursor(db) as cursor:
                local = dict(apply(cursor, json.loads(effect["payload_json"])))
            receipt = self._receipt(
                db, effect_id, claim_token, external_id, outcome, details
            )
            receipt["local"] = local
            return receipt

    def mark_unknown(
        self,
        effect_id: str,
        *,
        claim_token: str,
        external_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Record an ambiguous submission without authorizing a retry."""

        _required(external_id)
        with self._transaction() as db:
            effect = self._claimed(db, effect_id, claim_token)
            if effect["external_id"] and effect["external_id"] != external_id:
                raise ControlLedgerConflict("unknown outcome belongs to another external identity")
            if effect["state"] in {"unknown_reconciling", "needs_user_decision"}:
                if not effect["external_id"]:
                    db.execute(
                        "UPDATE control_effect_outbox SET external_id=? WHERE effect_id=?",
                        (external_id, effect_id),
                    )
                return self._row(db, "control_effect_outbox", "effect_id", effect_id)
            if effect["state"] != "dispatching":
                raise ControlLedgerConflict("only an unconfirmed dispatch can become unknown")
            db.execute(
                "UPDATE control_effect_outbox SET external_id=? WHERE effect_id=?",
                (external_id, effect_id),
            )
            self._unknown(db, effect_id, self._clock(), str(reason or "submission_unknown"))
            return self._row(db, "control_effect_outbox", "effect_id", effect_id)

    def apply_local(
        self,
        effect_id: str,
        *,
        owner: str,
        apply: Callable[[sqlite3.Cursor, Mapping[str, Any]], Mapping[str, Any]],
    ):
        """Domain SQL-only callback and receipt commit together; never external I/O.

        This is an offline integration seam, not a shipped Focus/Attention
        executor. The domain still validates identity/permission/state. An
        exception/process death rolls back claim, SQL writes and receipt. The
        outbox remains pending, so replay does not require an unknown outcome
        or a new effect. Externally claimed effects cannot use this SQL seam.
        Transaction control is denied while trusted domain code runs. This is
        not a sandbox for arbitrary Python or callbacks doing external I/O.
        """
        _required(owner)
        with self._transaction() as db:
            effect = self._row(db, "control_effect_outbox", "effect_id", effect_id)
            if effect["state"] == "terminal":
                receipt = self._row(db, "control_effect_receipts", "effect_id", effect_id)
                return {
                    "effect_id": effect_id,
                    "replayed": True,
                    "receipt": json.loads(receipt["receipt_json"]),
                }
            admission = self._row(db, "control_admissions", "root_id", effect["root_id"])
            if (
                effect["state"] != "pending"
                or not self._current(db, admission)
                or not db.execute("SELECT accepting FROM control_ledger_meta").fetchone()[0]
            ):
                raise ControlLedgerConflict("local apply fence is stale")
            claim_token = uuid.uuid4().hex
            db.execute(
                "UPDATE control_effect_outbox SET state='dispatching',claim_token=?,claim_owner=? WHERE effect_id=?",
                (claim_token, owner, effect_id),
            )
            with self._domain_cursor(db) as cursor:
                details = apply(cursor, json.loads(effect["payload_json"]))
            return self._receipt(
                db, effect_id, claim_token, "local:" + effect_id, "succeeded", details
            )

    def expire_claims(self):
        """Expired dispatch claims become unknown, never pending/re-dispatched."""
        with self._transaction() as db:
            now = self._clock()
            for row in db.execute(
                "SELECT effect_id FROM control_effect_outbox WHERE state='dispatching' AND claim_expires_at<=?",
                (now,),
            ).fetchall():
                self._unknown(db, row["effect_id"], now, "claim_expired")
            db.execute(
                """UPDATE control_effect_outbox SET state='needs_user_decision',reason='unknown_expired'
                       WHERE state='unknown_reconciling' AND unknown_expires_at<=?""",
                (now,),
            )

    def due_unknown(self, *, owner: str, limit: int = 64):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("a positive batch limit is required")
        with self._lock:
            return [
                dict(row)
                for row in self._db.execute(
                    """SELECT * FROM control_effect_outbox
                    WHERE state='unknown_reconciling' AND probe_owner=? AND next_probe_at<=? AND unknown_expires_at>?
                    AND probe_count<max_probes
                    ORDER BY next_probe_at,effect_id LIMIT ?""",
                    (owner, self._clock(), self._clock(), limit),
                ).fetchall()
            ]

    def claim_probe(self, effect_id: str, *, owner: str, expected_probe_at: float):
        """Consume one bounded probe attempt before any reconciliation I/O.

        due_unknown is only discovery. Concurrent/restarted workers must acquire
        this schedule CAS before querying the native domain. A crashed probe
        still consumes its attempt; no automatic submit is authorized.
        """
        with self._transaction() as db:
            effect = self._row(db, "control_effect_outbox", "effect_id", effect_id)
            now = self._clock()
            if (
                effect["state"] != "unknown_reconciling"
                or effect["probe_owner"] != owner
                or effect["next_probe_at"] != expected_probe_at
                or now < expected_probe_at
                or now >= effect["unknown_expires_at"]
                or effect["probe_count"] >= effect["max_probes"]
            ):
                raise ControlLedgerConflict("probe schedule/owner is stale")
            count = effect["probe_count"] + 1
            db.execute(
                "UPDATE control_effect_outbox SET probe_count=?,next_probe_at=? WHERE effect_id=?",
                (count, now + effect["probe_interval"], effect_id),
            )
            return {"effect_id": effect_id, "probe_attempt": count, "owner": owner}

    def note_probe_unresolved(self, effect_id: str, *, owner: str, probe_attempt: int):
        with self._transaction() as db:
            effect = self._row(db, "control_effect_outbox", "effect_id", effect_id)
            if (
                effect["state"] != "unknown_reconciling"
                or effect["probe_owner"] != owner
                or effect["probe_count"] != probe_attempt
                or probe_attempt < 1
            ):
                raise ControlLedgerConflict("probe result does not own the current attempt")
            state = (
                "needs_user_decision"
                if probe_attempt >= effect["max_probes"]
                or self._clock() >= effect["unknown_expires_at"]
                else "unknown_reconciling"
            )
            db.execute(
                "UPDATE control_effect_outbox SET state=?,reason='probe_unresolved' WHERE effect_id=?",
                (state, effect_id),
            )

    def quiesce(self) -> None:
        """Rollback boundary: stop new-mode admission/claims; retain old-mode tombstones."""
        with self._transaction() as db:
            db.execute("UPDATE control_ledger_meta SET accepting=0")
            db.execute(
                "UPDATE control_admissions SET lifecycle='superseded' WHERE authority_mode='turn_decision' AND lifecycle='current'"
            )
            db.execute(
                "UPDATE control_effect_outbox SET state='cancelled',reason='rollback' WHERE state='pending'"
            )
            for row in db.execute(
                "SELECT effect_id FROM control_effect_outbox WHERE state='dispatching'"
            ).fetchall():
                self._unknown(db, row["effect_id"], self._clock(), "rollback")

    def get_admission(self, root_id: str):
        with self._lock:
            return self._row(self._db, "control_admissions", "root_id", root_id)

    def find_admission(self, source_scope: str, utterance_id: str):
        """Read-only ingress preflight; open_admission rechecks under its transaction."""
        _required(source_scope)
        _required(utterance_id)
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM control_admissions WHERE source_scope=? AND utterance_id=?",
                (source_scope, utterance_id),
            ).fetchone()
            return dict(row) if row is not None else None

    def get_effect(self, effect_id: str):
        with self._lock:
            return self._row(self._db, "control_effect_outbox", "effect_id", effect_id)

    def get_receipt(self, effect_id: str):
        with self._lock:
            row = self._db.execute(
                "SELECT receipt_json FROM control_effect_receipts WHERE effect_id=?", (effect_id,)
            ).fetchone()
            return json.loads(row[0]) if row is not None else None

    def get_epoch_fence(self, fence_scope: str):
        """Return the durable watermark, not proof that its last root is current.

        Non-input invalidation retains a real root tombstone. Eligibility still
        requires the admission lifecycle, root and epoch join in _current().
        """
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM control_epoch_fences WHERE fence_scope=?", (fence_scope,)
            ).fetchone()
            return dict(row) if row is not None else None

    def pending_effects(self, *, limit: int = 64):
        """Restart discovery only; claim/apply still performs the atomic fence."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("a positive batch limit is required")
        with self._lock:
            return [
                dict(row)
                for row in self._db.execute(
                    """SELECT * FROM control_effect_outbox
                    WHERE state='pending' ORDER BY root_id,ordinal LIMIT ?""",
                    (limit,),
                ).fetchall()
            ]
