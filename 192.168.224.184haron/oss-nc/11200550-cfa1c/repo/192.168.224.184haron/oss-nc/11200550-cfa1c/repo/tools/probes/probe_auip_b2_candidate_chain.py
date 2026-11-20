r"""Run the product-inert AUIP B2 candidate-locked combination chain.

B2 combines an AppSession-local role branch, Host-owned accepted facts, exact
revision-bound candidate IDs, an optional compact image, one role choice plus
intent line, atomic AUIP invocation, application receipt, and delivery only
after acceptance.  The probe uses the real ``AuipRuntime`` state machine and
in-memory application fixtures; it never opens a user app or writes Work.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import hashlib
import itertools
import json
import math
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from openai import AsyncOpenAI


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings
from server.auip_contract import AuipProtocolError, validate_payload
from server.auip_role_branch_experiment import AppSessionBranchProposal
from tools.probes import probe_gemini37_gomoku_role as gomoku_probe
from tools.probes.probe_auip_appsession_branch_abc import (
    GOMOKU_TURNS,
    REACTOR_TURNS,
    TOWER_TURNS,
    InMemoryReactor,
    InMemorySplitGomoku,
    InMemoryTowerDefense,
    JourneyTurn,
    _initial_state,
    _reactor_state,
    _tower_state,
    _with_controls,
)


SCHEMA = "amadeus.auip-b2-candidate-chain-probe.v1"
DEFAULT_MODEL = "gpt-5.6-terra"
DEFAULT_SUITES = ("standard", "gomoku", "tower", "reactor", "edges")
PROTOCOL_WORDS = (
    "AUIP",
    "Host",
    "candidate_id",
    "payload",
    "schema",
    "receipt",
    "revision",
)


B2_SYSTEM = r"""
[APPSESSION B2 ROLE CHOICE]
You are Makise Kurisu inside one active application experience. Speak one
natural, concise Japanese line with her analytical, independent, slightly
competitive personality and restrained warmth. Do not mention models, agents,
delegation, candidates, Host machinery, payloads, schemas, receipts, or AUIP.

The supplied `host_facts` are the accepted current application truth. Branch
dialogue gives continuity but cannot override current facts. The candidate
catalog or compact grid-candidate contract describes exact revision-bound
actions compiled by the Host. Select one candidate ID according to that
contract; the Host rejects any ID absent from its private membership map. You
may not invent or modify an action or payload. Use the app objective and
interaction summary to choose the best current outcome. Prefer an immediately
objective-completing choice, then a choice that prevents an immediate
objective-ending danger. Follow a concrete safe user suggestion. If the
suggestion is unavailable or materially unsafe, choose a supported alternative
and explain the factual reason naturally. Do not search for a globally optimal
strategy: when no immediate objective or danger determines a unique choice,
select one reasonable candidate promptly.

Return one structured decision. `speech` must commit to the selected candidate
as present intent, not claim that execution already succeeded. For coordinate
actions, state the exact selected ASCII `(x,y)` once. `choice_reason` is private
audit evidence and may be factual English; `speech` is the Japanese user line.
Set `instruction_relation` to `not_applicable` when `user_instruction` is empty,
to `follows` when the selected candidate implements a nonempty suggestion, and
to `safe_alternative` when it deliberately selects another supported outcome.
[/APPSESSION B2 ROLE CHOICE]
"""


@dataclass(frozen=True, slots=True)
class LockedCandidate:
    candidate_id: str
    action_type: str
    payload: dict[str, Any]
    semantic_label: str
    revision: int
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", validate_payload(self.payload))


@dataclass
class ChainRow:
    suite: str
    case_id: str
    repeat: int
    user_instruction: str
    expected_action: str
    expected_payload: dict[str, Any] | None
    candidate_count: int = 0
    selected_candidate_id: str = ""
    selected_action: str = ""
    selected_payload: dict[str, Any] | None = None
    instruction_relation: str = ""
    choice_reason: str = ""
    speech: str = ""
    model_completed: bool = False
    candidate_membership: bool = False
    preflight_ok: bool = False
    invoke_ok: bool = False
    receipt_accepted: bool | None = None
    proposal_receipt_linked: bool = False
    narration_recorded: bool = False
    receipt_before_narration_record: bool = False
    expected_match: bool = False
    speech_action_match: bool = False
    speech_japanese: bool = False
    speech_protocol_clean: bool = False
    branch_active: bool = False
    branch_messages_before: int = 0
    branch_messages_after: int = 0
    revision_before: int = 0
    revision_after: int = 0
    model_latency_ms: float = 0.0
    path_latency_ms: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    requested_service_tier: str = ""
    served_service_tier: str = ""
    image_bytes: int = 0
    error: str = ""

    def __post_init__(self) -> None:
        self.expected_payload = (
            dict(self.expected_payload)
            if self.expected_payload is not None
            else None
        )
        self.selected_payload = dict(self.selected_payload or {})

    @property
    def full_pass(self) -> bool:
        return all(
            (
                self.model_completed,
                self.candidate_membership,
                self.preflight_ok,
                self.invoke_ok,
                self.receipt_accepted is True,
                self.proposal_receipt_linked,
                self.narration_recorded,
                self.receipt_before_narration_record,
                self.expected_match,
                self.speech_action_match,
                self.speech_japanese,
                self.speech_protocol_clean,
                not self.error,
            )
        )


def _attach_runtime_branch(fixture: Any) -> None:
    """Bind the fixture branch to the real runtime's A1 receipt/delivery path."""

    session = fixture.runtime._sessions[fixture.app_session_id]
    session.role_branch = fixture.branch
    if not fixture.runtime.role_branch_active(fixture.app_session_id):
        raise RuntimeError("runtime role branch did not activate")


