"""Host-owned turn admission identity, transcript hashes and bounded provenance.

Admission is captured independently of optional shadow observation. It is not
an execution grant; domain owners still validate and accept effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from itertools import islice
import json
import math
import time
from typing import Any, Mapping
import uuid

_ROOT_NAMESPACE = uuid.UUID("c0f9d879-95d2-4452-80a1-5848d0c1ed1e")
_PAYLOAD_STRING_CAP = 500
_PAYLOAD_COLLECTION_CAP = 40
_PAYLOAD_NODE_CAP = 512
_PAYLOAD_CHAR_CAP = 16_384


_SOURCE_EVIDENCE_KEYS = frozenset(
    {
        "asr_backend",
        "asr_confidence",
        "n_best_digest",
        "n_best_hashes",
        "audio_start_ms",
        "audio_end_ms",
        "tts_overlap",
        "question_adjacency",
        "transport",
        "utterance_identity_source",
    }
)


@dataclass(slots=True)
class _ProjectionBudget:
    nodes_left: int = _PAYLOAD_NODE_CAP
    chars_left: int = _PAYLOAD_CHAR_CAP
    exhausted: bool = False

    def claim_node(self) -> bool:
        if self.nodes_left <= 0:
            self.exhausted = True
            return False
        self.nodes_left -= 1
        return True

    def text(self, value: Any, *, cap: int = _PAYLOAD_STRING_CAP) -> str:
        if self.chars_left <= 0:
            self.exhausted = True
            return ""
        rendered = str(value).encode("utf-8", errors="replace").decode("utf-8")
        requested = min(len(rendered), max(0, int(cap)))
        size = min(requested, self.chars_left)
        self.chars_left -= size
        if size < requested:
            self.exhausted = True
        return rendered[:size]


def _digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def admission_transcript_hash(transcript: str) -> str:
    """Return the exact digest used by a captured admission transcript."""

    text = str(transcript or "")
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "an admitted transcript must contain only Unicode scalar values"
        ) from exc
    # `_digest` preserves the historical JSON-wrapped digest for every valid
    # transcript. Rejecting invalid scalar data first removes the replacement-
    # encoding collision without changing any canonical existing hash.
    return _digest(text)


_OMITTED_PROJECTION = object()


def _project_bounded_json(
    value: Any,
    *,
    depth: int = 0,
    budget: _ProjectionBudget,
) -> Any:
    if not budget.claim_node():
        return _OMITTED_PROJECTION
    if depth >= 5:
        return budget.text("<depth-limit>")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return budget.text(f"<non-finite-float:{value!s}>")
    if isinstance(value, str):
        return budget.text(value)
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        item_cap = _PAYLOAD_COLLECTION_CAP
        if len(value) > _PAYLOAD_COLLECTION_CAP:
            item_cap -= 1
        for key, item in islice(value.items(), item_cap):
            if budget.nodes_left <= 0 or budget.chars_left <= 0:
                budget.exhausted = True
                break
            safe_key = budget.text(key, cap=120)
            if not safe_key and budget.exhausted:
                break
            child = _project_bounded_json(
                item,
                depth=depth + 1,
                budget=budget,
            )
            if child is _OMITTED_PROJECTION:
                break
            projected[safe_key] = child
        if len(value) > _PAYLOAD_COLLECTION_CAP:
            marker = _project_bounded_json(True, depth=depth + 1, budget=budget)
            if marker is not _OMITTED_PROJECTION:
                projected["_shadow_projection_truncated"] = marker
        return projected
    if isinstance(value, (set, frozenset)):
        if len(value) > _PAYLOAD_COLLECTION_CAP:
            return budget.text(f"<set-truncated count={len(value)}>")
        projected_set: list[Any] = []
        for item in sorted(value, key=lambda item: repr(item)):
            if budget.nodes_left <= 0 or budget.chars_left <= 0:
                budget.exhausted = True
                break
            child = _project_bounded_json(item, depth=depth + 1, budget=budget)
            if child is _OMITTED_PROJECTION:
                break
            projected_set.append(child)
        return projected_set
    if isinstance(value, (list, tuple)):
        projected_items: list[Any] = []
        truncated = len(value) > _PAYLOAD_COLLECTION_CAP
        item_cap = _PAYLOAD_COLLECTION_CAP - 1 if truncated else _PAYLOAD_COLLECTION_CAP
        for item in islice(value, item_cap):
            if budget.nodes_left <= 0 or budget.chars_left <= 0:
                budget.exhausted = True
                break
            child = _project_bounded_json(item, depth=depth + 1, budget=budget)
            if child is _OMITTED_PROJECTION:
                break
            projected_items.append(child)
        if truncated:
            marker = _project_bounded_json(
                f"<collection-truncated count={len(value)}>",
                depth=depth + 1,
                budget=budget,
            )
            if marker is not _OMITTED_PROJECTION:
                projected_items.append(marker)
        return projected_items
    return budget.text(value)


def _bounded_json(value: Any) -> Any:
    """Return a JSON-safe projection with strict retained-node/character bounds.

    The recursive walk limits allocation before serialization.  The final
    compact-JSON check accounts for escaping (for example NUL becoming
    ``\\u0000``), which an input-character counter cannot predict.  If either
    global budget is exhausted, evidence degrades to one fixed diagnostic
    marker instead of retaining a deceptively partial structure.
    """

    budget = _ProjectionBudget()
    projected = _project_bounded_json(value, budget=budget)
    if projected is _OMITTED_PROJECTION or budget.exhausted:
        return {"_shadow_projection_budget_exhausted": True}
    try:
        rendered = json.dumps(
            projected,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        rendered.encode("utf-8")
    except (TypeError, ValueError):
        return {"_shadow_projection_budget_exhausted": True}
    if len(rendered) > _PAYLOAD_CHAR_CAP:
        return {"_shadow_projection_budget_exhausted": True}
    return projected


def _source_scope(session_id: str, supplied: str = "") -> str:
    clean = str(supplied or "").strip()
    if clean:
        return clean
    sid = str(session_id or "").strip()
    return f"chat:{sid}" if sid else "chat:unbound"


def _bounded_source_evidence(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep provenance signals, never raw alternate transcripts or audio."""

    if not isinstance(value, Mapping):
        return {}
    selected = {
        key: value[key]
        for key in sorted(_SOURCE_EVIDENCE_KEYS)
        if key in value
    }
    projected = _bounded_json(selected)
    return dict(projected) if isinstance(projected, Mapping) else {}


