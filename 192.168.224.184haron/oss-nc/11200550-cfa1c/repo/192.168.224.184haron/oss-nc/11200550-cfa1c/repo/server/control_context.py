"""Capture shared Host facts for semantic planners without adjudication/dispatch."""

from dataclasses import dataclass
from typing import Mapping

from server.control_proposal import ControlProposalBatch
from server.reference_catalog import TypedReferenceCandidate, candidate_catalog_from_coordinator


@dataclass(frozen=True, slots=True)
class ControlDecisionContext:
    messages: tuple[Mapping[str, str], ...]
    candidates: tuple[TypedReferenceCandidate, ...]
    catalog_complete: bool
    exhaustive_candidate_limit: int
    provider_ids: frozenset[str]


def capture_control_context(
    coordinator, batch: ControlProposalBatch, *, semantic_prompt: str,
    project_limit: int, work_item_limit: int,
    exhaustive_candidate_limit: int, include_app_capabilities: bool = False,
) -> ControlDecisionContext:
    from llm.prompts import registered_provider_ids
    from server.work_context import augment_system_prompt_for_control_decision

    candidates, complete, _catalog_reason = candidate_catalog_from_coordinator(
        coordinator, batch.session_id,
        project_limit=max(1, int(project_limit)), work_item_limit=max(1, int(work_item_limit)),
    )
    system_prompt = augment_system_prompt_for_control_decision(
        semantic_prompt, session_id=batch.session_id,
        include_app_capabilities=include_app_capabilities,
    )
    messages = (
        {"role": "system", "content": system_prompt},
        *({"role": str(message.get("role") or ""), "content": str(message.get("content") or "")}
          for message in batch.prior_messages),
        {"role": "user", "content": batch.user_text},
    )
    return ControlDecisionContext(
        messages=messages, candidates=candidates, catalog_complete=complete,
        exhaustive_candidate_limit=max(1, int(exhaustive_candidate_limit)),
        provider_ids=frozenset(registered_provider_ids()),
    )