def _nested(value: Mapping[str, Any], path: str) -> Any:
    current: Any = value
    for part in str(path or "").split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _first_situation(value: Any, kind: str) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        if str(value.get("kind") or "").strip().lower() == kind:
            return value
        for child in value.values():
            found = _first_situation(child, kind)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_situation(child, kind)
            if found is not None:
                return found
    return None


def _candidate_id(action_type: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        [str(action_type or "").strip().lower(), validate_payload(payload)],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "cand_" + hashlib.sha256(encoded).hexdigest()[:12]


def _preflight_candidate(fixture: Any, candidate: LockedCandidate) -> None:
    if candidate.revision != fixture.revision:
        raise AuipProtocolError("stale_action_revision")
    fixture.runtime.check_action_preconditions(
        app_session_id=fixture.app_session_id,
        type=candidate.action_type,
        payload=dict(candidate.payload),
        expected_revision=candidate.revision,
    )
    context = fixture.participant_context()
    choice_types = {
        str(value or "").strip().lower()
        for value in context.get("choice_action_types") or []
    }
    if candidate.action_type in choice_types and not any(
        str(option.get("action") or "").strip().lower()
        == candidate.action_type
        and validate_payload(option.get("payload") or {}) == candidate.payload
        for option in context.get("available_choice_options") or []
        if isinstance(option, Mapping)
    ):
        raise AuipProtocolError("action_not_available")


def _append_candidate(
    fixture: Any,
    output: dict[str, LockedCandidate],
    *,
    action_type: str,
    payload: Mapping[str, Any],
    semantic_label: str,
    source: str,
    candidate_id: str = "",
) -> None:
    candidate = LockedCandidate(
        candidate_id=(
            str(candidate_id or "").strip()
            or _candidate_id(action_type, payload)
        ),
        action_type=str(action_type or "").strip().lower(),
        payload=validate_payload(payload),
        semantic_label=str(semantic_label or action_type).strip()[:200],
        revision=int(fixture.revision),
        source=source,
    )
    try:
        _preflight_candidate(fixture, candidate)
    except AuipProtocolError:
        return
    output[candidate.candidate_id] = candidate


def _compile_locked_candidates(fixture: Any) -> dict[str, LockedCandidate]:
    """Compile exact candidates from choice/v1 and standard preconditions."""

    context = fixture.participant_context()
    output: dict[str, LockedCandidate] = {}
    choice_types = {
        str(value or "").strip().lower()
        for value in context.get("choice_action_types") or []
    }
    for option in context.get("available_choice_options") or []:
        if not isinstance(option, Mapping):
            continue
        action_type = str(option.get("action") or "").strip().lower()
        if not action_type:
            continue
        _append_candidate(
            fixture,
            output,
            action_type=action_type,
            payload=option.get("payload") or {},
            semantic_label=str(option.get("label") or action_type),
            source="choice/v1",
        )

    state = context.get("state") if isinstance(context.get("state"), Mapping) else {}
    for action_index, (raw_type, raw_spec) in enumerate(
        sorted((context.get("available_actions") or {}).items())
    ):
        action_type = str(raw_type or "").strip().lower()
        if not action_type or action_type in choice_types:
            continue
        spec = raw_spec if isinstance(raw_spec, Mapping) else {}
        schema = spec.get("inputSchema")
        schema = schema if isinstance(schema, Mapping) else {}
        required = {
            str(value or "").strip()
            for value in schema.get("required") or []
            if str(value or "").strip()
        }
        preconditions = [
            value
            for value in spec.get("preconditions") or []
            if isinstance(value, Mapping)
        ]
        grid_condition = next(
            (
                value
                for value in preconditions
                if str(value.get("kind") or "").strip().lower()
                == "grid_cell_empty/v1"
            ),
            None,
        )
        if grid_condition is not None:
            x_field = str(grid_condition.get("xField") or "").strip()
            y_field = str(grid_condition.get("yField") or "").strip()
            grid = _nested(state, str(grid_condition.get("statePath") or ""))
            if (
                not x_field
                or not y_field
                or not isinstance(grid, Mapping)
                or required.difference({x_field, y_field})
            ):
                continue
            rows = grid.get("rows")
            empty = str(grid.get("empty") or ".")
            if not isinstance(rows, list) or len(empty) != 1:
                continue
            for y, row in enumerate(rows):
                if not isinstance(row, str):
                    continue
                for x, value in enumerate(row):
                    if value != empty:
                        continue
                    _append_candidate(
                        fixture,
                        output,
                        action_type=action_type,
                        payload={x_field: x, y_field: y},
                        semantic_label=f"Coordinate ({x},{y})",
                        source="grid_cell_empty/v1",
                        candidate_id=f"g{action_index}_{y:02d}_{x:02d}",
                    )
            continue

        properties = schema.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        if not required and not properties:
            _append_candidate(
                fixture,
                output,
                action_type=action_type,
                payload={},
                semantic_label=str(spec.get("description") or action_type),
                source="closed_empty_payload",
            )
            continue
        finite_fields: list[tuple[str, list[Any]]] = []
        finite = bool(required) and required == set(properties)
        for key in sorted(required):
            prop = properties.get(key)
            values = prop.get("enum") if isinstance(prop, Mapping) else None
            if not isinstance(values, list) or not values or len(values) > 8:
                finite = False
                break
            finite_fields.append((key, list(values)))
        if not finite:
            continue
        combinations = math.prod(len(values) for _, values in finite_fields)
        if combinations > 32:
            continue
        for values in itertools.product(*(values for _, values in finite_fields)):
            payload = {
                finite_fields[index][0]: value
                for index, value in enumerate(values)
            }
            _append_candidate(
                fixture,
                output,
                action_type=action_type,
                payload=payload,
                semantic_label=(
                    str(spec.get("description") or action_type)
                    + " "
                    + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                ),
                source="finite_schema",
            )
    return output


def _response_schema(candidate_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "candidate_id": {"type": "string", "enum": candidate_ids},
            "instruction_relation": {
                "type": "string",
                "enum": ["follows", "safe_alternative", "not_applicable"],
            },
            "choice_reason": {"type": "string"},
            "speech": {"type": "string"},
        },
        "required": [
            "candidate_id",
            "instruction_relation",
            "choice_reason",
            "speech",
        ],
        "additionalProperties": False,
    }