@dataclass(frozen=True, slots=True)
class TurnAdmissionRecord:
    """Stable provenance for one admitted transport utterance.

    ``acceptance_key`` is identity-based.  Transcript hashes are evidence and
    cache material only; two different utterance ids may legitimately carry
    identical text.
    """

    root_id: str
    utterance_id: str
    turn_id: str
    session_id: str
    dialogue_source_scope: str
    input_source: str
    chat_epoch: int | None
    pending: bool
    authority_mode: str
    transcript_ref: str
    transcript_hash: str
    transcript_length: int
    admitted_at: float
    admitted_at_monotonic: float
    source_evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def acceptance_key(self) -> tuple[str, str]:
        return self.dialogue_source_scope, self.utterance_id


def capture_turn_admission(
    *,
    utterance_id: str,
    turn_id: str,
    session_id: str,
    transcript: str,
    dialogue_source_scope: str = "",
    input_source: str = "",
    chat_epoch: int | None = None,
    pending: bool = False,
    authority_mode: str = "source_witness_v1",
    source_evidence: Mapping[str, Any] | None = None,
) -> TurnAdmissionRecord | None:
    """Capture Host ingress facts independently of optional shadow retention.

    This is not an epoch grant, rollout choice or execution acceptance. A
    retry can share a provenance root while carrying its current presentation
    turn/epoch; the observer separately retains its original canonical record.
    """

    clean_turn = str(turn_id or "").strip()
    clean_utterance = str(utterance_id or clean_turn).strip()
    if not clean_turn or not clean_utterance:
        return None
    scope = _source_scope(session_id, dialogue_source_scope)
    text = str(transcript or "")
    return TurnAdmissionRecord(
        root_id=_stable_id(_ROOT_NAMESPACE, scope, clean_utterance),
        utterance_id=clean_utterance,
        turn_id=clean_turn,
        session_id=str(session_id or "").strip(),
        dialogue_source_scope=scope,
        input_source=str(input_source or "").strip(),
        chat_epoch=int(chat_epoch) if chat_epoch is not None else None,
        pending=bool(pending),
        authority_mode=str(authority_mode or "source_witness_v1"),
        transcript_ref=f"turn:{clean_turn}",
        transcript_hash=admission_transcript_hash(text),
        transcript_length=len(text),
        admitted_at=time.time(),
        admitted_at_monotonic=time.monotonic(),
        source_evidence=_bounded_source_evidence(source_evidence),
    )


def _stable_id(namespace: uuid.UUID, *parts: str) -> str:
    return uuid.uuid5(namespace, "\x1f".join(str(part) for part in parts)).hex
