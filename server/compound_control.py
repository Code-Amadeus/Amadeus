"""Shared whole-turn plan types and exact current-source clause validation."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Literal, Mapping, Sequence

CompoundPlanStatus = Literal["ok", "invalid", "unavailable", "incomplete"]
MAX_COMPOUND_OPERATIONS = 3


@dataclass(frozen=True, slots=True)
class SourceClause:
    text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class CompoundControlOperation:
    operation_index: int
    source_clause: str
    action: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CompoundControlPlan:
    status: CompoundPlanStatus
    operations: tuple[CompoundControlOperation, ...] = ()
    clauses: tuple[SourceClause, ...] = ()
    raw_reply: str = ""
    reason: str = ""
    decomposition_protocol_retries: int = 0
    decision_queries: int = 0
    candidate_verdict_queries: int = 0
    candidate_protocol_retries: int = 0


def parse_decomposition_reply(reply: str, *, source_user_text: str) -> tuple[SourceClause, ...]:
    """Validate source provenance and derive order without trusting model indexes."""

    raw = str(reply or "").strip()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"decomposition reply is not exact JSON: {exc}") from exc
    if not isinstance(parsed, dict) or set(parsed) != {"clauses"}:
        raise ValueError("decomposition root must contain only clauses")
    values = parsed.get("clauses")
    if not isinstance(values, list) or len(values) > MAX_COMPOUND_OPERATIONS:
        raise ValueError(
            f"clauses must be a list of at most {MAX_COMPOUND_OPERATIONS} items"
        )
    source = str(source_user_text or "")
    clauses: list[SourceClause] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError("every clause must be one non-empty trimmed string")
        if value in seen:
            raise ValueError("duplicate source clause")
        start = source.find(value)
        if start < 0 or source.find(value, start + 1) >= 0:
            raise ValueError("clause is not one uniquely occurring exact source substring")
        end = start + len(value)
        clauses.append(SourceClause(value, start, end))
        seen.add(value)
    clauses.sort(key=lambda clause: clause.start)
    for previous, current in zip(clauses, clauses[1:]):
        if current.start < previous.end:
            raise ValueError("source clauses overlap")
    return tuple(clauses)


def current_user_text(messages: Sequence[Mapping[str, str]]) -> str:
    """Shared exact-source lookup for clause and whole-turn planning."""
    return next(
        (
            str(message.get("content") or "")
            for message in reversed(messages)
            if str(message.get("role") or "") == "user"
        ),
        "",
    )


def operation_control_view(operation: CompoundControlOperation) -> dict[str, Any]:
    """Stable, payload-free comparison view for A/B reports."""

    hidden = {"task", "url", "query", "text"}
    return {
        key: value
        for key, value in operation.action.items()
        if key not in hidden and not str(key).startswith("_host_")
    }