def _image_bytes(fixture: Any) -> bytes:
    board = fixture.state.get("board") if isinstance(fixture.state, Mapping) else None
    rows = board.get("rows") if isinstance(board, Mapping) else None
    if not isinstance(rows, list) or len(rows) != gomoku_probe.SIZE:
        return b""
    if not all(isinstance(row, str) and len(row) == gomoku_probe.SIZE for row in rows):
        return b""
    return gomoku_probe._render_board(list(rows))


def _model_payload(
    fixture: Any,
    *,
    user_instruction: str,
    candidates: Mapping[str, LockedCandidate],
    has_image: bool,
) -> dict[str, Any]:
    context = fixture.participant_context()
    branch_messages = fixture.runtime.recent_role_branch_messages(
        fixture.conversation_id,
        limit=12,
    )
    if branch_messages is None:
        raise RuntimeError("runtime branch context is unavailable")
    grid_candidates = [
        candidate
        for candidate in candidates.values()
        if candidate.source == "grid_cell_empty/v1"
    ]
    grid_contracts: list[dict[str, Any]] = []
    state = context.get("state") if isinstance(context.get("state"), Mapping) else {}
    grid = _first_situation(state, "grid/v1")
    for action_type in sorted({candidate.action_type for candidate in grid_candidates}):
        group = [
            candidate
            for candidate in grid_candidates
            if candidate.action_type == action_type
        ]
        prefix = group[0].candidate_id.rsplit("_", 2)[0] if group else ""
        legal_ids = {candidate.candidate_id for candidate in group}
        width = int(grid.get("width") or 0) if isinstance(grid, Mapping) else 0
        height = int(grid.get("height") or 0) if isinstance(grid, Mapping) else 0
        unavailable_ids = [
            f"{prefix}_{y:02d}_{x:02d}"
            for y in range(max(0, height))
            for x in range(max(0, width))
            if f"{prefix}_{y:02d}_{x:02d}" not in legal_ids
        ]
        grid_contracts.append(
            {
                "action_meaning": "Select one currently empty grid coordinate.",
                "candidate_id_format": f"{prefix}_YY_XX",
                "encoding": (
                    "YY and XX are zero-padded decimal y and x. For example, "
                    f"coordinate (7,7) is {prefix}_07_07. Construct the ID "
                    "only for a point currently shown empty in host_facts; "
                    "the Host membership map rejects every other ID."
                ),
                "candidate_count": len(group),
                "unavailable_candidate_ids": unavailable_ids,
            }
        )
    return {
        "app": context.get("app") or {},
        "branch_messages": branch_messages,
        "host_facts": {
            "app_session_id": fixture.app_session_id,
            "revision": fixture.revision,
            "state": context.get("state") or {},
            "recent_verified_self_actions": context.get(
                "recent_verified_self_actions"
            )
            or [],
        },
        "user_instruction": str(user_instruction or "").strip(),
        "candidate_catalog": [
            {
                "candidate_id": candidate.candidate_id,
                "meaning": candidate.semantic_label,
            }
            for candidate in candidates.values()
            if candidate.source != "grid_cell_empty/v1"
        ],
        "grid_candidate_contracts": grid_contracts,
        "visual_observation": (
            "A compact grayscale rendering of the same grid is attached. "
            "Host facts remain authoritative."
            if has_image
            else "none"
        ),
    }


