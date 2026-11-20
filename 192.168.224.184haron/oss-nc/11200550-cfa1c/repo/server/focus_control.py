"""Isolated Phase-B integration: admitted Focus -> local domain + receipt.

No app composition, settings switch, model query, Provider, or presentation sink
is installed here. A Host caller must select the authority cohort and capture a
real granted epoch before using this adapter. The single typed Focus decision is
an already-adjudicated input, not permission to turn arbitrary text into Focus.
Whole-turn Work/AUIP/Browser exclusion and live admission/rollback wiring remain
caller integration gates; this module does not infer their absence.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Mapping
import uuid

from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerNotFound, WorkLedgerStore
from agent_host.work_ledger_types import ProjectRecord
from server.control_decision import ControlDecision
from server.control_ledger import ControlEffect, ControlLedgerConflict, ControlLedgerStore
from server.reference_catalog import TypedReferenceCandidate, validate_candidate_catalog
from server.turn_admission import TurnAdmissionRecord
from server.work_destination_service import WorkDestinationService


_IDENTITY_NAMESPACE = uuid.UUID("f3f781e0-c99d-49cd-afb4-ef5b466f5ffb")


class FocusControl:
    """Ground one taskless Focus using existing Host identity/trust ownership."""

    def __init__(
        self,
        ledger: ControlLedgerStore,
        destination: WorkDestinationService,
        *,
        fence_scope: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not str(fence_scope or "").strip():
            raise ValueError("the actual epoch issuer scope is required")
        if (
            destination.store.db_path == ":memory:"
            or Path(destination.store.db_path).resolve() != ledger.path
        ):
            raise ControlLedgerConflict("Focus domain and Control Ledger must share one database")
        self.ledger = ledger
        self.destination = destination
        self.fence_scope = fence_scope
        self._clock = clock

    @staticmethod
    def _validate_admission(admission: TurnAdmissionRecord) -> None:
        if (
            not isinstance(admission, TurnAdmissionRecord)
            or not admission.session_id
            or admission.dialogue_source_scope != "chat:" + admission.session_id
            or admission.pending
            or isinstance(admission.chat_epoch, bool)
            or not isinstance(admission.chat_epoch, int)
            or admission.chat_epoch < 0
            or admission.authority_mode not in {"legacy", "turn_decision"}
        ):
            raise ControlLedgerConflict("Focus requires a confirmed, mode-bound Chat admission")

    def admit(self, admission: TurnAdmissionRecord) -> dict[str, Any]:
        """Persist the Host's choice; never infer a mode or issue an epoch."""
        self._validate_admission(admission)
        return self.ledger.admit(
            root_id=admission.root_id,
            source_scope=admission.dialogue_source_scope,
            fence_scope=self.fence_scope,
            utterance_id=admission.utterance_id,
            chat_epoch=admission.chat_epoch,
            authority_mode=admission.authority_mode,
            transcript_hash=admission.transcript_hash,
        )

    def _bound_admission(self, admission: TurnAdmissionRecord) -> dict[str, Any]:
        self._validate_admission(admission)
        stored = self.ledger.get_admission(admission.root_id)
        if (
            stored["source_scope"], stored["fence_scope"], stored["utterance_id"],
            stored["authority_mode"], stored["transcript_hash"],
        ) != (
            admission.dialogue_source_scope, self.fence_scope, admission.utterance_id,
            admission.authority_mode, admission.transcript_hash,
        ):
            raise ControlLedgerConflict("Focus source does not match the durable admission")
        # A transport replay can have a new presentation alias/epoch; only the
        # first durable admission owns acceptance and its original fence.
        return stored

    @staticmethod
    def _target(decision: ControlDecision) -> str:
        if not isinstance(decision, ControlDecision) or decision.status != "ok" or len(decision.entries) != 1:
            raise ControlLedgerConflict("Focus requires one adjudicated decision")
        entry = decision.entries[0]
        if (
            entry.proposal_index != 0
            or entry.control.get("intent") != "focus"
            or set(entry.control) - {"provider", "intent", "subject"}
            or entry.work_placement != "not_applicable"
            or entry.workspace_effect != "none"
            or entry.payload_continuity != "current_turn"
        ):
            raise ControlLedgerConflict("only taskless context Focus belongs to this adapter")
        if entry.session_context == "clear" and entry.reference_candidates is None:
            return ""
        candidates = entry.reference_candidates
        if (
            entry.session_context != "bind"
            or not isinstance(candidates, tuple)
            or len(candidates) != 1
            or not isinstance(candidates[0], TypedReferenceCandidate)
            or candidates[0].kind != "project"
            or entry.reference_kind not in {"open", "project"}
        ):
            raise ControlLedgerConflict("Focus requires one exact Project or explicit Draft clear")
        catalog_error = validate_candidate_catalog(candidates)
        if catalog_error:
            raise ControlLedgerConflict("invalid Focus candidate: " + catalog_error)
        return candidates[0].entity_id

    @staticmethod
    def _identity(root_id: str, kind: str) -> str:
        return uuid.uuid5(_IDENTITY_NAMESPACE, json.dumps([root_id, kind])).hex

    def seal(self, admission: TurnAdmissionRecord, decision: ControlDecision) -> dict[str, Any]:
        """Validate the supported slice and freeze one root/plan/effect shape."""
        stored = self._bound_admission(admission)
        if stored["authority_mode"] != "turn_decision":
            raise ControlLedgerConflict("legacy admission cannot seal a new-mode Focus")
        project_id = self._target(decision)
        # Read-only validation. No scratch allocation, active-context getter,
        # feedback cleanup, EventBus or ordinary independently committing writer.
        # Existing accepted plans replay their frozen identity without depending
        # on whether their old Project still exists today.
        if project_id and stored["plan_id"] is None:
            self.destination.available_project(project_id)
        effect_id = self._identity(admission.root_id, "focus:0")
        accepted = self.ledger.accept(
            admission.root_id,
            chat_epoch=stored["chat_epoch"],
            plan_id=self._identity(admission.root_id, "focus-plan:v1"),
            effects=(ControlEffect(
                effect_id, "focus", "session_context:" + admission.session_id,
                {"session_id": admission.session_id, "project_id": project_id},
            ),),
            evidence={"adapter": "taskless_focus:v1", "transcript_hash": admission.transcript_hash},
        )
        return {**accepted, "effect_id": effect_id}

    @staticmethod
    def _write(
        cursor: sqlite3.Cursor,
        payload: Mapping[str, Any],
        *,
        project: ProjectRecord | None,
        now: float,
    ) -> dict[str, Any]:
        project_id = str(payload["project_id"])
        if project is not None:
            # The identity/path checked by the domain must still be the row
            # used by this SQL transaction; do not read another connection.
            row = cursor.execute(
                "SELECT canonical_path, name FROM projects WHERE project_id=?", (project_id,),
            ).fetchone()
            if row is None or row["canonical_path"] != project.canonical_path:
                raise ControlLedgerConflict("Focus Project changed after grounding")
            project_name = str(row["name"])
        else:
            project_name = "Draft"
        WorkLedgerStore.write_session_context(
            cursor, str(payload["session_id"]), project_id=project_id,
            binding_metadata={"source": "focus"}, now=now,
        )
        return {**payload, "project_name": project_name}

    def apply(self, effect_id: str) -> dict[str, Any]:
        """Revalidate then commit domain rows and receipt in the same database.

        Return a durable fact only. Callers publish/clear feedback after commit;
        a presentation failure never turns this effect back into pending.
        """
        effect = self.ledger.get_effect(effect_id)
        if effect["kind"] != "focus":
            raise ControlLedgerConflict("not a Focus effect")
        payload = json.loads(effect["payload_json"])
        if (
            set(payload) != {"session_id", "project_id"}
            or not isinstance(payload["session_id"], str)
            or not payload["session_id"]
            or not isinstance(payload["project_id"], str)
            or effect["target_key"] != "session_context:" + payload["session_id"]
        ):
            raise ControlLedgerConflict("invalid Focus effect target")
        admission = self.ledger.get_admission(effect["root_id"])
        if (
            admission["source_scope"] != "chat:" + payload["session_id"]
            or admission["fence_scope"] != self.fence_scope
            or admission["authority_mode"] != "turn_decision"
            or effect_id != self._identity(effect["root_id"], "focus:0")
        ):
            raise ControlLedgerConflict("Focus effect does not belong to this admitted source")
        project = None
        if payload["project_id"] and effect["state"] != "terminal":
            try:
                project = self.destination.available_project(payload["project_id"])
            except (WorkLedgerConflict, WorkLedgerNotFound):
                # Another applicant may have committed since our pending read.
                # A terminal fact does not need permission to reapply its old
                # effect. No receipt means the original refusal still stands.
                if self.ledger.get_receipt(effect_id) is None:
                    raise
        return self.ledger.apply_local(
            effect_id, owner="focus_control",
            apply=lambda cursor, frozen: self._write(
                cursor, frozen, project=project, now=float(self._clock()),
            ),
        )

    def apply_legacy(
        self, admission: TurnAdmissionRecord, decision: ControlDecision,
    ) -> dict[str, Any]:
        """Existing Focus SQL under a legacy admission, never a new-mode fallback."""
        stored = self._bound_admission(admission)
        if stored["authority_mode"] != "legacy":
            raise ControlLedgerConflict("legacy commit cannot bypass this admission")
        project_id = self._target(decision)
        project = self.destination.available_project(project_id) if project_id else None
        return self.ledger.apply_legacy_local(
            admission.root_id,
            apply=lambda cursor: self._write(
                cursor, {"session_id": admission.session_id, "project_id": project_id},
                project=project, now=float(self._clock()),
            ),
        )
