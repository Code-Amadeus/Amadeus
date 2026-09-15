"""Already-canonical operations reuse the existing reference evidence owner."""
import json

from server.control_decision import (
    ControlDecision,
    ControlDecisionEntry,
    resolve_control_references,
)
from server.reference_catalog import TypedReferenceCandidate


CATALOG = (
    TypedReferenceCandidate("work_item", "canvas", "Canvas", "session_draft",
        execution="running", session_current=True),
    TypedReferenceCandidate("work_item", "portal", "Portal", "session_draft",
        execution="running"),
)


def canonical():
    return ControlDecision(status="ok", raw_reply="sealed operation", entries=(
        ControlDecisionEntry(proposal_index=0,
            control={"provider": "codex", "intent": "amend", "subject": "work_item"},
            work_placement="not_applicable", session_context="unchanged",
            workspace_effect="write", reference_kind="work_item", reference_candidates=()),))


async def resolve(source, query, *, complete=True):
    return await resolve_control_references(canonical(),
        [{"role": "system", "content": "captured context"},
         {"role": "user", "content": source}],
        ({"task": source},), CATALOG, complete=complete, query=query,
        proposal_controls=({"provider": "codex", "intent": "amend"},),
        recover_zero_matches=False)


async def test_empty_evidence_does_not_reenter_a_global_chooser_or_erase_operation():
    queries = []

    async def query(messages):
        assert "[Independent candidate verdict - FINAL]" in messages[0]["content"]
        queries.append(messages)
        return '{"evidence":"none"}'

    decision = await resolve("继续海战那个任务，加个暂停按钮。", query)
    assert decision.status == "ok" and len(decision.entries) == 1
    assert decision.entries[0].control == canonical().entries[0].control
    assert decision.entries[0].reference_candidates == ()
    assert decision.raw_reply == "sealed operation"
    assert len(queries) == decision.candidate_verdict_queries == 2


async def test_positive_fuzzy_evidence_remains_available_without_zero_recovery():
    async def query(messages):
        assert "[Independent candidate verdict - FINAL]" in messages[0]["content"]
        evidence = "partial" if "work_item:canvas" in messages[-1]["content"] else "none"
        return json.dumps({"evidence": evidence})

    decision = await resolve("继续那个画板任务，加个暂停按钮。", query)
    assert decision.status == "ok"
    assert decision.entries[0].reference_candidates == (CATALOG[0],)
    assert decision.candidate_verdict_queries == 2


async def test_contextual_refinement_cannot_erase_positive_ambiguity():
    async def query(messages):
        if "[Independent candidate verdict - FINAL]" in messages[0]["content"]:
            return '{"evidence":"contextual"}'
        return '{"references":[]}'

    decision = await resolve("继续刚才那个任务，加个暂停按钮。", query)
    assert decision.status == "ok"
    assert decision.entries[0].reference_candidates == CATALOG
    assert decision.candidate_verdict_queries == 3


async def test_incomplete_catalog_does_not_start_an_evidence_query():
    async def query(_messages):
        raise AssertionError("incomplete catalog must not query")

    decision = await resolve("继续刚才那个任务。", query, complete=False)
    assert decision.status == "incomplete" and not decision.entries