async def _choose_candidate(
    client: AsyncOpenAI,
    *,
    fixture: Any,
    user_instruction: str,
    candidates: Mapping[str, LockedCandidate],
    model: str,
    reasoning_effort: str,
    service_tier: str,
    timeout_s: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not candidates:
        raise RuntimeError("B2 action turn has no Host-locked candidates")
    image_bytes = _image_bytes(fixture)
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": json.dumps(
                _model_payload(
                    fixture,
                    user_instruction=user_instruction,
                    candidates=candidates,
                    has_image=bool(image_bytes),
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
    ]
    if image_bytes:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:image/png;base64,{encoded}",
                "detail": "original",
            }
        )
    started = time.perf_counter()
    request: dict[str, Any] = {
        "model": model,
        "instructions": B2_SYSTEM,
        "input": [{"role": "user", "content": content}],
        "reasoning": {"effort": reasoning_effort},
        "max_output_tokens": 700,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "auip_b2_role_choice",
                "strict": True,
                "schema": _response_schema(list(candidates)),
            }
        },
        "store": False,
    }
    if service_tier != "auto":
        request["service_tier"] = service_tier
    response = await asyncio.wait_for(
        client.responses.create(**request),
        timeout=max(1.0, float(timeout_s)),
    )
    latency_ms = (time.perf_counter() - started) * 1000
    decision = json.loads(str(response.output_text or ""))
    usage = getattr(response, "usage", None)
    details = getattr(usage, "output_tokens_details", None) if usage else None
    metrics = {
        "latency_ms": latency_ms,
        "input_tokens": getattr(usage, "input_tokens", None) if usage else None,
        "output_tokens": getattr(usage, "output_tokens", None) if usage else None,
        "reasoning_tokens": (
            getattr(details, "reasoning_tokens", None) if details else None
        ),
        "served_service_tier": str(
            getattr(response, "service_tier", "") or ""
        ),
        "image_bytes": len(image_bytes),
    }
    return decision, metrics


def _application_transition(
    fixture: Any,
    proposal: AppSessionBranchProposal,
) -> tuple[bool, dict[str, Any], dict[str, Any], str]:
    if isinstance(fixture, InMemorySplitGomoku):
        return fixture._application_result(proposal)
    payload = dict(proposal.payload or {})
    if isinstance(fixture, InMemoryTowerDefense):
        mode = str(payload.get("mode") or "")
        accepted = proposal.action_type == "defense.set_mode" and mode in {
            "balance",
            "defend_left",
            "defend_right",
            "follow_user",
            "rewards",
        }
        next_state = _tower_state(
            current_mode=mode,
            left=str((fixture.state.get("threats") or {}).get("left") or "low"),
            right=str((fixture.state.get("threats") or {}).get("right") or "low"),
        )
        return (
            accepted,
            next_state if accepted else copy.deepcopy(fixture.state),
            {"mode": mode} if accepted else {},
            "" if accepted else "unsupported defense mode",
        )
    if isinstance(fixture, InMemoryReactor):
        level = str(payload.get("level") or "")
        available = {
            str(item.get("payload", {}).get("level") or "")
            for item in (fixture.state.get("controls") or {}).get("options", [])
            if item.get("available") is True
        }
        accepted = (
            proposal.action_type == "reactor.set_cooling" and level in available
        )
        metric = (fixture.state.get("metrics") or {}).get("temperature") or {}
        current = int(metric.get("value") or 0)
        next_temperature = {
            "high": max(60, current - 11),
            "low": max(60, current - 6),
            "off": current,
        }.get(level, current)
        next_state = _reactor_state(
            temperature=next_temperature,
            trend="falling" if level in {"high", "low"} else "stable",
            current_level=level,
            levels=("off",),
        )
        return (
            accepted,
            next_state if accepted else copy.deepcopy(fixture.state),
            {"coolingLevel": level, "temperature": next_temperature}
            if accepted
            else {},
            "" if accepted else "cooling level is not currently available",
        )
    raise TypeError(f"unsupported fixture: {type(fixture).__name__}")


