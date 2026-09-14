"""Host-owned cooperative context checkpoints in the existing Host database.

These rows preserve receiving identity and the latest execution uncertainty. They
are not WorkItems, a Control outbox, or evidence that a native action completed.
An unresolved checkpoint cannot authorize a replacement run after process loss.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import uuid

from agent_host.provider_types import (
    PreparedProviderRun,
    ProviderRunIntakeAuthority,
    ProviderRunIntakeReceipt,
    ProviderSessionHandle,
)
from server.provider_session_binding import ProviderSessionAttachment
from agent_host.provider_contract import ProviderRequirements
from agent_host.work_ledger_store import WorkLedgerStore
from server.control_ledger import ControlLedgerConflict, ControlLedgerStore
from server.turn_admission import admission_transcript_hash


class CooperativeContextStore:
    def __init__(self, ledger: ControlLedgerStore, session_id: str):
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("a cooperative Host Session is required")
        self.ledger, self.session_id = ledger, session_id
        with ledger._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS cooperative_contexts (
                session_id TEXT NOT NULL, context_id TEXT NOT NULL,
                label TEXT NOT NULL, provider TEXT NOT NULL, workspace TEXT NOT NULL,
                closed INTEGER NOT NULL CHECK(closed IN (0,1)),
                run_id TEXT NOT NULL, run_status TEXT NOT NULL CHECK(run_status IN
                    ('idle','dispatching','queued','running','done','error','cancelled','orphaned')),
                native_session TEXT NOT NULL, output TEXT NOT NULL,
                last_input_id TEXT NOT NULL, last_turn_id TEXT NOT NULL,
                work_item_id TEXT NOT NULL, run_effect_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                requirements TEXT NOT NULL,
                workspace_route TEXT NOT NULL,
                PRIMARY KEY(session_id,context_id))""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(cooperative_contexts)")}
            if "requirements" not in columns:
                # These older checkpoints came only from the fixed coding slice.
                # Freeze its old contract; do not infer new permission from today's manifest.
                legacy = ProviderRequirements(task_kind="general", workspace_access="write",
                    workspace_ownership="caller", ownership="managed", resume="attach")
                db.execute("ALTER TABLE cooperative_contexts ADD COLUMN requirements TEXT")
                db.execute("UPDATE cooperative_contexts SET requirements=?", (json.dumps(legacy.to_dict(), sort_keys=True),))
            if "last_turn_id" not in columns:
                db.execute("ALTER TABLE cooperative_contexts ADD COLUMN last_turn_id TEXT NOT NULL DEFAULT ''")
            if "work_item_id" not in columns:
                db.execute("ALTER TABLE cooperative_contexts ADD COLUMN work_item_id TEXT NOT NULL DEFAULT ''")
            if "run_effect_id" not in columns:
                db.execute("ALTER TABLE cooperative_contexts ADD COLUMN run_effect_id TEXT NOT NULL DEFAULT ''")
            if "workspace_route" not in columns:
                # Earlier rows predate destination provenance. Do not infer a
                # Project or Work owner from the path alone.
                db.execute("ALTER TABLE cooperative_contexts ADD COLUMN workspace_route TEXT NOT NULL DEFAULT '{}' ")
            if "retired_write_contract" not in columns:
                # Preserve the old execution contract when future conversation
                # starts become read-only; never rewrite historical authority.
                db.execute("ALTER TABLE cooperative_contexts ADD COLUMN retired_write_contract TEXT NOT NULL DEFAULT ''")
            db.execute("""CREATE TABLE IF NOT EXISTS cooperative_bindings (
                session_id TEXT PRIMARY KEY, context_id TEXT, token TEXT NOT NULL,
                FOREIGN KEY(session_id,context_id)
                    REFERENCES cooperative_contexts(session_id,context_id))""")
            existing = db.execute("SELECT 1 FROM cooperative_bindings WHERE session_id=?",
                (session_id,)).fetchone()
            if existing is None:
                prior = db.execute("SELECT 1 FROM control_admissions WHERE source_scope=? LIMIT 1",
                    ("chat:" + session_id,)).fetchone()
                if prior is not None:
                    raise ControlLedgerConflict("source history exists without a durable cooperative binding")
                db.execute("INSERT INTO cooperative_bindings VALUES (?,NULL,?)",
                    (session_id, uuid.uuid4().hex))

    def load(self):
        with self.ledger._transaction() as db:
            binding = dict(db.execute("SELECT * FROM cooperative_bindings WHERE session_id=?",
                (self.session_id,)).fetchone())
            contexts = [dict(row) for row in db.execute(
                "SELECT * FROM cooperative_contexts WHERE session_id=? ORDER BY context_id",
                (self.session_id,))]
        return binding, [self._decode_context(row) for row in contexts]

    @staticmethod
    def _decode_context(row):
        row["requirements"] = ProviderRequirements.from_dict(json.loads(row["requirements"]))
        row["workspace_route"] = json.loads(row["workspace_route"] or "{}")
        if not isinstance(row["workspace_route"], dict):
            raise ControlLedgerConflict("stored cooperative workspace route is invalid")
        raw = json.loads(row["native_session"])
        row["native_session"] = ProviderSessionHandle.from_dict(raw) if raw else None
        if row["native_session"] is not None and (
                row["native_session"].provider != row["provider"]
                or (row["requirements"].resume == "attach" and row["native_session"].scope != "interaction")):
            raise ControlLedgerConflict("stored native context has foreign identity")
        return row

    def load_catalog(self):
        """Read identities and liveness without hydrating Provider payloads."""
        with self.ledger._transaction() as db:
            binding = dict(db.execute("SELECT * FROM cooperative_bindings WHERE session_id=?",
                (self.session_id,)).fetchone())
            rows = [dict(row) for row in db.execute("""
                SELECT context_id,label,provider,workspace,closed,run_id,run_status,revision,work_item_id
                FROM cooperative_contexts WHERE session_id=? ORDER BY context_id""", (self.session_id,))]
        return binding, rows

    def load_context(self, context_id):
        with self.ledger._transaction() as db:
            row = db.execute("SELECT * FROM cooperative_contexts WHERE session_id=? AND context_id=?",
                (self.session_id, context_id)).fetchone()
        return self._decode_context(dict(row)) if row is not None else None

    def load_summary(self, context_id):
        """The existing role projection needs policy and a bounded output tail, not native state."""
        with self.ledger._transaction() as db:
            row = db.execute("""
                SELECT context_id,label,provider,workspace,closed,run_id,run_status,work_item_id,
                    requirements,workspace_route,substr(output,-4000) AS output
                FROM cooperative_contexts WHERE session_id=? AND context_id=?""",
                (self.session_id, context_id)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["requirements"] = ProviderRequirements.from_dict(json.loads(result["requirements"]))
        result["workspace_route"] = json.loads(result["workspace_route"] or "{}")
        if not isinstance(result["workspace_route"], dict):
            raise ControlLedgerConflict("stored cooperative workspace route is invalid")
        return result

    def _binding(self, db, token, context_id):
        row = db.execute("SELECT * FROM cooperative_bindings WHERE session_id=?",
            (self.session_id,)).fetchone()
        if row is None or (row["token"], row["context_id"] or "") != (token, context_id):
            raise ControlLedgerConflict("durable cooperative binding changed")

    @staticmethod
    def _has_workspace_leases(cursor) -> bool:
        return cursor.execute("""SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='workspace_leases'""").fetchone() is not None

    def require_binding(self, token, context_id):
        with self.ledger._transaction() as db:
            self._binding(db, token, context_id)

    def register(self, child, *, initial_binding_token=None):
        """Persist the contract prepared by the existing workspace owner/validator."""
        with self.ledger._transaction() as db:
            if initial_binding_token is not None:
                self._binding(db, initial_binding_token, "")
            db.execute("""INSERT INTO cooperative_contexts
                (session_id,context_id,label,provider,workspace,closed,run_id,run_status,
                 native_session,output,last_input_id,last_turn_id,work_item_id,run_effect_id,
                 revision,requirements,workspace_route)
                VALUES (?,?,?,?,?,0,'','idle','null','','','','','',0,?,?)""",
                (self.session_id, child.child_id, child.label, child.provider, child.workspace,
                 json.dumps(child.requirements.to_dict(), sort_keys=True),
                 json.dumps(child.workspace_route, ensure_ascii=False, sort_keys=True)))
            if initial_binding_token is not None:
                db.execute("UPDATE cooperative_bindings SET context_id=? WHERE session_id=?",
                    (child.child_id, self.session_id))

    def bind(self, context_id, *, expected_token, expected_context_id):
        if not isinstance(context_id, str) or (
            context_id and context_id != context_id.strip()
        ):
            raise ControlLedgerConflict("durable binding recipient is invalid")
        token = uuid.uuid4().hex
        with self.ledger._transaction() as db:
            self._binding(db, expected_token, expected_context_id)
            if context_id:
                child = db.execute(
                    "SELECT closed FROM cooperative_contexts WHERE session_id=? AND context_id=?",
                    (self.session_id, context_id),
                ).fetchone()
                if child is None or child["closed"]:
                    raise ControlLedgerConflict(
                        "durable binding recipient absent or closed"
                    )
            db.execute("UPDATE cooperative_bindings SET context_id=?,token=? WHERE session_id=?",
                (context_id or None, token, self.session_id))
        return token

    @staticmethod
    def _path_identity(value):
        return os.path.normcase(str(Path(str(value or "")).expanduser().resolve()))

    def bind_work_item(self, child, work_item_id, *, binding_token, source_context_id=None):
        """Rebind one existing same-workspace WorkItem without creating execution."""
        clean_work_item_id = str(work_item_id or "").strip()
        if not clean_work_item_id:
            raise ValueError("work_item_id is required")
        with self.ledger._transaction() as db:
            source_context_id = child.child_id if source_context_id is None else source_context_id
            self._binding(db, binding_token, source_context_id)
            context = db.execute("SELECT * FROM cooperative_contexts WHERE session_id=? AND context_id=?",
                (self.session_id, child.child_id)).fetchone()
            if (context is None or context["closed"]
                    or context["revision"] != child.revision):
                raise ControlLedgerConflict("cooperative Work association changed")
            try:
                item = db.execute("SELECT workspace_path,state FROM work_items WHERE work_item_id=?",
                    (clean_work_item_id,)).fetchone()
            except sqlite3.OperationalError as exc:
                raise ControlLedgerConflict("Work ledger is unavailable for cooperative association") from exc
            if item is None:
                raise ControlLedgerConflict("unknown cooperative Work association")
            if str(item["state"]) == "archived":
                raise ControlLedgerConflict("archived Work cannot become a cooperative association")
            if self._path_identity(item["workspace_path"]) != self._path_identity(child.workspace):
                raise ControlLedgerConflict("Work and cooperative context use different workspaces")
            if str(context["work_item_id"]) == clean_work_item_id:
                child.work_item_id = clean_work_item_id
                return binding_token
            next_token = uuid.uuid4().hex if source_context_id == child.child_id else binding_token
            changed = db.execute("""UPDATE cooperative_contexts
                SET work_item_id=?,revision=revision+1
                WHERE session_id=? AND context_id=? AND revision=? AND work_item_id=?""",
                (clean_work_item_id, self.session_id, child.child_id, child.revision,
                 str(context["work_item_id"])))
            if changed.rowcount != 1:
                raise ControlLedgerConflict("cooperative Work association changed")
            if source_context_id == child.child_id:
                rebound = db.execute("""UPDATE cooperative_bindings SET token=?
                    WHERE session_id=? AND context_id=? AND token=?""",
                    (next_token, self.session_id, child.child_id, binding_token))
                if rebound.rowcount != 1:
                    raise ControlLedgerConflict("cooperative Work binding changed")
        child.work_item_id = clean_work_item_id
        child.revision += 1
        return next_token

    def work_recipient(self, payload, *, cursor=None, workspace_path="",
                       require_current_binding=False):
        """Resolve a settled cooperative address for the existing Work owner."""

        def resolve(db):
            if str(getattr(payload, "session_id", "")) != self.session_id:
                raise ControlLedgerConflict("cooperative Work recipient Session changed")
            context_id = str(getattr(payload, "cooperative_context_id", ""))
            token = str(getattr(payload, "cooperative_binding_token", ""))
            if require_current_binding:
                self._binding(db, token, context_id)
            row = db.execute("""SELECT * FROM cooperative_contexts
                WHERE session_id=? AND context_id=?""",
                (self.session_id, context_id)).fetchone()
            if (row is None or row["closed"]
                    or row["revision"] != getattr(payload,
                        "cooperative_context_revision", -1)
                    or row["provider"] != getattr(payload, "provider", "")
                    or row["run_status"] not in {"idle", "done", "error", "cancelled"}
                    or not row["workspace"]):
                raise ControlLedgerConflict(
                    "cooperative Work recipient is not a settled local context")
            route = json.loads(row["workspace_route"] or "{}")
            if (not isinstance(route, dict)
                    or str(route.get("projectId") or "")
                    != str(getattr(payload, "project_id", ""))):
                raise ControlLedgerConflict(
                    "cooperative Work recipient Project changed")
            if workspace_path and self._path_identity(workspace_path) != self._path_identity(
                    row["workspace"]):
                raise ControlLedgerConflict(
                    "cooperative Work recipient workspace changed")
            raw = json.loads(row["native_session"] or "null")
            try:
                session = ProviderSessionHandle.from_dict(raw)
            except (TypeError, ValueError) as exc:
                raise ControlLedgerConflict(
                    "cooperative Work recipient has no native context") from exc
            if session.provider != row["provider"] or session.scope != "interaction":
                raise ControlLedgerConflict(
                    "cooperative Work recipient native identity changed")
            return ProviderSessionAttachment(session=session, audit={
                "state":"cooperative_context_recipient",
                "provider":session.provider,
                "cooperative_context_id":context_id,
                "cooperative_context_revision":row["revision"],
                "workspace_path":row["workspace"],
            })

        if cursor is not None:
            return resolve(cursor)
        with self.ledger._lock:
            return resolve(self.ledger._db)

    def checkpoint(self, child, *, input_id=None, turn_id=None, text="", binding_token=None):
        """Persist before native I/O; CAS rejects an obsolete Host snapshot.

        User dispatch also rechecks its durable source and receiving binding in
        this transaction. Terminal observations do not acquire new user authority.
        """
        handle = child.native_session
        if handle is not None and (handle.provider != child.provider
                or (child.requirements.resume == "attach" and handle.scope != "interaction")):
            raise ControlLedgerConflict("native context has foreign identity")
        encoded = json.dumps(handle.to_dict() if handle else None, ensure_ascii=False, sort_keys=True)
        with self.ledger._transaction() as db:
            if input_id is not None:
                if not isinstance(turn_id, str) or not turn_id.strip():
                    raise ControlLedgerConflict("cooperative input requires its Chat turn identity")
                source = db.execute("""SELECT * FROM control_admissions
                    WHERE source_scope=? AND utterance_id=?""",
                    ("chat:" + self.session_id, input_id)).fetchone()
                if (source is None or source["lifecycle"] != "current"
                        or source["authority_mode"] != "legacy"
                        or source["transcript_hash"] != admission_transcript_hash(text)
                        or not self.ledger._current(db, source)):
                    raise ControlLedgerConflict("cooperative input source no longer authorizes dispatch")
            if binding_token is not None:
                self._binding(db, binding_token, child.child_id)
            changed = db.execute("""UPDATE cooperative_contexts SET closed=?,run_id=?,run_status=?,
                native_session=?,output=?,last_input_id=COALESCE(?,last_input_id),
                last_turn_id=COALESCE(?,last_turn_id),work_item_id=?,run_effect_id=?,
                revision=revision+1
                WHERE session_id=? AND context_id=? AND provider=? AND workspace=? AND revision=?
                    AND requirements=? AND workspace_route=? AND work_item_id=?
                    AND (? OR native_session='null' OR native_session=?)""",
                (int(child.closed), child.run_id, child.run_status, encoded, child.output,
                 input_id, turn_id, child.work_item_id, child.run_effect_id,
                 self.session_id, child.child_id,
                 child.provider, child.workspace, child.revision,
                  json.dumps(child.requirements.to_dict(), sort_keys=True),
                  json.dumps(child.workspace_route, ensure_ascii=False, sort_keys=True),
                  child.work_item_id, child.requirements.resume == "none", encoded))
            if changed.rowcount != 1:
                raise ControlLedgerConflict("cooperative context checkpoint changed")
        child.revision += 1

    def retire_conversation_write(self, child) -> bool:
        """Narrow a settled address while preserving its old contract and identity."""
        if child.requirements.workspace_access != "write" or child.closed:
            return False
        narrowed = replace(child.requirements, workspace_access="read")
        with self.ledger._transaction() as db:
            row = db.execute("SELECT * FROM cooperative_contexts WHERE session_id=? AND context_id=?",
                (self.session_id, child.child_id)).fetchone()
            if (row is None or row["revision"] != child.revision
                    or row["run_status"] not in {"idle", "done", "error", "cancelled"}):
                return False
            if row["run_effect_id"]:
                effect = db.execute("SELECT state FROM control_effect_outbox WHERE effect_id=?",
                    (row["run_effect_id"],)).fetchone()
                if effect is None or effect["state"] != "terminal":
                    return False
            pending_work = db.execute("""SELECT 1 FROM control_effect_outbox e
                JOIN control_admissions a ON a.root_id=e.root_id
                WHERE a.source_scope=? AND e.kind='work'
                AND json_extract(e.payload_json,'$.cooperative_context_id')=?
                AND e.state NOT IN ('terminal','cancelled') LIMIT 1""",
                ("chat:" + self.session_id, child.child_id)).fetchone()
            if pending_work is not None:
                return False
            retired = json.dumps({"requirements":json.loads(row["requirements"]),
                "through_run_id":row["run_id"], "through_effect_id":row["run_effect_id"],
                "revision":row["revision"]}, sort_keys=True)
            db.execute("""UPDATE cooperative_contexts SET
                retired_write_contract=CASE WHEN retired_write_contract=''
                    THEN ? ELSE retired_write_contract END,
                requirements=?,revision=revision+1 WHERE session_id=? AND context_id=?""",
                (retired, json.dumps(narrowed.to_dict(), sort_keys=True), self.session_id, child.child_id))
        child.requirements = narrowed
        child.revision += 1
        return True

    @staticmethod
    def _effect_payload_matches(payload, child, *, operation, input_id, turn_id,
                                binding_token, session_id, run_id="",
                                source_binding_context_id=None, continuation_effect_id=""):
        source_context = (child.child_id if source_binding_context_id is None
            else source_binding_context_id)
        expected = {
            "operation": operation,
            "session_id": session_id,
            "context_id": child.child_id,
            "binding_token": binding_token,
            "source_utterance_id": input_id,
            "turn_id": turn_id,
            "provider": child.provider,
            "run_id": run_id,
            "workspace": child.workspace,
            "source_binding_context_id": source_context,
        }
        # Accepted effects written before source/target separation used the
        # target context as their implicit source binding. Preserve those bytes.
        if "source_binding_context_id" not in payload and source_context == child.child_id:
            expected.pop("source_binding_context_id")
        if continuation_effect_id:
            expected["continuation_effect_id"] = continuation_effect_id
        return payload == expected

    def claim_provider_effect(self, child, effects, accepted, intent, *, text,
                              binding_token, updates, register_child=False):
        """Claim one accepted Provider effect with its pre-I/O marker."""

        checkpoint = replace(child, **dict(updates))
        effect_id = accepted["effect"]["effect_id"]
        if intent.operation == "start":
            checkpoint.run_effect_id = effect_id

        def write_intent(cursor, payload):
            source_binding_context_id = str(
                intent.source_binding_context_id or "")
            self._binding(cursor, binding_token, source_binding_context_id)
            row = cursor.execute(
                "SELECT * FROM cooperative_contexts WHERE session_id=? AND context_id=?",
                (self.session_id, child.child_id),
            ).fetchone()
            if not self._effect_payload_matches(payload, child,
                    operation=intent.operation,
                    input_id=intent.source_utterance_id, turn_id=intent.turn_id,
                    binding_token=binding_token, session_id=self.session_id,
                    run_id=intent.run_id,
                    continuation_effect_id=intent.continuation_effect_id,
                    source_binding_context_id=source_binding_context_id):
                raise ControlLedgerConflict(
                    "cooperative Provider effect context changed")
            source = cursor.execute("""SELECT * FROM control_admissions
                WHERE source_scope=? AND utterance_id=?""",
                ("chat:" + self.session_id, intent.source_utterance_id)).fetchone()
            if (source is None or source["root_id"] != accepted["effect"]["root_id"]
                    or source["authority_mode"] != "turn_decision"
                    or source["transcript_hash"] != admission_transcript_hash(text)):
                raise ControlLedgerConflict("cooperative Provider effect source changed")
            if intent.continuation_effect_id:
                effects.task_root(intent.continuation_effect_id, session_id=self.session_id,
                    context_id=child.child_id, provider=child.provider, cursor=cursor)
            if row is None and register_child:
                encoded = json.dumps(
                    checkpoint.native_session.to_dict()
                    if checkpoint.native_session else None,
                    ensure_ascii=False, sort_keys=True,
                )
                cursor.execute("""INSERT INTO cooperative_contexts (
                    session_id,context_id,label,provider,workspace,closed,run_id,
                    run_status,native_session,output,last_input_id,last_turn_id,
                    work_item_id,run_effect_id,revision,requirements,workspace_route)
                    VALUES (?,?,?,?,?,0,?,?,?,?,?,?,?,?,1,?,?)""",
                    (self.session_id, child.child_id, child.label, child.provider,
                     child.workspace, checkpoint.run_id, checkpoint.run_status,
                     encoded, checkpoint.output, intent.source_utterance_id,
                     intent.turn_id, child.work_item_id, checkpoint.run_effect_id,
                     json.dumps(child.requirements.to_dict(), sort_keys=True),
                     json.dumps(child.workspace_route, ensure_ascii=False,
                        sort_keys=True)))
                return {"context_id":child.child_id, "revision":1,
                    "registered":True}
            if (row is None or row["closed"] or row["revision"] != child.revision
                    or row["provider"] != child.provider
                    or row["workspace"] != child.workspace
                    or row["work_item_id"] != child.work_item_id):
                raise ControlLedgerConflict("cooperative Provider effect context changed")
            encoded = json.dumps(
                checkpoint.native_session.to_dict() if checkpoint.native_session else None,
                ensure_ascii=False, sort_keys=True,
            )
            changed = cursor.execute("""UPDATE cooperative_contexts
                SET run_id=?,run_status=?,native_session=?,output=?,last_input_id=?,
                    last_turn_id=?,run_effect_id=?,revision=revision+1
                WHERE session_id=? AND context_id=? AND revision=? AND requirements=?""",
                (checkpoint.run_id, checkpoint.run_status, encoded, checkpoint.output,
                 intent.source_utterance_id, intent.turn_id, checkpoint.run_effect_id,
                 self.session_id, child.child_id, child.revision,
                 json.dumps(child.requirements.to_dict(), sort_keys=True)))
            if changed.rowcount != 1:
                raise ControlLedgerConflict("cooperative context checkpoint changed")
            return {"context_id":child.child_id, "revision":child.revision + 1}

        claimed = effects.claim_with_local_intent(effect_id, apply=write_intent)
        for key, value in updates.items():
            setattr(child, key, value)
        if intent.operation == "start":
            child.run_effect_id = effect_id
        child.revision += 1
        return claimed["effect"]

    def settle_provider_run(self, child, effects, claim, intent, record):
        """Commit the Provider terminal receipt and context result together."""

        if record.status not in {"done", "error", "cancelled"}:
            raise ControlLedgerConflict(
                "cooperative Provider receipt requires a terminal result"
            )

        raw = record.metadata.get("provider_session")
        handle = ProviderSessionHandle.from_dict(raw) if raw else child.native_session
        if child.native_session is not None and handle != child.native_session:
            raise ControlLedgerConflict("native context identity changed within one conversation")
        output = record.result or record.error or ""

        def write_terminal(cursor, payload):
            row = cursor.execute(
                "SELECT * FROM cooperative_contexts WHERE session_id=? AND context_id=?",
                (self.session_id, child.child_id),
            ).fetchone()
            if (row is None or row["revision"] != child.revision
                    or row["run_id"] != record.run_id
                    or row["run_effect_id"] != claim["effect_id"]
                    or not self._effect_payload_matches(payload, child,
                        operation="start", input_id=intent.source_utterance_id,
                        turn_id=intent.turn_id, binding_token=intent.binding_token,
                        session_id=self.session_id,
                        continuation_effect_id=intent.continuation_effect_id,
                        source_binding_context_id=(
                            intent.source_binding_context_id))):
                raise ControlLedgerConflict("cooperative terminal checkpoint changed")
            encoded = json.dumps(handle.to_dict() if handle else None,
                ensure_ascii=False, sort_keys=True)
            changed = cursor.execute("""UPDATE cooperative_contexts
                SET run_status=?,native_session=?,output=?,revision=revision+1
                WHERE session_id=? AND context_id=? AND revision=?""",
                (record.status, encoded, output, self.session_id, child.child_id,
                 child.revision))
            if changed.rowcount != 1:
                raise ControlLedgerConflict("cooperative terminal checkpoint changed")
            if self._has_workspace_leases(cursor):
                WorkLedgerStore.release_cooperative_writer_lease_in_transaction(
                    cursor, claim["effect_id"], status="released",
                    metadata={"provider_status":record.status},
                    session_id=self.session_id, context_id=child.child_id,
                    workspace_path=child.workspace)
            return {"context_id":child.child_id, "revision":child.revision + 1}

        outcome = "succeeded" if record.status == "done" else (
            "cancelled" if record.status == "cancelled" else "failed"
        )
        receipt = effects.settle(claim, intent, run_id=record.run_id,
            outcome=outcome, details={"status":record.status}, apply=write_terminal)
        child.run_status, child.native_session, child.output = record.status, handle, output
        child.revision += 1
        return receipt

    def restore_rejected_start(self, child, effects, claim, intent, *, prior, reason,
                               external_run_id=""):
        """Close a start effect when Runtime proved that no adapter was scheduled."""

        prior_run_id, prior_status, prior_session, prior_output, prior_effect_id = prior

        def write_rejection(cursor, payload):
            row = cursor.execute(
                "SELECT * FROM cooperative_contexts WHERE session_id=? AND context_id=?",
                (self.session_id, child.child_id),
            ).fetchone()
            expected_status = "queued" if external_run_id else "dispatching"
            if (row is None or row["revision"] != child.revision
                    or row["run_status"] != expected_status
                    or (external_run_id and row["run_id"] != external_run_id)
                    or row["run_effect_id"] != claim["effect_id"]
                    or payload.get("operation") != "start"):
                raise ControlLedgerConflict("cooperative rejected start checkpoint changed")
            encoded = json.dumps(prior_session.to_dict() if prior_session else None,
                ensure_ascii=False, sort_keys=True)
            cursor.execute("""UPDATE cooperative_contexts SET run_id=?,run_status=?,
                native_session=?,output=?,run_effect_id=?,revision=revision+1
                WHERE session_id=? AND context_id=? AND revision=?""",
                (prior_run_id, prior_status, encoded, prior_output, prior_effect_id,
                 self.session_id, child.child_id, child.revision))
            if self._has_workspace_leases(cursor):
                WorkLedgerStore.release_cooperative_writer_lease_in_transaction(
                    cursor, claim["effect_id"], status="released",
                    metadata={"start_rejected":str(reason)},
                    session_id=self.session_id, context_id=child.child_id,
                    workspace_path=child.workspace)
            return {"context_id":child.child_id, "revision":child.revision + 1}

        receipt = (effects.settle(claim, intent, run_id=external_run_id,
            outcome="failed", details={"state":"rejected", "reason":str(reason),
                "before_execution":True}, apply=write_rejection)
            if external_run_id else effects.settle_unstarted(
                claim, intent, reason=reason, apply=write_rejection))
        child.run_id, child.run_status = prior_run_id, prior_status
        child.native_session, child.output = prior_session, prior_output
        child.run_effect_id = prior_effect_id
        child.revision += 1
        return receipt

    def bind_runtime_run(self, child, request, run_id, intake_authority=None, *,
                         writer_lease_required=False):
        """Bind Runtime's allocated identity to the already accepted dispatch slot.

        This is a dedicated Runtime's intake hook, before adapter scheduling.
        Metadata locates the slot; it cannot create or authorize a new one.
        """
        metadata = request.metadata
        if (not run_id or request.provider != child.provider or (request.cwd or "") != child.workspace
                or request.session != child.native_session
                or request.ownership != child.requirements.ownership or request.requirements != child.requirements
                or request.recovery is not None or "work" in metadata
                or metadata.get("cooperative_context_id") != child.child_id
                or metadata.get("session_id") != self.session_id
                or metadata.get("source_context_scope") != "chat:" + self.session_id
                or str(metadata.get("cooperative_work_item_id") or "") != child.work_item_id
                or metadata.get("source_user_text") != request.task):
            raise ControlLedgerConflict("request does not match the cooperative context")
        if intake_authority is None:
            # Compatibility seam for the provider-contract fixtures. The real
            # Chat assembly installs an effect ledger and rejects this path
            # before Runtime intake.
            with self.ledger._transaction() as db:
                row = db.execute(
                    "SELECT * FROM cooperative_contexts WHERE session_id=? AND context_id=?",
                    (self.session_id, child.child_id),
                ).fetchone()
                if (row is None or row["closed"] or row["run_id"]
                        or row["run_status"] != "dispatching"
                        or row["revision"] != child.revision
                        or not row["last_input_id"] or not row["last_turn_id"]
                        or row["last_input_id"] != metadata.get("source_utterance_id")
                        or row["last_turn_id"] != metadata.get("turn_id")
                        or row["work_item_id"] != child.work_item_id):
                    raise ControlLedgerConflict("cooperative dispatch slot is not available")
                source = db.execute("""SELECT * FROM control_admissions
                    WHERE source_scope=? AND utterance_id=?""",
                    ("chat:" + self.session_id, row["last_input_id"])).fetchone()
                if (source is None or source["authority_mode"] != "legacy"
                        or source["transcript_hash"] != admission_transcript_hash(request.task)):
                    raise ControlLedgerConflict("cooperative dispatch source does not match")
                db.execute("""UPDATE cooperative_contexts
                    SET run_id=?,run_status='queued',revision=revision+1
                    WHERE session_id=? AND context_id=?""",
                    (run_id, self.session_id, child.child_id))
            child.run_id, child.run_status = run_id, "queued"
            child.revision += 1
            return request
        if (not isinstance(intake_authority, ProviderRunIntakeAuthority)
                or intake_authority.kind != "cooperative_provider_effect"
                or intake_authority.effect_id != child.run_effect_id):
            raise ControlLedgerConflict(
                "cooperative dispatch requires its accepted Provider effect"
            )
        effect = self.ledger.get_effect(intake_authority.effect_id)

        def bind_run(cursor, payload):
            row = cursor.execute(
                "SELECT * FROM cooperative_contexts WHERE session_id=? AND context_id=?",
                (self.session_id, child.child_id),
            ).fetchone()
            if (row is None or row["closed"] or row["run_id"] or row["run_status"] != "dispatching"
                    or row["revision"] != child.revision or not row["last_input_id"]
                    or not row["last_turn_id"]
                    or row["last_input_id"] != metadata.get("source_utterance_id")
                    or row["last_turn_id"] != metadata.get("turn_id")
                    or row["work_item_id"] != child.work_item_id
                    or row["run_effect_id"] != intake_authority.effect_id
                    or not self._effect_payload_matches(payload, child,
                        operation="start", input_id=row["last_input_id"],
                        turn_id=row["last_turn_id"],
                        binding_token=payload.get("binding_token"),
                        session_id=self.session_id,
                        continuation_effect_id=payload.get("continuation_effect_id", ""),
                        source_binding_context_id=payload.get(
                            "source_binding_context_id", child.child_id))):
                raise ControlLedgerConflict("cooperative dispatch slot is not available")
            if ProviderRequirements.from_dict(json.loads(row["requirements"])) != request.requirements:
                raise ControlLedgerConflict("cooperative request changed its accepted requirements")
            source = cursor.execute("SELECT transcript_hash FROM control_admissions WHERE source_scope=? AND utterance_id=?",
                ("chat:" + self.session_id, row["last_input_id"])).fetchone()
            if source is None or source["transcript_hash"] != admission_transcript_hash(request.task):
                raise ControlLedgerConflict("cooperative dispatch source does not match")
            if self._has_workspace_leases(cursor):
                WorkLedgerStore.bind_cooperative_writer_run_in_transaction(
                    cursor, intake_authority.effect_id, run_id,
                    session_id=self.session_id, context_id=child.child_id,
                    workspace_path=child.workspace,
                    required=bool(writer_lease_required))
            # The slot was accepted before this hook. A subsequent Chat turn or
            # binding change cannot redirect that previously accepted input.
            cursor.execute("""UPDATE cooperative_contexts SET run_id=?,run_status='queued',revision=revision+1
                WHERE session_id=? AND context_id=?""", (run_id, self.session_id, child.child_id))
            return {"context_id":child.child_id, "revision":child.revision + 1}

        self.ledger.bind_external_with_local(intake_authority.effect_id,
            claim_token=effect["claim_token"], external_id=run_id, apply=bind_run)
        child.run_id, child.run_status = run_id, "queued"
        child.revision += 1
        return PreparedProviderRun(request=request,
            intake_receipt=ProviderRunIntakeReceipt(
                effect_id=intake_authority.effect_id, run_id=run_id))
