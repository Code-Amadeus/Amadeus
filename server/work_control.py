"""Host bridge from one accepted Work plan to Work intake.

It accepts already-grounded typed payloads, seals their immutable Control plan, and
atomically persists each selected dispatch claim plus its new Work or existing-Work
operation.
It performs no Provider, filesystem, EventBus, permission, presentation, or
semantic-model work.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Literal, Mapping
import uuid

from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_authoring import auip_authoring_outcome_requirement
from agent_host.provider_identity import (
    MAIN_ROLE_NAME_METADATA_KEY,
    SOURCE_CONTEXT_SCOPE_METADATA_KEY,
    SOURCE_UTTERANCE_ID_METADATA_KEY,
)
from agent_host.provider_types import ProviderRunIntakeAuthority, ProviderRunRequest, ProviderSessionHandle
from agent_host.work_ledger_store import (
    WorkLedgerConflict,
    WorkLedgerNotFound,
    WorkLedgerStore,
)
from server.control_ledger import (
    ControlEffect,
    ControlLedgerConflict,
    ControlLedgerStore,
    ReconciliationPolicy,
)
from server.turn_admission import TurnAdmissionRecord, admission_transcript_hash
from server.inherited_role_prompt import MAIN_CONVERSATION_ROLE_NAME
from server.provider_session_binding import ProviderSessionAttachment
from server.scratch_workspace import is_scratch_root, scratch_workspace_path
from server.provider_event_ingestion import (
    PROVIDER_TERMINAL_PIPELINE_METADATA_KEY,
    ProviderEventIngestor,
)


_IDENTITY_NAMESPACE = uuid.UUID("d596fdf7-b102-4dc8-85da-9f5cc03226d5")
_RUNTIME_CLAIM_LEASE_SECONDS = 15.0
_RUNTIME_RECONCILIATION = ReconciliationPolicy(
    "provider_submission",
    max_probes=3,
    interval_seconds=5.0,
    ttl_seconds=60.0,
)
_PAYLOAD_KEYS = frozenset(
    {
        "version",
        "operation",
        "provider",
        "task",
        "title",
        "mode",
        "ownership",
        "project_id",
        "session_id",
        "turn_id",
        "requirements",
    }
)
_PAYLOAD_V2_KEYS = frozenset(
    {
        *_PAYLOAD_KEYS,
        "utterance_id",
        "source_user_text",
        "source_user_context",
        "source_context_scope",
        "payload_continuity",
    }
)
_SOURCE_SPAN_V1_KEYS = frozenset(
    {
        "version",
        "kind",
        "digest_algorithm",
        "root_id",
        "source_scope",
        "utterance_id",
        "transcript_hash",
        "start",
        "end",
        "selected_text_sha256",
    }
)
_PAYLOAD_V3_KEYS = frozenset(
    {
        *_PAYLOAD_KEYS,
        "utterance_id",
        "source_user_text",
        "source_user_context",
        "source_context_scope",
        "source_proof",
    }
)
_SOURCE_DIGEST_ALGORITHM: Literal["sha256_utf8_v1"] = "sha256_utf8_v1"
_MAX_SOURCE_USER_TEXT = 4000
_BATCH_EVIDENCE_ADAPTER = "proposal_gated_current_turn_work_batch:v1"
_ACCEPTED_REQUEST_REQUIRED_METADATA_KEYS = frozenset(
    {
        "source",
        "session_id",
        "turn_id",
        SOURCE_UTTERANCE_ID_METADATA_KEY,
        "source_user_text",
        SOURCE_CONTEXT_SCOPE_METADATA_KEY,
        "payload_continuity",
        "intent",
        "continuation",
        "write_intent",
        "provider_requirements",
        MAIN_ROLE_NAME_METADATA_KEY,
        "work",
    }
)
_ACCEPTED_REQUEST_OPTIONAL_METADATA_KEYS = frozenset(
    {
        "source_user_context",
        "source_user_operation_text",
        "external_export",
        "host_outcome_requirement",
        # ProviderRuntime deterministically projects these from the registered
        # manifest before the configured Work intake owner is called.
        "provider_ownership",
        "provider_manifest",
        "provider_operation",
    }
)
_ACCEPTED_REQUEST_WORK_KEYS = frozenset(
    {"project_id", "workspace_path", "workspace_mode", "title"}
)


def _work_payload_shape(value: Any, required: frozenset[str] | set[str]) -> bool:
    return isinstance(value, Mapping) and set(value) in (
        set(required), set(required) | {"external_export_target"})


def _required(value: str, label: str, *, limit: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    clean = value.strip()
    if len(clean) > limit:
        raise ValueError(f"{label} is too long")
    return clean


def _strict_utf8(value: str, label: str) -> str:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must contain only Unicode scalar values") from exc
    return value


def _exact_source_user_text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("source_user_text is required")
    if len(value) > _MAX_SOURCE_USER_TEXT:
        raise ValueError("source_user_text is too long")
    return _strict_utf8(value, "source_user_text")


def _strict_trimmed(value: str, label: str, *, limit: int = 4000) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be one non-empty trimmed string")
    if len(value) > limit:
        raise ValueError(f"{label} is too long")
    return _strict_utf8(value, label)


def _sha256_utf8(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def _canonical_sha256(value: str, label: str) -> str:
    clean = _strict_trimmed(value, label, limit=64)
    if len(clean) != 64 or any(char not in "0123456789abcdef" for char in clean):
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return clean


def _canonical_requirements(
    value: ProviderRequirements,
    *,
    owner_label: str,
) -> ProviderRequirements:
    if not isinstance(value, ProviderRequirements):
        raise TypeError("requirements must use ProviderRequirements")
    try:
        canonical = ProviderRequirements.from_dict(value.to_dict())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid Provider requirements: {exc}") from exc
    if canonical != value:
        raise ValueError("Provider requirements must use their canonical typed form")
    if value.ownership != "managed":
        raise ValueError(f"the {owner_label} Work effect requires managed Provider ownership")
    return value


@dataclass(frozen=True, slots=True)
class CurrentTurnSourceSpanV1:
    """Exact local Host selection inside one admitted current transcript.

    This proves provenance membership only.  It does not infer whether the
    selected text semantically authorizes an operation.
    """

    root_id: str
    source_scope: str
    utterance_id: str
    transcript_hash: str
    start: int
    end: int
    selected_text_sha256: str
    digest_algorithm: Literal["sha256_utf8_v1"] = _SOURCE_DIGEST_ALGORITHM

    def __post_init__(self) -> None:
        root_id = _strict_trimmed(self.root_id, "root_id", limit=240)
        source_scope = _strict_trimmed(self.source_scope, "source_scope", limit=800)
        utterance_id = _strict_trimmed(self.utterance_id, "utterance_id", limit=240)
        transcript_hash = _canonical_sha256(self.transcript_hash, "transcript_hash")
        selected_text_sha256 = _canonical_sha256(
            self.selected_text_sha256,
            "selected_text_sha256",
        )
        if self.digest_algorithm != _SOURCE_DIGEST_ALGORITHM:
            raise ValueError("unsupported source-span digest algorithm")
        if (
            isinstance(self.start, bool)
            or not isinstance(self.start, int)
            or isinstance(self.end, bool)
            or not isinstance(self.end, int)
            or self.start < 0
            or self.end <= self.start
            or self.end > _MAX_SOURCE_USER_TEXT
        ):
            raise ValueError("source span must be one bounded increasing integer range")
        object.__setattr__(self, "root_id", root_id)
        object.__setattr__(self, "source_scope", source_scope)
        object.__setattr__(self, "utterance_id", utterance_id)
        object.__setattr__(self, "transcript_hash", transcript_hash)
        object.__setattr__(self, "selected_text_sha256", selected_text_sha256)

    @classmethod
    def capture(
        cls,
        admission: TurnAdmissionRecord,
        source_user_text: str,
        *,
        start: int,
        end: int,
    ) -> "CurrentTurnSourceSpanV1":
        """Construct proof only from an exact local admission/transcript pair."""

        if not isinstance(admission, TurnAdmissionRecord):
            raise TypeError("source span requires a TurnAdmissionRecord")
        source = _exact_source_user_text(source_user_text)
        if admission_transcript_hash(source) != admission.transcript_hash:
            raise ValueError("source span transcript does not match admission")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or end > len(source)
        ):
            raise ValueError("source span is outside the admitted transcript")
        selected = source[start:end]
        _strict_trimmed(selected, "selected source span")
        return cls(
            root_id=admission.root_id,
            source_scope=admission.dialogue_source_scope,
            utterance_id=admission.utterance_id,
            transcript_hash=admission.transcript_hash,
            start=start,
            end=end,
            selected_text_sha256=_sha256_utf8(selected),
        )

    def selected_text(self, source_user_text: str) -> str:
        source = _exact_source_user_text(source_user_text)
        if self.end > len(source):
            raise ValueError("source span is outside the admitted transcript")
        selected = source[self.start : self.end]
        _strict_trimmed(selected, "selected source span")
        if _sha256_utf8(selected) != self.selected_text_sha256:
            raise ValueError("selected source span digest does not match")
        return selected

    def validate_identity(
        self,
        *,
        root_id: str,
        source_scope: str,
        utterance_id: str,
        transcript_hash: str,
        source_user_text: str,
    ) -> str:
        if (
            self.root_id != root_id
            or self.source_scope != source_scope
            or self.utterance_id != utterance_id
            or self.transcript_hash != transcript_hash
            or admission_transcript_hash(source_user_text) != transcript_hash
        ):
            raise ValueError("source span identity does not match admission")
        return self.selected_text(source_user_text)

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "kind": "current_turn_span",
            "digest_algorithm": self.digest_algorithm,
            "root_id": self.root_id,
            "source_scope": self.source_scope,
            "utterance_id": self.utterance_id,
            "transcript_hash": self.transcript_hash,
            "start": self.start,
            "end": self.end,
            "selected_text_sha256": self.selected_text_sha256,
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "CurrentTurnSourceSpanV1":
        if not isinstance(value, Mapping) or set(value) != _SOURCE_SPAN_V1_KEYS:
            raise ValueError("invalid current-turn source-span shape")
        if (
            type(value.get("version")) is not int
            or value.get("version") != 1
            or value.get("kind") != "current_turn_span"
        ):
            raise ValueError("unsupported current-turn source-span version/kind")
        proof = cls(
            root_id=value.get("root_id"),  # type: ignore[arg-type]
            source_scope=value.get("source_scope"),  # type: ignore[arg-type]
            utterance_id=value.get("utterance_id"),  # type: ignore[arg-type]
            transcript_hash=value.get("transcript_hash"),  # type: ignore[arg-type]
            start=value.get("start"),  # type: ignore[arg-type]
            end=value.get("end"),  # type: ignore[arg-type]
            selected_text_sha256=value.get("selected_text_sha256"),  # type: ignore[arg-type]
            digest_algorithm=value.get("digest_algorithm"),  # type: ignore[arg-type]
        )
        if proof.to_payload() != dict(value):
            raise ValueError("current-turn source-span proof is not canonical")
        return proof


@dataclass(frozen=True, slots=True)
class WorkEffectPayloadV1:
    """Minimal durable input for the first new-Work authority slice.

    This is not a generic Provider envelope or a semantic proposal.  A future
    Runtime-integrated slice may widen/version the payload only after this
    storage boundary passes.  C1 intentionally supports new Work only.
    """

    provider: str
    task: str
    title: str
    project_id: str
    session_id: str
    turn_id: str
    requirements: ProviderRequirements
    mode: str = "agent"

    def __post_init__(self) -> None:
        provider = _required(self.provider, "provider", limit=120).lower()
        task = _required(self.task, "task")
        title = _required(self.title, "title", limit=240)
        project_id = _required(self.project_id, "project_id", limit=240)
        session_id = _required(self.session_id, "session_id", limit=240)
        turn_id = _required(self.turn_id, "turn_id", limit=240)
        mode = _required(self.mode, "mode", limit=80).lower()
        _canonical_requirements(self.requirements, owner_label="C1")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "task", task)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "mode", mode)

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "operation": "execute",
            "provider": self.provider,
            "task": self.task,
            "title": self.title,
            "mode": self.mode,
            "ownership": "managed",
            "project_id": self.project_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "requirements": self.requirements.to_dict(),
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "WorkEffectPayloadV1":
        if not isinstance(value, Mapping) or set(value) != _PAYLOAD_KEYS:
            raise ControlLedgerConflict("invalid Work effect payload shape")
        if type(value.get("version")) is not int or (
            value.get("version") != 1 or value.get("operation") != "execute"
        ):
            raise ControlLedgerConflict("unsupported Work effect payload version/operation")
        if value.get("ownership") != "managed":
            raise ControlLedgerConflict("invalid Work effect ownership")
        raw_requirements = value.get("requirements")
        if not isinstance(raw_requirements, dict):
            raise ControlLedgerConflict("invalid Work effect requirements")
        try:
            requirements = ProviderRequirements.from_dict(raw_requirements)
            payload = cls(
                provider=value.get("provider"),  # type: ignore[arg-type]
                task=value.get("task"),  # type: ignore[arg-type]
                title=value.get("title"),  # type: ignore[arg-type]
                project_id=value.get("project_id"),  # type: ignore[arg-type]
                session_id=value.get("session_id"),  # type: ignore[arg-type]
                turn_id=value.get("turn_id"),  # type: ignore[arg-type]
                mode=value.get("mode"),  # type: ignore[arg-type]
                requirements=requirements,
            )
        except (TypeError, ValueError) as exc:
            raise ControlLedgerConflict(f"invalid Work effect payload: {exc}") from exc
        # ProviderRequirements.from_dict deliberately ignores unknown fields
        # for compatibility. Durable authority cannot: require the exact
        # canonical representation accepted by this payload version.
        if payload.to_payload() != dict(value):
            raise ControlLedgerConflict("Work effect payload is not canonical")
        return payload


@dataclass(frozen=True, slots=True)
class WorkEffectPayloadV2:
    """Runtime-ready accepted Work assignment with bounded source context."""

    provider: str
    task: str
    title: str
    project_id: str
    session_id: str
    utterance_id: str
    turn_id: str
    source_user_text: str
    source_user_context: str
    source_context_scope: str
    payload_continuity: Literal["current_turn", "confirmed_prior_request"]
    requirements: ProviderRequirements
    mode: str = "agent"

    def __post_init__(self) -> None:
        provider = _required(self.provider, "provider", limit=120).lower()
        task = _required(self.task, "task")
        title = _required(self.title, "title", limit=240)
        project_id = _required(self.project_id, "project_id", limit=240)
        session_id = _required(self.session_id, "session_id", limit=240)
        utterance_id = _required(self.utterance_id, "utterance_id", limit=240)
        turn_id = _required(self.turn_id, "turn_id", limit=240)
        source_user_text = _required(
            self.source_user_text,
            "source_user_text",
        )[:4000]
        source_user_context = "\n".join(
            line.strip()
            for line in str(self.source_user_context or "").splitlines()
            if line.strip()
        )[:2000]
        source_context_scope = _required(
            self.source_context_scope,
            "source_context_scope",
            limit=800,
        )
        if source_context_scope != "chat:" + session_id:
            raise ValueError("source_context_scope must identify the originating Chat Session")
        if self.payload_continuity not in {
            "current_turn",
            "confirmed_prior_request",
        }:
            raise ValueError("invalid Work effect payload continuity")
        mode = _required(self.mode, "mode", limit=80).lower()
        _canonical_requirements(self.requirements, owner_label="C2")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "task", task)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "utterance_id", utterance_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "source_user_text", source_user_text)
        object.__setattr__(self, "source_user_context", source_user_context)
        object.__setattr__(self, "source_context_scope", source_context_scope)
        object.__setattr__(self, "mode", mode)

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": 2,
            "operation": "execute",
            "provider": self.provider,
            "task": self.task,
            "title": self.title,
            "mode": self.mode,
            "ownership": "managed",
            "project_id": self.project_id,
            "session_id": self.session_id,
            "utterance_id": self.utterance_id,
            "turn_id": self.turn_id,
            "source_user_text": self.source_user_text,
            "source_user_context": self.source_user_context,
            "source_context_scope": self.source_context_scope,
            "payload_continuity": self.payload_continuity,
            "requirements": self.requirements.to_dict(),
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "WorkEffectPayloadV2":
        if not isinstance(value, Mapping) or set(value) != _PAYLOAD_V2_KEYS:
            raise ControlLedgerConflict("invalid Work effect v2 payload shape")
        if type(value.get("version")) is not int or (
            value.get("version") != 2 or value.get("operation") != "execute"
        ):
            raise ControlLedgerConflict("unsupported Work effect v2 version/operation")
        if value.get("ownership") != "managed":
            raise ControlLedgerConflict("invalid Work effect v2 ownership")
        raw_requirements = value.get("requirements")
        if not isinstance(raw_requirements, dict):
            raise ControlLedgerConflict("invalid Work effect v2 requirements")
        try:
            payload = cls(
                provider=value.get("provider"),  # type: ignore[arg-type]
                task=value.get("task"),  # type: ignore[arg-type]
                title=value.get("title"),  # type: ignore[arg-type]
                project_id=value.get("project_id"),  # type: ignore[arg-type]
                session_id=value.get("session_id"),  # type: ignore[arg-type]
                utterance_id=value.get("utterance_id"),  # type: ignore[arg-type]
                turn_id=value.get("turn_id"),  # type: ignore[arg-type]
                source_user_text=value.get("source_user_text"),  # type: ignore[arg-type]
                source_user_context=value.get("source_user_context"),  # type: ignore[arg-type]
                source_context_scope=value.get("source_context_scope"),  # type: ignore[arg-type]
                payload_continuity=value.get("payload_continuity"),  # type: ignore[arg-type]
                mode=value.get("mode"),  # type: ignore[arg-type]
                requirements=ProviderRequirements.from_dict(raw_requirements),
            )
        except (TypeError, ValueError) as exc:
            raise ControlLedgerConflict(f"invalid Work effect v2 payload: {exc}") from exc
        if payload.to_payload() != dict(value):
            raise ControlLedgerConflict("Work effect v2 payload is not canonical")
        return payload


@dataclass(frozen=True, slots=True)
class WorkEffectPayloadV3:
    """Current-turn new Work whose task is one admission-bound exact span."""

    provider: str
    task: str
    title: str
    project_id: str
    session_id: str
    utterance_id: str
    turn_id: str
    source_user_text: str
    source_user_context: str
    source_context_scope: str
    source_proof: CurrentTurnSourceSpanV1
    requirements: ProviderRequirements
    mode: str = "agent"
    external_export_target: str = ""

    def __post_init__(self) -> None:
        if self.external_export_target not in ("", "desktop"):
            raise ValueError("unsupported Work external export target")
        provider = _required(self.provider, "provider", limit=120).lower()
        task = _strict_trimmed(self.task, "task")
        title = _required(self.title, "title", limit=240)
        project_id = _required(self.project_id, "project_id", limit=240)
        session_id = _required(self.session_id, "session_id", limit=240)
        utterance_id = _required(self.utterance_id, "utterance_id", limit=240)
        turn_id = _required(self.turn_id, "turn_id", limit=240)
        source_user_text = _exact_source_user_text(self.source_user_text)
        source_user_context = "\n".join(
            line.strip()
            for line in str(self.source_user_context or "").splitlines()
            if line.strip()
        )[:2000]
        source_context_scope = _required(
            self.source_context_scope,
            "source_context_scope",
            limit=800,
        )
        if source_context_scope != "chat:" + session_id:
            raise ValueError("source_context_scope must identify the originating Chat Session")
        if not isinstance(self.source_proof, CurrentTurnSourceSpanV1):
            raise TypeError("source_proof must use CurrentTurnSourceSpanV1")
        if self.source_proof.selected_text(source_user_text) != task:
            raise ValueError("task must equal the selected current-turn source span")
        mode = _required(self.mode, "mode", limit=80).lower()
        _canonical_requirements(self.requirements, owner_label="source-bound")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "task", task)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "utterance_id", utterance_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "source_user_text", source_user_text)
        object.__setattr__(self, "source_user_context", source_user_context)
        object.__setattr__(self, "source_context_scope", source_context_scope)
        object.__setattr__(self, "mode", mode)

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": 3,
            "operation": "execute",
            "provider": self.provider,
            "task": self.task,
            "title": self.title,
            "mode": self.mode,
            "ownership": "managed",
            "project_id": self.project_id,
            "session_id": self.session_id,
            "utterance_id": self.utterance_id,
            "turn_id": self.turn_id,
            "source_user_text": self.source_user_text,
            "source_user_context": self.source_user_context,
            "source_context_scope": self.source_context_scope,
            "source_proof": self.source_proof.to_payload(),
            "requirements": self.requirements.to_dict(),
            **({"external_export_target": self.external_export_target}
                if self.external_export_target else {}),
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "WorkEffectPayloadV3":
        if not _work_payload_shape(value, _PAYLOAD_V3_KEYS):
            raise ControlLedgerConflict("invalid Work effect v3 payload shape")
        if type(value.get("version")) is not int or (
            value.get("version") != 3 or value.get("operation") != "execute"
        ):
            raise ControlLedgerConflict("unsupported Work effect v3 version/operation")
        if value.get("ownership") != "managed":
            raise ControlLedgerConflict("invalid Work effect v3 ownership")
        raw_requirements = value.get("requirements")
        raw_source_proof = value.get("source_proof")
        if not isinstance(raw_requirements, dict):
            raise ControlLedgerConflict("invalid Work effect v3 requirements")
        if not isinstance(raw_source_proof, dict):
            raise ControlLedgerConflict("invalid Work effect v3 source proof")
        try:
            payload = cls(
                provider=value.get("provider"),  # type: ignore[arg-type]
                task=value.get("task"),  # type: ignore[arg-type]
                title=value.get("title"),  # type: ignore[arg-type]
                project_id=value.get("project_id"),  # type: ignore[arg-type]
                session_id=value.get("session_id"),  # type: ignore[arg-type]
                utterance_id=value.get("utterance_id"),  # type: ignore[arg-type]
                turn_id=value.get("turn_id"),  # type: ignore[arg-type]
                source_user_text=value.get("source_user_text"),  # type: ignore[arg-type]
                source_user_context=value.get("source_user_context"),  # type: ignore[arg-type]
                source_context_scope=value.get("source_context_scope"),  # type: ignore[arg-type]
                source_proof=CurrentTurnSourceSpanV1.from_payload(raw_source_proof),
                mode=value.get("mode"),  # type: ignore[arg-type]
                requirements=ProviderRequirements.from_dict(raw_requirements),
                external_export_target=value.get("external_export_target", ""),
            )
        except (TypeError, ValueError) as exc:
            raise ControlLedgerConflict(f"invalid Work effect v3 payload: {exc}") from exc
        if payload.to_payload() != dict(value):
            raise ControlLedgerConflict("Work effect v3 payload is not canonical")
        return payload


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkAmendPayloadV4(WorkEffectPayloadV3):
    """Current-source instruction for one existing Work; its goal stays intact."""

    work_item_id: str

    def __post_init__(self) -> None:
        WorkEffectPayloadV3.__post_init__(self)
        _strict_trimmed(self.work_item_id, "work_item_id", limit=240)

    def to_payload(self) -> dict[str, Any]:
        return {**WorkEffectPayloadV3.to_payload(self), "version": 4,
                "operation": "amend", "work_item_id": self.work_item_id}

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "WorkAmendPayloadV4":
        if not _work_payload_shape(value, _PAYLOAD_V3_KEYS | {"work_item_id"}):
            raise ControlLedgerConflict("invalid Work amend v4 payload shape")
        if type(value.get("version")) is not int or value["version"] != 4 or value.get("operation") != "amend":
            raise ControlLedgerConflict("unsupported Work amend v4 version/operation")
        base_value = {k: v for k, v in value.items() if k != "work_item_id"}
        base_value.update(version=3, operation="execute")
        base = WorkEffectPayloadV3.from_payload(base_value)
        try:
            result = cls(**{f.name: getattr(base, f.name) for f in fields(WorkEffectPayloadV3)},
                         work_item_id=value["work_item_id"])
        except (TypeError, ValueError) as exc:
            raise ControlLedgerConflict(f"invalid Work amend target: {exc}") from exc
        if result.to_payload() != dict(value):
            raise ControlLedgerConflict("Work amend v4 payload is not canonical")
        return result


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkContextPayloadV5(WorkEffectPayloadV3):
    """An explicit recipient independent of the new/existing Work target.

    The address is a Host Attempt record, never a caller-supplied native id.
    Empty work_item_id means a new goal. v3/v4 records keep their exact shape.
    """

    context_attempt_id: str
    work_item_id: str = ""

    def __post_init__(self) -> None:
        WorkEffectPayloadV3.__post_init__(self)
        _strict_trimmed(self.context_attempt_id, "context_attempt_id", limit=240)
        if self.work_item_id != "":
            _strict_trimmed(self.work_item_id, "work_item_id", limit=240)

    def to_payload(self) -> dict[str, Any]:
        return {**WorkEffectPayloadV3.to_payload(self), "version": 5,
                "operation": "amend" if self.work_item_id else "execute",
                "work_item_id": self.work_item_id, "context_attempt_id": self.context_attempt_id}

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "WorkContextPayloadV5":
        if not _work_payload_shape(value, _PAYLOAD_V3_KEYS | {"work_item_id", "context_attempt_id"}):
            raise ControlLedgerConflict("invalid Work context v5 payload shape")
        if type(value.get("version")) is not int or value["version"] != 5:
            raise ControlLedgerConflict("unsupported Work context payload version")
        base_value = {k:v for k,v in value.items() if k not in {"work_item_id", "context_attempt_id"}}
        base_value.update(version=3, operation="execute")
        base = WorkEffectPayloadV3.from_payload(base_value)
        try:
            result = cls(**{f.name:getattr(base, f.name) for f in fields(WorkEffectPayloadV3)},
                work_item_id=value["work_item_id"], context_attempt_id=value["context_attempt_id"])
        except (TypeError, ValueError) as exc:
            raise ControlLedgerConflict(f"invalid Work context reference: {exc}") from exc
        if result.to_payload() != dict(value):
            raise ControlLedgerConflict("Work context v5 payload is not canonical")
        return result


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkCooperativeContextPayloadV6(WorkEffectPayloadV3):
    """A Work effect addressed to one Host-owned cooperative context."""

    cooperative_context_id: str
    cooperative_binding_token: str
    cooperative_context_revision: int
    work_item_id: str = ""

    def __post_init__(self) -> None:
        WorkEffectPayloadV3.__post_init__(self)
        _strict_trimmed(self.cooperative_context_id,
            "cooperative_context_id", limit=240)
        _strict_trimmed(self.cooperative_binding_token,
            "cooperative_binding_token", limit=240)
        if (isinstance(self.cooperative_context_revision, bool)
                or not isinstance(self.cooperative_context_revision, int)
                or self.cooperative_context_revision < 0):
            raise ValueError("cooperative_context_revision must be a non-negative integer")
        if self.work_item_id != "":
            _strict_trimmed(self.work_item_id, "work_item_id", limit=240)

    def to_payload(self) -> dict[str, Any]:
        return {**WorkEffectPayloadV3.to_payload(self), "version":6,
            "operation":"amend" if self.work_item_id else "execute",
            "work_item_id":self.work_item_id,
            "cooperative_context_id":self.cooperative_context_id,
            "cooperative_binding_token":self.cooperative_binding_token,
            "cooperative_context_revision":self.cooperative_context_revision}

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "WorkCooperativeContextPayloadV6":
        extra = {"work_item_id", "cooperative_context_id",
            "cooperative_binding_token", "cooperative_context_revision"}
        if not _work_payload_shape(value, _PAYLOAD_V3_KEYS | extra):
            raise ControlLedgerConflict("invalid cooperative Work context v6 payload shape")
        if type(value.get("version")) is not int or value["version"] != 6:
            raise ControlLedgerConflict("unsupported cooperative Work context version")
        base_value = {key:item for key, item in value.items() if key not in extra}
        base_value.update(version=3, operation="execute")
        base = WorkEffectPayloadV3.from_payload(base_value)
        try:
            result = cls(**{field.name:getattr(base, field.name)
                for field in fields(WorkEffectPayloadV3)},
                work_item_id=value["work_item_id"],
                cooperative_context_id=value["cooperative_context_id"],
                cooperative_binding_token=value["cooperative_binding_token"],
                cooperative_context_revision=value["cooperative_context_revision"])
        except (TypeError, ValueError) as exc:
            raise ControlLedgerConflict(
                f"invalid cooperative Work context reference: {exc}") from exc
        if result.to_payload() != dict(value):
            raise ControlLedgerConflict("cooperative Work context v6 payload is not canonical")
        return result


WorkEffectPayload = (WorkEffectPayloadV1 | WorkEffectPayloadV2 | WorkEffectPayloadV3
    | WorkAmendPayloadV4 | WorkContextPayloadV5 | WorkCooperativeContextPayloadV6)
RuntimeWorkEffectPayload = (WorkEffectPayloadV2 | WorkEffectPayloadV3
    | WorkAmendPayloadV4 | WorkContextPayloadV5 | WorkCooperativeContextPayloadV6)


def _payload_work_id(payload: WorkEffectPayload) -> str:
    return payload.work_item_id if isinstance(payload, (
        WorkAmendPayloadV4, WorkContextPayloadV5,
        WorkCooperativeContextPayloadV6)) else ""


def _payload_operation(payload: WorkEffectPayload) -> str:
    return "amend" if _payload_work_id(payload) else "execute"


def _canonical_work_title(payload: WorkEffectPayload) -> str:
    return ProviderEventIngestor.work_item_title(
        payload.task,
        "" if _payload_work_id(payload) else payload.title,
    )


def _payload_target_key(payload: WorkEffectPayload) -> str:
    work_item_id = _payload_work_id(payload)
    if work_item_id:
        return "work-item:" + work_item_id
    source_id = str(getattr(payload, "utterance_id", "") or payload.turn_id)
    return f"work-source:{payload.session_id}:{source_id}"


def _payload_target_keys(payload: WorkEffectPayload) -> set[str]:
    current = _payload_target_key(payload)
    if _payload_work_id(payload):
        return {current}
    # Project-scoped target keys were emitted before new-Work identity was
    # separated from placement. They remain valid for replay/reconciliation,
    # but newly sealed effects use the exact admitted source identity.
    return {current, "work-project:" + payload.project_id}


def _batch_target_key(
    root_id: str,
    ordinal: int,
    payload: WorkEffectPayloadV3,
) -> str:
    """One provisional new-Work identity per independently sourced operation."""

    work_item_id = _payload_work_id(payload)
    if work_item_id:
        return "work-item:" + work_item_id
    source_identity = json.dumps(
        [
            root_id,
            ordinal,
            payload.session_id,
            payload.utterance_id,
            payload.source_proof.selected_text_sha256,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "work-source:" + hashlib.sha256(source_identity.encode("utf-8")).hexdigest()


def _decode_work_effect_payload(value: Mapping[str, Any]) -> WorkEffectPayload:
    version = value.get("version") if isinstance(value, Mapping) else None
    if type(version) is not int:
        raise ControlLedgerConflict("unsupported Work effect payload version")
    if version == 1:
        return WorkEffectPayloadV1.from_payload(value)
    if version == 2:
        return WorkEffectPayloadV2.from_payload(value)
    if version == 3:
        return WorkEffectPayloadV3.from_payload(value)
    if version == 4:
        return WorkAmendPayloadV4.from_payload(value)
    if version == 5:
        return WorkContextPayloadV5.from_payload(value)
    if version == 6:
        return WorkCooperativeContextPayloadV6.from_payload(value)
    raise ControlLedgerConflict("unsupported Work effect payload version")


def _payload_version(payload: WorkEffectPayload) -> int:
    if isinstance(payload, WorkCooperativeContextPayloadV6):
        return 6
    if isinstance(payload, WorkContextPayloadV5):
        return 5
    if isinstance(payload, WorkAmendPayloadV4):
        return 4
    if isinstance(payload, WorkEffectPayloadV3):
        return 3
    if isinstance(payload, WorkEffectPayloadV2):
        return 2
    return 1


def _payload_evidence_adapter(payload: WorkEffectPayload) -> str:
    if isinstance(payload, WorkCooperativeContextPayloadV6):
        return "proposal_gated_cooperative_context_work:v6"
    if isinstance(payload, WorkContextPayloadV5):
        return "proposal_gated_explicit_context_work:v5"
    if isinstance(payload, WorkAmendPayloadV4):
        return "proposal_gated_current_turn_amend:v4"
    if isinstance(payload, WorkEffectPayloadV3):
        return "proposal_gated_current_turn_work:v3"
    if isinstance(payload, WorkEffectPayloadV2):
        return "proposal_gated_work_c2:v2"
    return "proposal_gated_work_c1:v1"


def _work_has_no_workspace(payload: RuntimeWorkEffectPayload) -> bool:
    return (payload.requirements.workspace_access == "none"
        and payload.requirements.workspace_ownership == "none")


def _payload_continuity(payload: RuntimeWorkEffectPayload) -> str:
    return (
        payload.payload_continuity
        if isinstance(payload, WorkEffectPayloadV2)
        else "current_turn"
    )


class WorkControl:
    """Seal one proposal-gated Work plan and bind each selected effect."""

    def __init__(self, ledger: ControlLedgerStore, work: WorkLedgerStore, *,
                 cooperative_context_resolver: Callable | None = None) -> None:
        if work.db_path == ":memory:" or Path(work.db_path).resolve() != ledger.path:
            raise ControlLedgerConflict("Work domain and Control Ledger must share one database")
        self.ledger = ledger
        self.work = work
        self.cooperative_context_resolver = cooperative_context_resolver
        self.reconcile_intake_rejections()

    def reconcile_intake_rejections(self) -> int:
        """Recover only durable, locally rejected preparations; never submit."""
        with self.ledger._lock:
            rows = self.ledger._db.execute("""SELECT e.effect_id
                FROM control_effect_outbox e JOIN run_attempts r ON r.origin_effect_id=e.effect_id
                WHERE e.kind='work' AND e.state IN ('dispatching','running',
                    'unknown_reconciling','needs_user_decision')
                AND r.execution_status='cancelled'
                AND json_extract(r.metadata_json,'$.start_rejected') IS NOT NULL""").fetchall()
        count = 0
        for row in rows:
            try:
                self.record_intake_rejection(row["effect_id"])
            except (ControlLedgerConflict, WorkLedgerConflict):
                continue
            count += 1
        return count

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
            raise ControlLedgerConflict(
                "Work effect requires a confirmed, mode-bound Chat admission"
            )

    def admit(self, admission: TurnAdmissionRecord, *, fence_scope: str) -> dict[str, Any]:
        """Persist an externally issued epoch; never choose authority mode."""

        self._validate_admission(admission)
        return self.ledger.admit(
            root_id=admission.root_id,
            source_scope=admission.dialogue_source_scope,
            fence_scope=_required(fence_scope, "fence_scope", limit=240),
            utterance_id=admission.utterance_id,
            chat_epoch=admission.chat_epoch,
            authority_mode=admission.authority_mode,  # type: ignore[arg-type]
            transcript_hash=admission.transcript_hash,
        )

    @staticmethod
    def _identity(root_id: str, suffix: str) -> str:
        return uuid.uuid5(_IDENTITY_NAMESPACE, json.dumps([root_id, suffix])).hex

    @classmethod
    def initial_work_item_id(cls, effect_id: str) -> str:
        """A retry/competing intake derives the same new Work and Draft address."""
        return "work_" + cls._identity(effect_id, "work-item")

    def _bound_admission(self, admission: TurnAdmissionRecord) -> dict[str, Any]:
        self._validate_admission(admission)
        stored = self.ledger.get_admission(admission.root_id)
        if (
            stored["source_scope"],
            stored["utterance_id"],
            stored["authority_mode"],
            stored["transcript_hash"],
            stored["chat_epoch"],
        ) != (
            admission.dialogue_source_scope,
            admission.utterance_id,
            admission.authority_mode,
            admission.transcript_hash,
            admission.chat_epoch,
        ):
            raise ControlLedgerConflict("Work effect source does not match durable admission")
        return stored

    def _amend_target(self, payload: WorkAmendPayloadV4 | WorkContextPayloadV5
                      | WorkCooperativeContextPayloadV6):
        item = self.work.get_work_item(payload.work_item_id)
        if item is None:
            raise WorkLedgerNotFound(f"unknown amend Work: {payload.work_item_id}")
        if item.project_id != payload.project_id:
            raise WorkLedgerConflict("amend target belongs to another Project")
        return item

    def _validate_amend_start(self, payload: WorkAmendPayloadV4 | WorkContextPayloadV5
                              | WorkCooperativeContextPayloadV6):
        item = self._amend_target(payload)
        if item.workspace_mode != "local" or item.state == "archived":
            raise WorkLedgerConflict("accepted amend currently requires an available local Work")
        attempts = self.work.list_attempts(item.work_item_id)
        if not attempts or any(a.execution_status not in {"succeeded", "failed", "cancelled"} for a in attempts):
            raise WorkLedgerConflict("accepted amend requires settled predecessor attempts")
        # Work's existing intake/export owners validate and carry forward its
        # obligations. Their presence does not make a source-bound amend invalid.
        return item

    def _context_attachment(self, payload: WorkContextPayloadV5
                            | WorkCooperativeContextPayloadV6, *, cursor=None,
                            workspace_path="", require_current_binding=False
                            ) -> ProviderSessionAttachment:
        if isinstance(payload, WorkCooperativeContextPayloadV6):
            if self.cooperative_context_resolver is None:
                raise WorkLedgerConflict(
                    "cooperative Work recipient owner is unavailable")
            attachment = self.cooperative_context_resolver(payload,
                cursor=cursor, workspace_path=workspace_path,
                require_current_binding=require_current_binding)
            if not isinstance(attachment, ProviderSessionAttachment):
                raise WorkLedgerConflict(
                    "cooperative Work recipient did not return a typed attachment")
            return attachment
        if cursor is None:
            attempt = self.work.get_attempt(payload.context_attempt_id)
            item = self.work.get_work_item(attempt.work_item_id) if attempt else None
        else:
            row = cursor.execute("SELECT * FROM run_attempts WHERE attempt_id=?", (payload.context_attempt_id,)).fetchone()
            attempt = self.work._attempt_from_row(row) if row else None
            row = cursor.execute("SELECT * FROM work_items WHERE work_item_id=?", (attempt.work_item_id,)).fetchone() if attempt else None
            item = self.work._work_item_from_row(row) if row else None
        if attempt is None or item is None:
            raise WorkLedgerConflict("explicit Provider recipient is no longer available")
        if attempt.execution_status not in {"succeeded", "failed", "cancelled"}:
            raise WorkLedgerConflict("explicit Provider recipient must have a settled Attempt")
        if item.state == "archived" or item.workspace_mode != "local" or item.project_id != payload.project_id:
            raise WorkLedgerConflict("explicit Provider recipient must remain in the same available local Project")
        if workspace_path and Path(item.workspace_path).resolve() != Path(workspace_path).resolve():
            raise WorkLedgerConflict("explicit Provider recipient cannot change execution workspace in this slice")
        try:
            session = ProviderSessionHandle.from_dict(attempt.metadata.get("provider_session"))
        except (TypeError, ValueError) as exc:
            raise WorkLedgerConflict("explicit Provider recipient has no valid native context") from exc
        if session.provider != payload.provider or attempt.provider != payload.provider or session.scope != "interaction":
            raise WorkLedgerConflict("Provider recipient does not permit explicit context rebinding")
        return ProviderSessionAttachment(session=session, audit={"state":"explicit_recipient",
            "provider":session.provider, "context_attempt_id":attempt.attempt_id})

    def addressed_context(self, authority: ProviderRunIntakeAuthority) -> ProviderSessionAttachment | None:
        """Resolve the separately accepted recipient; the request cannot author it."""
        _, payload = self._validate_effect(authority.effect_id)
        return self._context_attachment(payload) if isinstance(payload, (
            WorkContextPayloadV5, WorkCooperativeContextPayloadV6)) else None

    @staticmethod
    def _validate_payload_for_admission(
        admission: TurnAdmissionRecord,
        stored: Mapping[str, Any],
        payload: WorkEffectPayload,
    ) -> None:
        if not isinstance(
            payload,
            (WorkEffectPayloadV1, WorkEffectPayloadV2, WorkEffectPayloadV3),
        ):
            raise TypeError("Work effect seal requires a typed Work effect payload")
        if payload.session_id != admission.session_id or payload.turn_id != admission.turn_id:
            raise ControlLedgerConflict("Work effect source identity does not match admission")
        if isinstance(payload, (WorkEffectPayloadV2, WorkEffectPayloadV3)):
            if (
                payload.utterance_id != admission.utterance_id
                or admission_transcript_hash(payload.source_user_text)
                != admission.transcript_hash
            ):
                raise ControlLedgerConflict(
                    "Work effect transcript identity does not match admission"
                )
            if payload.title != _canonical_work_title(payload):
                raise ControlLedgerConflict("Work effect title is not canonical")
        if isinstance(payload, WorkEffectPayloadV3):
            try:
                selected = payload.source_proof.validate_identity(
                    root_id=admission.root_id,
                    source_scope=admission.dialogue_source_scope,
                    utterance_id=admission.utterance_id,
                    transcript_hash=admission.transcript_hash,
                    source_user_text=payload.source_user_text,
                )
            except ValueError as exc:
                raise ControlLedgerConflict(
                    f"Work effect source proof does not match admission: {exc}"
                ) from exc
            if selected != payload.task:
                raise ControlLedgerConflict(
                    "Work effect task does not match its selected current source"
                )
        if isinstance(payload, WorkEffectPayloadV2) and stored["plan_id"] is None:
            raise ControlLedgerConflict(
                "new Runtime Work effects require current-turn source proof v3"
            )

    def _validate_new_plan_destination(self, payload: WorkEffectPayload) -> None:
        project = self.work.get_project(payload.project_id)
        if project is None:
            raise WorkLedgerNotFound(f"unknown project: {payload.project_id}")
        if project.state != "active":
            raise WorkLedgerConflict("Work effect Project is not active")
        if _payload_work_id(payload):
            self._validate_amend_start(payload)  # type: ignore[arg-type]
        if isinstance(payload, (WorkContextPayloadV5, WorkCooperativeContextPayloadV6)):
            self._context_attachment(payload, require_current_binding=True)

    @staticmethod
    def _validate_batch_payloads(payloads: tuple[WorkEffectPayload, ...]) -> None:
        if not payloads:
            raise ControlLedgerConflict("Work plan requires at least one effect")
        if len(payloads) == 1:
            return
        if not all(isinstance(payload, WorkEffectPayloadV3) for payload in payloads):
            raise ControlLedgerConflict(
                "multi-effect Work plans require current-source payload v3 or later"
            )
        source_identity = {
            (
                payload.session_id,
                payload.turn_id,
                payload.utterance_id,
                payload.source_user_text,
                payload.source_context_scope,
            )
            for payload in payloads
        }
        if len(source_identity) != 1:
            raise ControlLedgerConflict(
                "multi-effect Work plan payloads must share one admitted source"
            )
        spans = sorted(
            (payload.source_proof.start, payload.source_proof.end)
            for payload in payloads
        )
        if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
            raise ControlLedgerConflict(
                "multi-effect Work plan source selections must be disjoint"
            )
        encoded_payloads = {
            json.dumps(payload.to_payload(), ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"))
            for payload in payloads
        }
        if len(encoded_payloads) != len(payloads):
            raise ControlLedgerConflict(
                "multi-effect Work plan payload identities must be distinct"
            )
        existing_work_ids = [
            _payload_work_id(payload) for payload in payloads
            if _payload_work_id(payload)
        ]
        if len(set(existing_work_ids)) != len(existing_work_ids):
            raise ControlLedgerConflict(
                "one immutable Work plan cannot mutate the same existing Work twice"
            )

    @classmethod
    def _plan_id(
        cls,
        root_id: str,
        payloads: tuple[WorkEffectPayload, ...],
    ) -> str:
        if len(payloads) == 1:
            return cls._identity(root_id, f"work-plan:v{_payload_version(payloads[0])}")
        versions = ",".join(str(_payload_version(payload)) for payload in payloads)
        return cls._identity(root_id, f"work-plan:batch:v1:{versions}")

    @classmethod
    def _effect_ids(cls, root_id: str, count: int) -> tuple[str, ...]:
        return tuple(cls._identity(root_id, f"work:{ordinal}") for ordinal in range(count))

    def seal_many(
        self,
        admission: TurnAdmissionRecord,
        payloads: tuple[WorkEffectPayload, ...],
        *,
        plan_evidence: Mapping[str, Any] | None = None,
        local_apply: Callable | None = None,
    ) -> dict[str, Any]:
        """Atomically accept one ordered, immutable plan of grounded Work effects."""

        if not isinstance(payloads, tuple):
            raise TypeError("Work plan payloads must be supplied as one immutable tuple")
        stored = self._bound_admission(admission)
        if stored["authority_mode"] != "turn_decision":
            raise ControlLedgerConflict("legacy admission cannot seal a new-mode Work effect")
        self._validate_batch_payloads(payloads)
        for payload in payloads:
            self._validate_payload_for_admission(admission, stored, payload)
        if stored["plan_id"] is None:
            for payload in payloads:
                self._validate_new_plan_destination(payload)

        is_batch = len(payloads) > 1
        effect_ids = self._effect_ids(admission.root_id, len(payloads))
        plan_id = self._plan_id(admission.root_id, payloads)
        target_keys = tuple(
            _batch_target_key(admission.root_id, ordinal, payload)  # type: ignore[arg-type]
            if is_batch else _payload_target_key(payload)
            for ordinal, payload in enumerate(payloads)
        )
        if stored["plan_id"] == plan_id:
            retained_keys = []
            for ordinal, (effect_id, payload) in enumerate(zip(effect_ids, payloads)):
                retained = self.ledger.get_effect(effect_id)
                compatible_targets = (
                    {_batch_target_key(admission.root_id, ordinal, payload)}
                    if is_batch else _payload_target_keys(payload)
                )
                if (retained["kind"] != "work"
                        or retained["target_key"] not in compatible_targets):
                    raise ControlLedgerConflict(
                        "retained Work effect target is not compatible with its payload"
                    )
                retained_keys.append(retained["target_key"])
            target_keys = tuple(retained_keys)

        extra_evidence = dict(plan_evidence or {})
        if {"adapter", "transcript_hash"}.intersection(extra_evidence):
            raise ControlLedgerConflict(
                "additional Work plan evidence cannot replace source authority"
            )
        for payload in payloads:
            requirement = self._cooperative_outcome_requirement(payload, extra_evidence)
            if is_batch and requirement is not None:
                raise ControlLedgerConflict(
                    "single-Work outcome evidence cannot authorize a multi-effect plan"
                )
        accepted = self.ledger.accept(
            admission.root_id,
            chat_epoch=stored["chat_epoch"],
            plan_id=plan_id,
            effects=tuple(
                ControlEffect(
                    effect_id,
                    "work",
                    target_key,
                    payload.to_payload(),
                )
                for effect_id, target_key, payload in zip(
                    effect_ids, target_keys, payloads
                )
            ),
            evidence={
                "adapter": (
                    _BATCH_EVIDENCE_ADAPTER
                    if is_batch else _payload_evidence_adapter(payloads[0])
                ),
                "transcript_hash": admission.transcript_hash,
                **extra_evidence,
            },
            local_apply=local_apply,
        )
        return {**accepted, "effect_ids": effect_ids}

    def seal(
        self,
        admission: TurnAdmissionRecord,
        payload: WorkEffectPayload,
        *,
        plan_evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compatibility port for one effect through the shared plan sealer."""

        accepted = self.seal_many(
            admission,
            (payload,),
            plan_evidence=plan_evidence,
        )
        effect_id = accepted["effect_ids"][0]
        return {
            key: value for key, value in accepted.items() if key != "effect_ids"
        } | {"effect_id": effect_id}

    def _validate_effect(self, effect_id: str) -> tuple[dict[str, Any], WorkEffectPayload]:
        clean_effect_id = _required(effect_id, "effect_id", limit=240)
        if clean_effect_id != effect_id:
            raise ControlLedgerConflict("Work effect identity is not canonical")
        effect = self.ledger.get_effect(clean_effect_id)
        try:
            raw_payload = json.loads(effect["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ControlLedgerConflict("Work effect payload is not valid JSON") from exc
        if not isinstance(raw_payload, dict):
            raise ControlLedgerConflict("Work effect payload is not one JSON object")
        payload = _decode_work_effect_payload(raw_payload)
        if effect["kind"] != "work":
            raise ControlLedgerConflict("effect is not a supported Work effect")
        admission = self.ledger.get_admission(effect["root_id"])
        try:
            accepted_plan = json.loads(str(admission["plan_json"] or ""))
        except json.JSONDecodeError as exc:
            raise ControlLedgerConflict("Work effect has no canonical accepted plan") from exc
        if (
            not isinstance(accepted_plan, dict)
            or set(accepted_plan) != {"effects", "evidence"}
            or not isinstance(accepted_plan.get("effects"), list)
            or not accepted_plan["effects"]
            or not isinstance(accepted_plan.get("evidence"), dict)
        ):
            raise ControlLedgerConflict("Work effect has no canonical accepted plan")
        plan_effects = accepted_plan["effects"]
        evidence = accepted_plan["evidence"]
        decoded_payloads: list[WorkEffectPayload] = []
        for ordinal, member in enumerate(plan_effects):
            if (
                not isinstance(member, dict)
                or set(member) != {"effect_id", "kind", "target_key", "payload"}
                or member.get("kind") != "work"
                or not isinstance(member.get("payload"), dict)
            ):
                raise ControlLedgerConflict("accepted Work plan membership is not canonical")
            candidate = _decode_work_effect_payload(member["payload"])
            if member["effect_id"] != self._identity(effect["root_id"], f"work:{ordinal}"):
                raise ControlLedgerConflict(
                    "accepted Work plan effect identity does not belong to its admitted source"
                )
            expected_targets = (
                {_batch_target_key(effect["root_id"], ordinal, candidate)}
                if len(plan_effects) > 1 and isinstance(candidate, WorkEffectPayloadV3)
                else _payload_target_keys(candidate)
            )
            if member["target_key"] not in expected_targets:
                raise ControlLedgerConflict("accepted Work plan target identity is invalid")
            if isinstance(candidate, (WorkEffectPayloadV2, WorkEffectPayloadV3)):
                if (
                    admission["utterance_id"] != candidate.utterance_id
                    or admission["transcript_hash"]
                    != admission_transcript_hash(candidate.source_user_text)
                    or candidate.title != _canonical_work_title(candidate)
                ):
                    raise ControlLedgerConflict(
                        "accepted Work effect source facts are no longer canonical"
                    )
            if isinstance(candidate, WorkEffectPayloadV3):
                try:
                    selected = candidate.source_proof.validate_identity(
                        root_id=effect["root_id"],
                        source_scope=admission["source_scope"],
                        utterance_id=admission["utterance_id"],
                        transcript_hash=admission["transcript_hash"],
                        source_user_text=candidate.source_user_text,
                    )
                except ValueError as exc:
                    raise ControlLedgerConflict(
                        f"accepted Work effect source proof is invalid: {exc}"
                    ) from exc
                if selected != candidate.task:
                    raise ControlLedgerConflict(
                        "accepted Work effect task is outside its source selection"
                    )
            decoded_payloads.append(candidate)
        frozen_payloads = tuple(decoded_payloads)
        self._validate_batch_payloads(frozen_payloads)
        is_batch = len(frozen_payloads) > 1
        expected_adapter = (
            _BATCH_EVIDENCE_ADAPTER
            if is_batch else _payload_evidence_adapter(frozen_payloads[0])
        )
        for candidate in frozen_payloads:
            requirement = self._cooperative_outcome_requirement(candidate, evidence)
            if is_batch and requirement is not None:
                raise ControlLedgerConflict(
                    "single-Work outcome evidence cannot authorize a multi-effect plan"
                )
        ordinal = effect["ordinal"]
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or not 0 <= ordinal < len(plan_effects)
        ):
            raise ControlLedgerConflict("Work effect ordinal is outside its accepted plan")
        if (
            admission["authority_mode"] != "turn_decision"
            or admission["source_scope"] != "chat:" + payload.session_id
            or admission["plan_id"] != self._plan_id(effect["root_id"], frozen_payloads)
            or evidence.get("adapter") != expected_adapter
            or evidence.get("transcript_hash") != admission["transcript_hash"]
            or clean_effect_id != self._identity(effect["root_id"], f"work:{ordinal}")
            or plan_effects[ordinal]
            != {
                "effect_id": clean_effect_id,
                "kind": "work",
                "target_key": effect["target_key"],
                "payload": payload.to_payload(),
            }
            or frozen_payloads[ordinal] != payload
        ):
            raise ControlLedgerConflict("Work effect does not belong to its admitted source")
        return effect, payload

    @staticmethod
    def _cooperative_outcome_requirement(payload, evidence):
        """Bind the accepted result-entry clause to its existing authoring contract."""
        prepared_work = evidence.get("auip_preparation_work_item_id")
        if prepared_work is not None:
            if (not isinstance(payload, WorkEffectPayloadV3)
                    or _payload_operation(payload) != "amend" or not prepared_work
                    or _payload_work_id(payload) != prepared_work):
                raise ControlLedgerConflict("AUIP preparation must amend its selected existing Work")
            mode = evidence.get("auip_preparation_mode")
            if mode is not None and mode not in {"observe", "collaborate", "delegate"}:
                raise ControlLedgerConflict("AUIP preparation mode is invalid")
            return auip_authoring_outcome_requirement(mode=mode or "")
        batch = evidence.get("cooperative_batch")
        if not isinstance(batch, dict) or batch.get("kind") != "work_then_auip_after_work":
            return None
        actions = batch.get("actions")
        if (not isinstance(payload, WorkEffectPayloadV3)
                or _payload_operation(payload) not in {"execute", "amend"}
                or type(batch.get("version")) is not int or batch["version"] not in {1, 2}
                or not isinstance(actions, list) or len(actions) != 2
                or not all(isinstance(action, dict) for action in actions)):
            raise ControlLedgerConflict("AUIP handoff evidence is not a supported Work plan")
        work, entry = actions
        if (work.get("op") != "work"
                or work.get("intent") != _payload_operation(payload)
                or entry.get("op") != "auip_after_work"
                or entry.get("mode") not in {"observe", "collaborate", "delegate"}):
            raise ControlLedgerConflict("AUIP handoff evidence changed its operations")
        for index, action in enumerate(actions):
            start, end = action.get("source_start"), action.get("source_end")
            if (action.get("index") != index or type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= len(payload.source_user_text)
                    or action.get("source_sha256") != hashlib.sha256(
                        payload.source_user_text[start:end].encode("utf-8")).hexdigest()):
                raise ControlLedgerConflict("AUIP handoff source evidence changed")
        entry_uses_complete_source = (
            entry["source_start"] == 0
            and entry["source_end"] == len(payload.source_user_text)
        )
        # The original batch owner supplies a later, disjoint entry clause. The
        # professional AUIP owner instead interprets the complete admitted turn
        # and passes that completed decision as Host evidence. Its rewritten
        # instruction is presentation/semantic output, never source authority.
        if (work["source_start"] != payload.source_proof.start
                or work["source_end"] != payload.source_proof.end
                or (work["source_end"] > entry["source_start"]
                    and not entry_uses_complete_source)):
            raise ControlLedgerConflict("AUIP handoff does not follow its accepted Work source")
        # Version 1 is persisted in existing admissions whose request metadata
        # contained only AUIP capability. Do not reinterpret those on replay.
        return auip_authoring_outcome_requirement(
            mode=entry["mode"] if batch["version"] == 2 else "")

    def _accepted_outcome_requirement(self, effect, payload):
        admission = self.ledger.get_admission(effect["root_id"])
        plan = json.loads(admission["plan_json"])
        return self._cooperative_outcome_requirement(payload, plan.get("evidence") or {})

    def provider_request(self, effect_id: str) -> ProviderRunRequest:
        """Reconstruct the bounded C2 Provider request from accepted facts."""

        _effect, payload = self._validate_effect(effect_id)
        if not isinstance(payload, WorkEffectPayloadV3):
            raise ControlLedgerConflict(
                "new Runtime execution requires current-turn source proof v3"
            )
        project = self.work.get_project(payload.project_id)
        if project is None:
            raise WorkLedgerNotFound(f"unknown project: {payload.project_id}")
        if project.state != "active":
            raise WorkLedgerConflict("Work effect Project is not active")
        item = self._validate_amend_start(payload) if _payload_work_id(payload) else None
        attachment = (self._context_attachment(payload)
            if isinstance(payload, WorkCooperativeContextPayloadV6) else None)
        workspace_path = (item.workspace_path if item is not None
            else str(attachment.audit.get("workspace_path") or "")
            if attachment is not None else project.canonical_path)
        if attachment is not None and not workspace_path:
            raise WorkLedgerConflict("cooperative Work recipient has no workspace")
        no_workspace = _work_has_no_workspace(payload)
        if no_workspace:
            workspace_path = ""
        metadata: dict[str, Any] = {
            "source": "control_work_effect",
            "session_id": payload.session_id,
            "turn_id": payload.turn_id,
            SOURCE_UTTERANCE_ID_METADATA_KEY: payload.utterance_id,
            "source_user_text": payload.source_user_text,
            SOURCE_CONTEXT_SCOPE_METADATA_KEY: payload.source_context_scope,
            "payload_continuity": _payload_continuity(payload),
            "intent": _payload_operation(payload),
            "continuation": "amend" if item is not None else "new",
            "write_intent": payload.requirements.workspace_access == "write",
            "provider_requirements": payload.requirements.to_dict(),
            MAIN_ROLE_NAME_METADATA_KEY: MAIN_CONVERSATION_ROLE_NAME,
            "work": {
                "project_id": payload.project_id,
                "workspace_path": workspace_path,
                "workspace_mode": "none" if no_workspace else "local",
                "title": item.title if item is not None else payload.title,
                **({"work_item_id": item.work_item_id} if item is not None else {}),
            },
        }
        if payload.task != payload.source_user_text:
            metadata["source_user_operation_text"] = payload.task
        if payload.source_user_context:
            metadata["source_user_context"] = payload.source_user_context
        if payload.external_export_target:
            metadata["external_export"] = {"target":payload.external_export_target}
        outcome_requirement = self._accepted_outcome_requirement(_effect, payload)
        if outcome_requirement is not None:
            metadata["host_outcome_requirement"] = outcome_requirement
        return ProviderRunRequest(
            provider=payload.provider,
            task=payload.task,
            cwd=None if no_workspace else workspace_path,
            mode=payload.mode,
            metadata=metadata,
            requirements=payload.requirements,
            ownership="managed",
        )

    def assert_start_allowed_without_authority(self, request: ProviderRunRequest) -> None:
        """Reject an explicitly identified new-mode source at legacy Work intake."""

        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        source_scope = str(metadata.get(SOURCE_CONTEXT_SCOPE_METADATA_KEY) or "").strip()
        utterance_id = str(
            metadata.get(SOURCE_UTTERANCE_ID_METADATA_KEY) or ""
        ).strip()
        if not source_scope or not utterance_id:
            return
        admission = self.ledger.find_admission(source_scope, utterance_id)
        if admission is not None and admission["authority_mode"] == "turn_decision":
            raise WorkLedgerConflict(
                "new-mode Work start requires its accepted effect authority"
            )

    def validate_runtime_request(
        self,
        authority: ProviderRunIntakeAuthority,
        request: ProviderRunRequest,
    ) -> RuntimeWorkEffectPayload:
        """Rejoin a separate Host authority to its exact Runtime request facts."""

        if not isinstance(authority, ProviderRunIntakeAuthority):
            raise TypeError("Work Runtime intake requires typed Host authority")
        effect, payload = self._validate_effect(authority.effect_id)
        if not isinstance(payload, WorkEffectPayloadV3):
            raise ControlLedgerConflict(
                "Work Runtime intake requires current-turn source proof v3"
            )
        if effect["state"] not in {
            "pending",
            "dispatching",
            "running",
            "terminal",
            "unknown_reconciling",
            "needs_user_decision",
        }:
            raise ControlLedgerConflict("Work effect is not executable")
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        work = metadata.get("work") if isinstance(metadata.get("work"), dict) else {}
        metadata_keys = frozenset(metadata)
        metadata_shape_matches = (
            _ACCEPTED_REQUEST_REQUIRED_METADATA_KEYS <= metadata_keys
            and metadata_keys
            <= (
                _ACCEPTED_REQUEST_REQUIRED_METADATA_KEYS
                | _ACCEPTED_REQUEST_OPTIONAL_METADATA_KEYS
            )
        )
        is_amend = bool(_payload_work_id(payload))
        work_shape_matches = frozenset(work) == (_ACCEPTED_REQUEST_WORK_KEYS | ({"work_item_id"} if is_amend else set()))
        provider_manifest = metadata.get("provider_manifest")
        provider_projection_matches = (
            (
                "provider_ownership" not in metadata
                or metadata.get("provider_ownership") == "managed"
            )
            and (
                provider_manifest is None
                or (
                    isinstance(provider_manifest, dict)
                    and provider_manifest.get("provider_id") == payload.provider
                )
            )
            and (
                "provider_operation" not in metadata
                or (
                    isinstance(metadata.get("provider_operation"), str)
                    and bool(str(metadata["provider_operation"]).strip())
                )
            )
        )
        raw_context = metadata.get("source_user_context")
        context = (
            ""
            if raw_context is None
            else (
                "\n".join(
                    line.strip()
                    for line in raw_context.splitlines()
                    if line.strip()
                )[:2000]
                if isinstance(raw_context, str)
                else None
            )
        )
        project = self.work.get_project(payload.project_id)
        if project is None:
            raise WorkLedgerNotFound(f"unknown project: {payload.project_id}")
        target = self._amend_target(payload) if is_amend else None
        attachment = (self._context_attachment(payload)
            if isinstance(payload, WorkCooperativeContextPayloadV6) else None)
        workspace_path = (target.workspace_path if target is not None
            else str(attachment.audit.get("workspace_path") or "")
            if attachment is not None else project.canonical_path)
        no_workspace = _work_has_no_workspace(payload)
        if no_workspace:
            workspace_path = ""
        try:
            same_workspace = (
                request.cwd is None if no_workspace else
                isinstance(request.cwd, str)
                and bool(request.cwd)
                and Path(request.cwd).resolve()
                == Path(workspace_path).resolve()
            )
        except (OSError, RuntimeError, ValueError):
            same_workspace = False
        if (
            request.provider != payload.provider
            or request.task != payload.task
            or request.mode != payload.mode
            or request.ownership != "managed"
            or request.requirements != payload.requirements
            or request.recovery is not None
            or request.session is not None
            or not same_workspace
            or not metadata_shape_matches
            or not work_shape_matches
            or not provider_projection_matches
            or metadata.get("source") != "control_work_effect"
            or metadata.get("session_id") != payload.session_id
            or metadata.get("turn_id") != payload.turn_id
            or metadata.get(SOURCE_UTTERANCE_ID_METADATA_KEY) != payload.utterance_id
            or metadata.get("source_user_text") != payload.source_user_text
            or ((metadata.get("source_user_operation_text") != payload.task)
                if payload.task != payload.source_user_text
                else "source_user_operation_text" in metadata)
            or context != payload.source_user_context
            or metadata.get("external_export") != (
                {"target":payload.external_export_target} if payload.external_export_target else None)
            or metadata.get("host_outcome_requirement") != self._accepted_outcome_requirement(effect, payload)
            or metadata.get(SOURCE_CONTEXT_SCOPE_METADATA_KEY)
            != payload.source_context_scope
            or metadata.get("payload_continuity") != _payload_continuity(payload)
            or metadata.get("intent") != _payload_operation(payload)
            or metadata.get("continuation") != ("amend" if is_amend else "new")
            or type(metadata.get("write_intent")) is not bool
            or metadata.get("write_intent")
            != (payload.requirements.workspace_access == "write")
            or metadata.get("provider_requirements")
            != payload.requirements.to_dict()
            or metadata.get(MAIN_ROLE_NAME_METADATA_KEY)
            != MAIN_CONVERSATION_ROLE_NAME
            or work.get("project_id") != payload.project_id
            or work.get("workspace_path") != workspace_path
            or work.get("title") != (target.title if target is not None else payload.title)
            or (is_amend and work.get("work_item_id") != payload.work_item_id)
            or work.get("workspace_mode") != ("none" if no_workspace else "local")
            or request.ownership != payload.requirements.ownership
        ):
            raise WorkLedgerConflict("Provider request does not match accepted Work effect")
        if is_amend and self.work.get_origin_effect_binding(authority.effect_id) is None:
            self._validate_amend_start(payload)
        return payload

    def bind_runtime_dispatch_intent(
        self,
        authority: ProviderRunIntakeAuthority,
        request: ProviderRunRequest,
        *,
        provider_run_id: str,
        project_id: str,
        title: str,
        goal: str,
        workspace_mode: str,
        workspace_path: str,
        branch: str,
        base_revision: str,
        work_metadata: Mapping[str, Any],
        operation_metadata: Mapping[str, Any],
        attempt_metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Atomically bind the exact Runtime/Coordinator-prepared Work intake."""

        for metadata in (work_metadata, operation_metadata, attempt_metadata):
            if "origin_effect_id" in metadata:
                raise WorkLedgerConflict("metadata cannot supply Work effect authority")
        _effect, candidate = self._validate_effect(authority.effect_id)
        if isinstance(candidate, (WorkContextPayloadV5,
                WorkCooperativeContextPayloadV6)) and (
                request.session != self._context_attachment(candidate).session):
            raise WorkLedgerConflict("prepared request did not retain the accepted Provider recipient")
        if _payload_work_id(candidate):
            # The configured Coordinator already validated the incoming request
            # and now owns its native session attachment and context delivery.
            payload = self.validate_runtime_request(authority, replace(request, session=None))
            target = self._amend_target(payload)
            if (request.provider, request.task, request.mode, request.requirements) != (
                payload.provider, payload.task, payload.mode, payload.requirements,
            ) or (project_id, title, goal, workspace_mode, workspace_path, branch, base_revision) != (
                target.project_id, target.title, target.goal, target.workspace_mode,
                target.workspace_path, target.branch, target.base_revision,
            ):
                raise WorkLedgerConflict("prepared amendment differs from its accepted Work target")
            return self._bind_dispatch_intent(
                authority.effect_id, provider_run_id=provider_run_id,
                lease_seconds=_RUNTIME_CLAIM_LEASE_SECONDS, reconciliation=_RUNTIME_RECONCILIATION,
                workspace_mode=workspace_mode, workspace_path=workspace_path, branch=branch,
                base_revision=base_revision, operation_metadata=dict(operation_metadata),
                attempt_metadata=dict(attempt_metadata),
            )
        project = self.work.get_project(candidate.project_id)
        assert project is not None
        draft = project.metadata.get("scratch") is True
        if draft and not is_scratch_root(project.canonical_path):
            raise WorkLedgerConflict("accepted Draft container is no longer the configured scratch root")
        no_workspace = _work_has_no_workspace(candidate)
        incoming = replace(request,
            cwd=project.canonical_path if draft and not no_workspace else request.cwd,
            session=None if isinstance(candidate, (WorkContextPayloadV5,
                WorkCooperativeContextPayloadV6)) else request.session)
        payload = self.validate_runtime_request(authority, incoming)
        work_item_id = self.initial_work_item_id(authority.effect_id) if draft else ""
        expected_workspace = (Path(str(self._context_attachment(candidate).audit.get(
                "workspace_path") or ""))
            if isinstance(candidate, WorkCooperativeContextPayloadV6)
            else scratch_workspace_path(payload.title, unique_id=work_item_id)
            if draft else Path(project.canonical_path))
        try:
            workspace_matches = (
                not workspace_path and request.cwd is None if no_workspace else
                Path(workspace_path).resolve() == expected_workspace.resolve()
            )
        except (OSError, RuntimeError, ValueError):
            workspace_matches = False
        if (
            project_id != payload.project_id
            or title != payload.title
            or goal != payload.task
            or workspace_mode != ("none" if no_workspace else "local")
            or not workspace_matches
            or branch
            or base_revision
        ):
            raise WorkLedgerConflict("prepared workspace differs from the accepted Work contract")
        return self._bind_dispatch_intent(
            authority.effect_id,
            provider_run_id=provider_run_id,
            lease_seconds=_RUNTIME_CLAIM_LEASE_SECONDS,
            reconciliation=_RUNTIME_RECONCILIATION,
            workspace_mode=workspace_mode,
            workspace_path=workspace_path,
            branch=branch,
            base_revision=base_revision,
            work_metadata=dict(work_metadata),
            operation_metadata=dict(operation_metadata),
            attempt_metadata=dict(attempt_metadata),
            work_item_id=work_item_id,
        )

    def _exact_binding(
        self,
        effect_id: str,
        payload: WorkEffectPayload,
        provider_run_id: str,
    ) -> dict[str, str] | None:
        binding = self.work.get_origin_effect_binding(effect_id)
        if binding is None:
            return None
        item = self.work.get_work_item(binding["work_item_id"])
        operation = self.work.get_operation(binding["operation_id"])
        attempt = self.work.get_attempt(binding["attempt_id"])
        if (
            item is None
            or operation is None
            or attempt is None
            or item.project_id != payload.project_id
            or (_payload_work_id(payload) and item.work_item_id != _payload_work_id(payload))
            or (not _payload_work_id(payload) and (item.title != payload.title or item.goal != payload.task))
            or operation.intent != _payload_operation(payload)
            or operation.instruction != payload.task
            or attempt.provider != payload.provider
            or attempt.task != payload.task
            or attempt.mode != payload.mode
            or (isinstance(payload, WorkContextPayloadV5) and
                attempt.metadata.get("provider_session_attach", {}).get("context_attempt_id") != payload.context_attempt_id)
            or (isinstance(payload, WorkCooperativeContextPayloadV6) and (
                attempt.metadata.get("provider_session_attach", {}).get(
                    "cooperative_context_id") != payload.cooperative_context_id
                or attempt.metadata.get("provider_session_attach", {}).get(
                    "cooperative_context_revision")
                != payload.cooperative_context_revision))
            or binding["provider_run_id"] != provider_run_id
        ):
            raise WorkLedgerConflict(
                f"origin effect has a different Work binding: {effect_id}"
            )
        return binding

    def binding(self, effect_id: str) -> dict[str, str] | None:
        """Return only a complete binding that still matches its accepted payload."""

        _effect, payload = self._validate_effect(effect_id)
        binding = self.work.get_origin_effect_binding(effect_id)
        if binding is None:
            return None
        return self._exact_binding(
            effect_id,
            payload,
            binding["provider_run_id"],
        )

    def runtime_binding(self, effect_id: str) -> dict[str, str] | None:
        """Return one exact Runtime binding without reapplying execution eligibility."""

        _effect, payload = self._validate_effect(effect_id)
        if not isinstance(payload, (WorkEffectPayloadV2, WorkEffectPayloadV3)):
            raise ControlLedgerConflict(
                "Runtime reconciliation requires a source-bearing Work effect payload"
            )
        binding = self.work.get_origin_effect_binding(effect_id)
        if binding is None:
            return None
        return self._exact_binding(
            effect_id,
            payload,
            binding["provider_run_id"],
        )

    def record_intake_rejection(self, effect_id: str) -> dict[str, Any]:
        """Close an exact Work preparation failure before Provider execution.

        Work intake alone writes start_rejected while cancelling its unstarted
        Attempt. This is a Host preparation outcome, not a Provider terminal
        receipt; an arbitrary missing Runtime or expired claim cannot use it.
        """
        effect, _payload = self._validate_effect(effect_id)
        binding = self.binding(effect_id)
        attempt = self.work.get_attempt(binding["attempt_id"]) if binding else None
        reason = str(attempt.metadata.get("start_rejected") or "") if attempt else ""
        if (binding is None or attempt is None or not reason
                or attempt.execution_status != "cancelled" or attempt.started_at
                or attempt.metadata.get(PROVIDER_TERMINAL_PIPELINE_METADATA_KEY)):
            raise ControlLedgerConflict("Work has no rejected, unstarted intake fact")
        lease = self.work.get_writer_lease(attempt.attempt_id)
        if lease is not None and lease.status != "released":
            raise ControlLedgerConflict("rejected Work intake still owns its writer lease")
        return self.ledger.record_receipt(effect_id,
            claim_token=str(effect["claim_token"] or ""),
            external_id=binding["provider_run_id"], outcome="cancelled",
            details={**binding, "authority":"work_intake_rejection",
                "provider_started":False, "reason":reason, "error":attempt.error})

    def record_terminal_receipt(self, effect_id: str) -> dict[str, Any]:
        """Project one completed Work terminal fact into its Control receipt."""

        effect, payload = self._validate_effect(effect_id)
        binding = self.binding(effect_id)
        if binding is None or effect["state"] == "pending":
            raise ControlLedgerConflict("Work effect has no submitted domain binding")
        attempt = self.work.get_attempt(binding["attempt_id"])
        if attempt is None or attempt.execution_status not in {
            "succeeded",
            "failed",
            "cancelled",
        }:
            raise ControlLedgerConflict("Work effect has no terminal Attempt fact")
        terminal_receipt = attempt.metadata.get(
            PROVIDER_TERMINAL_PIPELINE_METADATA_KEY
        )
        if not (
            isinstance(terminal_receipt, dict)
            and terminal_receipt.get("version") == 1
            and terminal_receipt.get("state") == "completed"
            and terminal_receipt.get("provider") == payload.provider
            and terminal_receipt.get("run_id") == binding["provider_run_id"]
            and terminal_receipt.get("status") == attempt.execution_status
            and isinstance(terminal_receipt.get("receipt_sha256"), str)
            and terminal_receipt.get("receipt_sha256")
        ):
            raise ControlLedgerConflict(
                "Work effect terminal pipeline receipt is incomplete or mismatched"
            )
        completions = [
            row
            for row in self.work.list_completions(binding["work_item_id"])
            if row.attempt_id == binding["attempt_id"] and row.terminal
        ]
        if len(completions) != 1 or (
            completions[0].execution_status != attempt.execution_status
        ):
            raise ControlLedgerConflict(
                "Work effect has no unique matching completion assessment"
            )
        if attempt.metadata.get("write_intent") is True:
            lease = self.work.get_writer_lease(attempt.attempt_id)
            if lease is None or lease.status != "released":
                raise ControlLedgerConflict("Work effect writer lease is not released")
        details = {
            **binding,
            "authority": "work_terminal_pipeline",
            "work_terminal_receipt_sha256": terminal_receipt["receipt_sha256"],
            "completion_assessment_id": completions[0].assessment_id,
        }
        return self.ledger.record_receipt(
            effect_id,
            claim_token=str(effect["claim_token"] or ""),
            external_id=binding["provider_run_id"],
            outcome=attempt.execution_status,
            details=details,
        )

    def bind_dispatch_intent(
        self,
        effect_id: str,
        *,
        provider_run_id: str,
        lease_seconds: float,
        reconciliation: ReconciliationPolicy,
    ) -> dict[str, Any]:
        """Atomically claim one effect and create its initial Work triple."""

        return self._bind_dispatch_intent(
            effect_id,
            provider_run_id=provider_run_id,
            lease_seconds=lease_seconds,
            reconciliation=reconciliation,
        )

    def _bind_dispatch_intent(
        self,
        effect_id: str,
        *,
        provider_run_id: str,
        lease_seconds: float,
        reconciliation: ReconciliationPolicy,
        workspace_mode: str = "local",
        workspace_path: str = "",
        branch: str = "",
        base_revision: str = "",
        work_metadata: dict[str, Any] | None = None,
        operation_metadata: dict[str, Any] | None = None,
        attempt_metadata: dict[str, Any] | None = None,
        work_item_id: str = "",
    ) -> dict[str, Any]:

        run_id = _required(provider_run_id, "provider_run_id", limit=240)
        effect, payload = self._validate_effect(effect_id)
        binding = self._exact_binding(effect_id, payload, run_id)
        if binding is not None:
            if effect["state"] == "pending":
                raise ControlLedgerConflict("pending Work effect already has a domain binding")
            return {"effect": effect, "binding": binding, "replayed": True}
        if isinstance(payload, WorkEffectPayloadV2):
            raise ControlLedgerConflict(
                "unbound Work effect v2 cannot create a new Runtime dispatch"
            )
        if effect["state"] != "pending":
            raise ControlLedgerConflict("claimed Work effect has no domain binding")

        def write_intent(
            cursor: sqlite3.Cursor,
            frozen_payload: Mapping[str, Any],
        ) -> dict[str, str]:
            current = _decode_work_effect_payload(frozen_payload)
            if current != payload:
                raise ControlLedgerConflict("Work effect payload changed before dispatch")
            project = cursor.execute(
                "SELECT state FROM projects WHERE project_id=?", (current.project_id,)
            ).fetchone()
            if project is None:
                raise WorkLedgerNotFound(f"unknown project: {current.project_id}")
            if str(project["state"]) != "active":
                raise WorkLedgerConflict("Work effect Project changed before dispatch")
            if not _payload_work_id(current):
                destination = cursor.execute("SELECT canonical_path,metadata_json FROM projects WHERE project_id=?", (current.project_id,)).fetchone()
                if json.loads(destination["metadata_json"] or "{}").get("scratch") is True:
                    if not is_scratch_root(destination["canonical_path"]):
                        raise WorkLedgerConflict("accepted Draft container changed before dispatch")
                    expected_id = self.initial_work_item_id(effect_id)
                    expected_path = scratch_workspace_path(current.title, unique_id=expected_id).resolve()
                    allocation_matches = (
                        workspace_mode == "none" and not workspace_path
                        if _work_has_no_workspace(current) else
                        bool(workspace_path) and Path(workspace_path).resolve() == expected_path)
                    if work_item_id != expected_id or not allocation_matches:
                        raise WorkLedgerConflict("Draft requires its exact effect-owned Work allocation")
            if isinstance(current, (WorkContextPayloadV5,
                    WorkCooperativeContextPayloadV6)):
                attachment = self._context_attachment(current, cursor=cursor, workspace_path=workspace_path)
                if not attempt_metadata or attempt_metadata.get("provider_session") != attachment.session.to_dict():
                    raise WorkLedgerConflict("prepared Provider context differs from its accepted recipient")
            if _payload_work_id(current):
                row = cursor.execute("SELECT * FROM work_items WHERE work_item_id=?", (current.work_item_id,)).fetchone()
                if row is None or row["project_id"] != current.project_id or row["workspace_mode"] != "local":
                    raise WorkLedgerConflict("amend Work target changed before dispatch")
                if workspace_path and row["workspace_path"] != workspace_path:
                    raise WorkLedgerConflict("amend workspace changed before dispatch")
                prior = cursor.execute("SELECT execution_status,metadata_json FROM run_attempts WHERE work_item_id=?", (current.work_item_id,)).fetchall()
                if not prior or any(r["execution_status"] not in {"succeeded", "failed", "cancelled"} for r in prior):
                    raise WorkLedgerConflict("amend predecessor is no longer settled")
                operation, attempt = self.work.write_effect_operation_attempt(
                    cursor, current.work_item_id, intent="amend", instruction=current.task,
                    provider=current.provider, task=current.task, mode=current.mode,
                    provider_run_id=run_id, origin_effect_id=effect_id,
                    operation_metadata=operation_metadata, attempt_metadata=attempt_metadata,
                )
                return {"origin_effect_id":effect_id, "work_item_id":current.work_item_id,
                        "operation_id":operation.operation_id, "attempt_id":attempt.attempt_id,
                        "provider_run_id":attempt.provider_run_id}
            item, operation, attempt = self.work.write_effect_work_item_with_attempt(
                cursor,
                current.project_id,
                title=current.title,
                goal=current.task,
                intent="execute",
                instruction=current.task,
                provider=current.provider,
                task=current.task,
                mode=current.mode,
                workspace_mode=workspace_mode,
                workspace_path=workspace_path or None,
                branch=branch,
                base_revision=base_revision,
                provider_run_id=run_id,
                origin_effect_id=effect_id,
                work_item_id=work_item_id,
                metadata=(
                    dict(work_metadata)
                    if work_metadata is not None
                    else {
                        "source": "control_work_effect",
                        "session_id": current.session_id,
                    }
                ),
                operation_metadata=(
                    dict(operation_metadata)
                    if operation_metadata is not None
                    else {
                        "source": "control_work_effect",
                        "turn_id": current.turn_id,
                        "session_id": current.session_id,
                    }
                ),
                attempt_metadata=(
                    dict(attempt_metadata)
                    if attempt_metadata is not None
                    else {
                        "source": "control_work_effect",
                        "turn_id": current.turn_id,
                        "session_id": current.session_id,
                        "continuation": "new",
                        "provider_requirements": current.requirements.to_dict(),
                    }
                ),
            )
            return {
                "origin_effect_id": effect_id,
                "work_item_id": item.work_item_id,
                "operation_id": operation.operation_id,
                "attempt_id": attempt.attempt_id,
                "provider_run_id": attempt.provider_run_id,
            }

        try:
            claimed = self.ledger.claim_with_local_intent(
                effect_id,
                owner="work_control_c1",
                lease_seconds=lease_seconds,
                reconciliation=reconciliation,
                apply=write_intent,
            )
        except ControlLedgerConflict:
            # A separate connection may have won after our read. Return only
            # an exact committed replay; every partial/mismatched state remains
            # a conflict rather than permission to write a sibling.
            effect = self.ledger.get_effect(effect_id)
            binding = self._exact_binding(effect_id, payload, run_id)
            if binding is not None and effect["state"] != "pending":
                return {"effect": effect, "binding": binding, "replayed": True}
            raise
        binding = self._exact_binding(effect_id, payload, run_id)
        if binding is None or claimed["details"] != binding:
            raise WorkLedgerConflict("committed Work effect binding is incomplete")
        return {"effect": claimed["effect"], "binding": binding, "replayed": False}


__all__ = [
    "CurrentTurnSourceSpanV1",
    "WorkControl",
    "WorkEffectPayloadV1",
    "WorkEffectPayloadV2",
    "WorkEffectPayloadV3",
    "WorkAmendPayloadV4",
    "WorkContextPayloadV5",
    "WorkCooperativeContextPayloadV6",
]