def _invoke_and_resolve(
    fixture: Any,
    candidate: LockedCandidate,
) -> dict[str, Any]:
    invoked = fixture.runtime.invoke_action(
        app_session_id=fixture.app_session_id,
        actor="kurisu",
        type=candidate.action_type,
        payload=dict(candidate.payload),
        expected_revision=candidate.revision,
        proposal_id=candidate.candidate_id,
    )
    proposal = AppSessionBranchProposal(
        action="act",
        action_type=candidate.action_type,
        payload=dict(candidate.payload),
    )
    accepted, next_state, effects, reason = _application_transition(fixture, proposal)
    resulting_revision = fixture.revision + 1 if accepted else fixture.revision
    resolved = fixture.runtime.resolve_action(
        app_session_id=fixture.app_session_id,
        bridge_token=fixture.bridge_token,
        action_id=str(invoked["action"]["action_id"]),
        accepted=accepted,
        resulting_revision=resulting_revision,
        state=next_state if accepted else None,
        effects=effects,
        reason=reason,
    )
    if accepted:
        fixture.revision = resulting_revision
        fixture.state = next_state
    return dict(resolved["receipt"])


def _speech_action_match(speech: str, candidate: LockedCandidate) -> bool:
    payload = candidate.payload
    if isinstance(payload.get("x"), int) and isinstance(payload.get("y"), int):
        target = (int(payload["x"]), int(payload["y"]))
        normalized = str(speech or "").replace("（", "(").replace("）", ")")
        coordinates = [
            (int(match.group(1)), int(match.group(2)))
            for match in gomoku_probe.COORDINATE_RE.finditer(normalized)
        ]
        # An explained safe alternative may name both the rejected user point
        # and the selected legal point. The selected point must still be stated
        # exactly once; other coordinates do not by themselves create drift.
        return coordinates.count(target) == 1
    return True


