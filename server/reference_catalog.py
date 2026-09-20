"""Host-owned typed Project/WorkItem reference catalog.

This module contains facts only: durable Projects, WorkItems reachable from the
current Session, validation, and prompt-safe rendering.  It owns no LLM query,
control decision, Provider routing, or Attention UI.  Those consumers must all
use the same frozen catalog so entity ambiguity cannot change between layers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping, Sequence


ReferenceKind = Literal["project", "work_item"]

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class TypedReferenceCandidate:
    kind: ReferenceKind
    entity_id: str
    label: str
    scope: Literal["persistent", "project", "session_draft"]
    parent_project_id: str = ""
    parent_project_label: str = ""
    recency_rank: int = 0
    aliases: tuple[str, ...] = ()
    session_focus: bool = False
    state: str = ""
    execution: str = ""
    relation: str = ""
    # The durable workspace pin and the conversational subject are different
    # authorities. ``session_focus`` is the legacy workspace-pin projection;
    # ``session_current`` names the WorkItem/Project the current conversation
    # most recently attached.
    session_current: bool = False

    @property
    def token(self) -> str:
        return f"{self.kind}:{self.entity_id}"


def validate_candidate_catalog(candidates: Sequence[TypedReferenceCandidate]) -> str:
    """Return an invariant failure, or an empty string for a safe catalog."""

    tokens: list[str] = []
    for candidate in candidates:
        if candidate.kind not in {"project", "work_item"}:
            return "candidate catalog contains an invalid kind"
        if _ID_RE.fullmatch(str(candidate.entity_id or "")) is None:
            return "candidate catalog contains an invalid id"
        if candidate.kind == "project" and candidate.scope != "persistent":
            return "Project candidates must be persistent"
        if candidate.kind == "work_item" and candidate.scope not in {
            "project",
            "session_draft",
        }:
            return "WorkItem candidates require an explicit owner scope"
        if candidate.scope == "project" and not candidate.parent_project_id:
            return "Project-owned WorkItems require a parent Project"
        tokens.append(candidate.token)
    if len(tokens) != len(set(tokens)):
        return "candidate catalog contains duplicate typed ids"
    return ""


def candidate_catalog_from_coordinator(
    coordinator,
    session_id: str,
    *,
    project_limit: int = 200,
    work_item_limit: int = 200,
) -> tuple[tuple[TypedReferenceCandidate, ...], bool, str]:
    """Freeze durable Projects and current-Session WorkItems for one decision."""

    try:
        routing = coordinator.workspace_routing_context(limit=project_limit)
        project_rows = routing.get("candidates") if isinstance(routing, dict) else []
        focus = (
            routing.get("focus")
            if isinstance(routing, dict) and isinstance(routing.get("focus"), Mapping)
            else {}
        )
        focus_project_id = str(focus.get("projectId") or "").strip()
        focus_work_item_id = str(focus.get("workItemId") or "").strip()
        conversation_binding = {}
        binding_reader = getattr(coordinator, "conversation_binding", None)
        if callable(binding_reader):
            value = binding_reader(str(session_id or "").strip())
            if isinstance(value, Mapping):
                conversation_binding = value
        session_work_item_id = str(
            conversation_binding.get("workItemId") or ""
        ).strip()
        session_project_id = str(
            conversation_binding.get("defaultProjectId")
            or conversation_binding.get("projectId")
            or ""
        ).strip()
        projects_complete = bool(
            isinstance(routing, dict)
            and routing.get("candidatesComplete")
            and int(routing.get("candidateCount") or len(project_rows or []))
            == len(project_rows or [])
        )
        projects = tuple(
            TypedReferenceCandidate(
                kind="project",
                entity_id=str(row.get("projectId") or ""),
                label=str(row.get("projectName") or ""),
                scope="persistent",
                recency_rank=index,
                aliases=tuple(
                    dict.fromkeys(
                        str(alias).strip()
                        for alias in (row.get("projectAliases") or [])
                        if str(alias).strip()
                    )
                ),
                session_focus=(
                    bool(focus_project_id)
                    and str(row.get("projectId") or "") == focus_project_id
                ),
                session_current=(
                    bool(session_project_id)
                    and str(row.get("projectId") or "") == session_project_id
                ),
            )
            for index, row in enumerate(project_rows or [], start=1)
            if isinstance(row, Mapping) and str(row.get("projectId") or "")
        )
        project_labels = {
            candidate.entity_id: candidate.label for candidate in projects
        }

        work_payload = coordinator.conversation_work_items_for_resolution(
            str(session_id or "").strip(), limit=work_item_limit
        )
        work_rows = work_payload.get("items") if isinstance(work_payload, dict) else []
        work_complete = bool(
            isinstance(work_payload, dict) and work_payload.get("complete")
        )
        work_items: list[TypedReferenceCandidate] = []
        for index, row in enumerate(work_rows or [], start=1):
            if not isinstance(row, Mapping):
                continue
            work_item_id = str(row.get("work_item_id") or "").strip()
            if not work_item_id:
                continue
            parent_id = str(row.get("project_id") or "").strip()
            parent_is_project = parent_id in project_labels
            parent_label = project_labels.get(parent_id, "")
            work_items.append(
                TypedReferenceCandidate(
                    kind="work_item",
                    entity_id=work_item_id,
                    label=(
                        str(row.get("title") or "").strip()
                        or "/".join(str(name) for name in (row.get("files") or [])[:3])
                        or "Untitled WorkItem"
                    ),
                    # Registry membership is the ownership fact. A scratch
                    # container can also have an id, while a registered
                    # Project may legitimately have a blank display label.
                    scope="project" if parent_is_project else "session_draft",
                    parent_project_id=parent_id if parent_is_project else "",
                    parent_project_label=parent_label,
                    recency_rank=index,
                    aliases=tuple(
                        dict.fromkeys(
                            text
                            for text in (
                                str(row.get("source_user_text") or "").strip(),
                                *(
                                    str(name).strip()
                                    for name in (row.get("files") or [])[:8]
                                ),
                            )
                            if text
                        )
                    ),
                    session_focus=(
                        bool(focus_work_item_id)
                        and work_item_id == focus_work_item_id
                    ),
                    state=str(row.get("state") or ""),
                    execution=str(row.get("execution") or ""),
                    relation=str(row.get("relation") or ""),
                    session_current=(
                        bool(session_work_item_id)
                        and work_item_id == session_work_item_id
                    ),
                )
            )
        candidates = (*work_items, *projects)
        error = validate_candidate_catalog(candidates)
        if error:
            return (), False, error
        complete = projects_complete and work_complete
        return tuple(candidates), complete, "" if complete else "catalog incomplete"
    except Exception as exc:
        return (), False, f"{type(exc).__name__}: {exc}"


def amend_candidates_from_host_rows(
    coordinator,
    rows: Iterable[Mapping[str, Any]],
) -> tuple[TypedReferenceCandidate, ...]:
    """Rehydrate a grounded amend ambiguity without another model lookup."""

    routing = coordinator.workspace_routing_context(limit=200)
    project_rows = routing.get("candidates") if isinstance(routing, dict) else []
    project_labels = {
        str(row.get("projectId") or ""): str(row.get("projectName") or "")
        for row in (project_rows or [])
        if isinstance(row, Mapping) and str(row.get("projectId") or "")
    }
    candidates: list[TypedReferenceCandidate] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            continue
        work_item_id = str(row.get("work_item_id") or "").strip()
        if not work_item_id:
            continue
        project_id = str(row.get("project_id") or "").strip()
        parent_is_project = project_id in project_labels
        parent_label = project_labels.get(project_id, "")
        candidates.append(
            TypedReferenceCandidate(
                kind="work_item",
                entity_id=work_item_id,
                label=(
                    str(row.get("title") or "").strip()
                    or "/".join(str(name) for name in (row.get("files") or [])[:3])
                    or "Untitled WorkItem"
                ),
                scope="project" if parent_is_project else "session_draft",
                parent_project_id=project_id if parent_is_project else "",
                parent_project_label=parent_label,
                recency_rank=index,
                aliases=tuple(
                    str(name).strip()
                    for name in (row.get("files") or [])[:8]
                    if str(name).strip()
                ),
                state=str(row.get("state") or ""),
                execution=str(row.get("execution") or ""),
                relation=str(row.get("relation") or ""),
            )
        )
    error = validate_candidate_catalog(candidates)
    return tuple(candidates) if not error else ()


def _safe_text(value: Any, *, limit: int = 160) -> str:
    return (
        " ".join(str(value or "").split())[:limit]
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("[", "\\u005b")
        .replace("]", "\\u005d")
    )


def render_candidate_rows(candidates: Sequence[TypedReferenceCandidate]) -> str:
    """Render typed semantic data without allowing new prompt sections."""

    rows: list[str] = []
    for index, candidate in enumerate(candidates, start=1):
        parts = [candidate.token, candidate.kind, _safe_text(candidate.label)]
        parts.append(f"scope={candidate.scope}")
        if candidate.parent_project_id:
            parts.append(f"parent_project={candidate.parent_project_id}")
            parts.append(f"parent_label={_safe_text(candidate.parent_project_label)}")
        if candidate.aliases:
            parts.append(
                "aliases="
                + " ; ".join(
                    _safe_text(alias, limit=200) for alias in candidate.aliases[:6]
                )
            )
        if candidate.session_focus:
            parts.append("session_focus=true")
        if candidate.session_current:
            parts.append("session_current=true")
        if candidate.state:
            parts.append(f"state={_safe_text(candidate.state, limit=48)}")
        if candidate.execution:
            parts.append(
                f"execution={_safe_text(candidate.execution, limit=48)}"
            )
        if candidate.relation:
            parts.append(f"relation={_safe_text(candidate.relation, limit=48)}")
        parts.append(f"recency_rank={candidate.recency_rank or index}")
        rows.append(f"- {index} | " + " | ".join(parts))
    return "\n".join(rows)