async def _run_action_case(
    client: AsyncOpenAI,
    *,
    fixture: Any,
    suite: str,
    case_id: str,
    repeat: int,
    user_instruction: str,
    expected_action: str,
    expected_payload: Mapping[str, Any] | None,
    expected_relation: str = "",
    persistent_strategy: bool = False,
    model: str,
    reasoning_effort: str,
    service_tier: str,
    timeout_s: float,
) -> ChainRow:
    started = time.perf_counter()
    branch_before = fixture.runtime.recent_role_branch_messages(
        fixture.conversation_id,
        limit=20,
    )
    row = ChainRow(
        suite=suite,
        case_id=case_id,
        repeat=repeat,
        user_instruction=user_instruction,
        expected_action=expected_action,
        expected_payload=dict(expected_payload) if expected_payload is not None else None,
        branch_active=fixture.runtime.role_branch_active(fixture.app_session_id),
        branch_messages_before=len(branch_before or []),
        revision_before=fixture.revision,
        requested_service_tier=service_tier,
    )
    try:
        if user_instruction:
            if persistent_strategy:
                fixture.branch.record_strategy_directive(user_instruction)
            else:
                fixture.branch.record_user(user_instruction)
        candidates = _compile_locked_candidates(fixture)
        row.candidate_count = len(candidates)
        decision, metrics = await _choose_candidate(
            client,
            fixture=fixture,
            user_instruction=user_instruction,
            candidates=candidates,
            model=model,
            reasoning_effort=reasoning_effort,
            service_tier=service_tier,
            timeout_s=timeout_s,
        )
        row.model_completed = True
        row.model_latency_ms = float(metrics["latency_ms"])
        row.input_tokens = metrics["input_tokens"]
        row.output_tokens = metrics["output_tokens"]
        row.reasoning_tokens = metrics["reasoning_tokens"]
        row.served_service_tier = metrics["served_service_tier"]
        row.image_bytes = int(metrics["image_bytes"])
        row.selected_candidate_id = str(decision.get("candidate_id") or "")
        row.instruction_relation = str(
            decision.get("instruction_relation") or ""
        )
        row.choice_reason = str(decision.get("choice_reason") or "").strip()
        row.speech = str(decision.get("speech") or "").strip()
        candidate = candidates.get(row.selected_candidate_id)
        row.candidate_membership = candidate is not None
        if candidate is None:
            raise AuipProtocolError("unknown_candidate_id")
        row.selected_action = candidate.action_type
        row.selected_payload = dict(candidate.payload)
        _preflight_candidate(fixture, candidate)
        row.preflight_ok = True
        receipt = _invoke_and_resolve(fixture, candidate)
        row.invoke_ok = True
        row.receipt_accepted = bool(receipt.get("accepted"))
        row.proposal_receipt_linked = (
            str(receipt.get("proposal_id") or "") == candidate.candidate_id
        )
        if row.receipt_accepted is True:
            fixture.runtime.record_delivered_narration(
                app_session_id=fixture.app_session_id,
                text=row.speech,
                event_id=str(receipt.get("action_id") or ""),
            )
            row.narration_recorded = True
        branch_after = fixture.runtime.recent_role_branch_messages(
            fixture.conversation_id,
            limit=20,
        )
        row.branch_messages_after = len(branch_after or [])
        receipt_index = max(
            (
                index
                for index, item in enumerate(branch_after or [])
                if "[Verified AUIP receipt]" in str(item.get("content") or "")
            ),
            default=-1,
        )
        speech_index = next(
            (
                index
                for index, item in enumerate(branch_after or [])
                if str(item.get("content") or "") == row.speech
            ),
            -1,
        )
        row.receipt_before_narration_record = (
            receipt_index >= 0 and speech_index > receipt_index
        )
        row.expected_match = (
            candidate.action_type == expected_action
            and (
                expected_payload is None
                or candidate.payload == validate_payload(expected_payload)
            )
            and (
                not expected_relation
                or row.instruction_relation == expected_relation
            )
        )
        row.speech_action_match = _speech_action_match(row.speech, candidate)
        row.speech_japanese = bool(gomoku_probe.JAPANESE_RE.search(row.speech))
        row.speech_protocol_clean = not any(
            word.casefold() in row.speech.casefold() for word in PROTOCOL_WORDS
        )
    except Exception as exc:
        row.error = f"{type(exc).__name__}: {exc}"
        row.branch_messages_after = len(
            fixture.runtime.recent_role_branch_messages(
                fixture.conversation_id,
                limit=20,
            )
            or []
        )
    row.revision_after = fixture.revision
    row.path_latency_ms = (time.perf_counter() - started) * 1000
    return row


def _standard_fixture(scenario: gomoku_probe.Scenario, repeat: int) -> Any:
    fixture = InMemorySplitGomoku("b2-standard", repeat)
    _attach_runtime_branch(fixture)
    state = _initial_state(binding={"kurisu": "black", "user": "white"})
    state["board"] = {**state["board"], "rows": gomoku_probe._rows(scenario)}
    state["turn"] = "black"
    state["moveCount"] = len(scenario.stones)
    state["lastMove"] = None
    fixture.state = _with_controls(state)
    fixture._publish_external_state()
    return fixture


def _standard_expected(scenario: gomoku_probe.Scenario) -> dict[str, Any] | None:
    expected = gomoku_probe._expected(scenario).get("expected_move")
    if not isinstance(expected, list) or len(expected) != 2:
        return None
    return {"x": int(expected[0]), "y": int(expected[1])}


async def _run_standard(
    client: AsyncOpenAI,
    args: argparse.Namespace,
) -> list[ChainRow]:
    rows: list[ChainRow] = []
    selected = set(args.standard_scenario or ())
    scenarios = [
        scenario
        for scenario in gomoku_probe.SCENARIOS
        if scenario.turn_owner == "kurisu"
        and (not selected or scenario.scenario_id in selected)
    ]
    for repeat in range(1, args.repeats + 1):
        for scenario in scenarios:
            fixture = _standard_fixture(scenario, repeat)
            relation = (
                "safe_alternative"
                if scenario.scenario_id == "occupied_user_request"
                else "follows"
                if scenario.scenario_id == "explicit_empty_coordinate"
                else ""
            )
            row = await _run_action_case(
                client,
                fixture=fixture,
                suite="standard",
                case_id=scenario.scenario_id,
                repeat=repeat,
                user_instruction=scenario.user_instruction,
                expected_action="gomoku.place_stone",
                expected_payload=_standard_expected(scenario),
                expected_relation=relation,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                service_tier=args.service_tier,
                timeout_s=args.timeout,
            )
            rows.append(row)
            _print_row(row, len(rows))
    return rows


async def _run_journey(
    client: AsyncOpenAI,
    args: argparse.Namespace,
    *,
    suite: str,
    fixture_type: type,
    turns: tuple[JourneyTurn, ...],
) -> list[ChainRow]:
    rows: list[ChainRow] = []
    for repeat in range(1, args.repeats + 1):
        fixture = fixture_type(f"b2-{suite}", repeat)
        _attach_runtime_branch(fixture)
        for turn in turns:
            fixture.prepare(turn.before)
            expected_relation = (
                "safe_alternative"
                if suite == "tower" and turn.turn_id == "unsafe_repeated_left"
                else "follows"
                if turn.user
                else "not_applicable"
            )
            row = await _run_action_case(
                client,
                fixture=fixture,
                suite=suite,
                case_id=turn.turn_id,
                repeat=repeat,
                user_instruction=turn.user,
                expected_action=turn.expected_action,
                expected_payload=turn.expected_payload,
                expected_relation=expected_relation,
                persistent_strategy=turn.persistent_strategy,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                service_tier=args.service_tier,
                timeout_s=args.timeout,
            )
            rows.append(row)
            _print_row(row, len(rows))
    return rows


def _edge_checks() -> dict[str, Any]:
    stale = InMemorySplitGomoku("b2-stale", 1)
    _attach_runtime_branch(stale)
    candidates = _compile_locked_candidates(stale)
    center = next(
        candidate
        for candidate in candidates.values()
        if candidate.payload == {"x": 7, "y": 7}
    )
    stale.simulate_user_move()
    stale_blocked = False
    try:
        _preflight_candidate(stale, center)
    except AuipProtocolError as exc:
        stale_blocked = exc.code == "stale_action_revision"
    stale_snapshot = stale.runtime.focused_projection(stale.conversation_id) or {}

    rejected = InMemorySplitGomoku("b2-rejected", 1)
    _attach_runtime_branch(rejected)
    rejected_candidate = next(
        candidate
        for candidate in _compile_locked_candidates(rejected).values()
        if candidate.payload == {"x": 7, "y": 7}
    )
    invoked = rejected.runtime.invoke_action(
        app_session_id=rejected.app_session_id,
        actor="kurisu",
        type=rejected_candidate.action_type,
        payload=dict(rejected_candidate.payload),
        expected_revision=rejected_candidate.revision,
        proposal_id=rejected_candidate.candidate_id,
    )
    resolved = rejected.runtime.resolve_action(
        app_session_id=rejected.app_session_id,
        bridge_token=rejected.bridge_token,
        action_id=str(invoked["action"]["action_id"]),
        accepted=False,
        resulting_revision=rejected.revision,
        reason="deterministic probe rejection",
    )
    rejected_snapshot = rejected.runtime.focused_projection(rejected.conversation_id) or {}
    stale_pending = stale_snapshot.get("pending_action") is not None
    stale_narrations = len(
        stale_snapshot.get("recent_delivered_narrations") or []
    )
    stale_pass = stale_blocked and not stale_pending and stale_narrations == 0
    rejected_accepted = bool((resolved.get("receipt") or {}).get("accepted"))
    rejected_linked = (
        str((resolved.get("receipt") or {}).get("proposal_id") or "")
        == rejected_candidate.candidate_id
    )
    rejected_narrations = len(
        rejected_snapshot.get("recent_delivered_narrations") or []
    )
    rejected_pass = (
        not rejected_accepted and rejected_linked and rejected_narrations == 0
    )
    return {
        "overall_pass": stale_pass and rejected_pass,
        "stale_candidate": {
            "pass": stale_pass,
            "blocked": stale_blocked,
            "pending_action": stale_pending,
            "recorded_narrations": stale_narrations,
        },
        "rejected_receipt": {
            "pass": rejected_pass,
            "accepted": rejected_accepted,
            "proposal_linked": rejected_linked,
            "recorded_narrations": rejected_narrations,
        },
    }


def _print_row(row: ChainRow, index: int) -> None:
    print(
        f"[{index}] suite={row.suite} case={row.case_id} "
        f"candidates={row.candidate_count} selected={row.selected_candidate_id} "
        f"accepted={row.receipt_accepted} full={row.full_pass} "
        f"latency_ms={row.path_latency_ms:.0f}",
        flush=True,
    )


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return round(ordered[index], 2)


def _summary(rows: list[ChainRow]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for suite in sorted({row.suite for row in rows}):
        selected = [row for row in rows if row.suite == suite]
        model_completed = [row for row in selected if row.model_completed]
        result[suite] = {
            "cases": len(selected),
            "model_completed": len(model_completed),
            "candidate_membership": sum(row.candidate_membership for row in selected),
            "preflight_ok": sum(row.preflight_ok for row in selected),
            "accepted_receipts": sum(
                row.receipt_accepted is True for row in selected
            ),
            "proposal_receipt_linked": sum(
                row.proposal_receipt_linked for row in selected
            ),
            "narration_recorded": sum(row.narration_recorded for row in selected),
            "receipt_before_narration_record": sum(
                row.receipt_before_narration_record for row in selected
            ),
            "expected_match": sum(row.expected_match for row in selected),
            "speech_action_match": sum(
                row.speech_action_match for row in selected
            ),
            "full_pass": sum(row.full_pass for row in selected),
            "median_path_ms": (
                round(statistics.median(row.path_latency_ms for row in model_completed), 2)
                if model_completed
                else None
            ),
            "p95_path_ms": _percentile(
                [row.path_latency_ms for row in model_completed],
                0.95,
            ),
            "reasoning_tokens": sum(
                int(row.reasoning_tokens or 0) for row in model_completed
            ),
            "served_service_tiers": {
                tier: sum(row.served_service_tier == tier for row in model_completed)
                for tier in sorted(
                    {
                        row.served_service_tier
                        for row in model_completed
                        if row.served_service_tier
                    }
                )
            },
            "errors": [
                {"case_id": row.case_id, "error": row.error}
                for row in selected
                if row.error
            ],
        }
    return result


async def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    if not str(settings.OPENAI_API_KEY or "").strip():
        raise RuntimeError("OPENAI_API_KEY is not configured")
    client = AsyncOpenAI(
        api_key=settings.OPENAI_API_KEY,
        base_url=settings.OPENAI_BASE_URL,
        max_retries=0,
        timeout=max(1.0, float(args.timeout)),
    )
    rows: list[ChainRow] = []
    suites = set(args.suites)
    if "standard" in suites:
        rows.extend(await _run_standard(client, args))
    if "gomoku" in suites:
        rows.extend(
            await _run_journey(
                client,
                args,
                suite="gomoku",
                fixture_type=InMemorySplitGomoku,
                turns=GOMOKU_TURNS,
            )
        )
    if "tower" in suites:
        rows.extend(
            await _run_journey(
                client,
                args,
                suite="tower",
                fixture_type=InMemoryTowerDefense,
                turns=TOWER_TURNS,
            )
        )
    if "reactor" in suites:
        rows.extend(
            await _run_journey(
                client,
                args,
                suite="reactor",
                fixture_type=InMemoryReactor,
                turns=REACTOR_TURNS,
            )
        )
    await client.close()
    edges = _edge_checks() if "edges" in suites else {}
    overall_pass = all(row.full_pass for row in rows)
    if edges:
        overall_pass = overall_pass and bool(edges.get("overall_pass"))
    return {
        "schema": SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "service_tier": args.service_tier,
        "timeout_s": args.timeout,
        "repeats": args.repeats,
        "suites": list(args.suites),
        "product_inert": True,
        "candidate_contract": {
            "model_fields": [
                "candidate_id",
                "instruction_relation",
                "choice_reason",
                "speech",
            ],
            "model_may_emit_payload": False,
            "candidate_revision_bound": True,
            "atomic_authority": "AuipRuntime.invoke_action",
            "narration_record_after": "accepted receipt",
        },
        "overall_pass": overall_pass,
        "summary": _summary(rows),
        "edge_checks": edges,
        "rows": [asdict(row) | {"full_pass": row.full_pass} for row in rows],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "low", "medium", "high", "xhigh", "max"),
        default="low",
    )
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument(
        "--service-tier",
        choices=("auto", "default", "fast", "priority"),
        default="auto",
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument(
        "--suites",
        nargs="+",
        choices=DEFAULT_SUITES,
        default=list(DEFAULT_SUITES),
    )
    parser.add_argument(
        "--standard-scenario",
        nargs="+",
        choices=tuple(s.scenario_id for s in gomoku_probe.SCENARIOS),
        default=[],
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "runtime" / "e2e_reports" / "auip_b2_candidate_chain"),
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    report = asyncio.run(run_probe(args))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"auip_b2_candidate_chain_{stamp}.json"
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(json.dumps(report["edge_checks"], ensure_ascii=False, indent=2))
    print(f"report={output_path}")
    if not report["overall_pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
