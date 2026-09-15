"""Whole-turn operations keep their source while shared evidence resolves identity."""
import json

from server.control_decision import CONTROL_REFERENCE_CANDIDATES_ATTR as REFS
from server.reference_catalog import TypedReferenceCandidate
from server.whole_turn_control import parse_whole_turn_reply, resolve_whole_turn_references


CATALOG = (
    TypedReferenceCandidate("work_item", "canvas", "Canvas", "session_draft"),
    TypedReferenceCandidate("work_item", "portal", "Portal", "session_draft"),
)


def row(source, *, index=0, intent="amend", references=("work_item:canvas",)):
    return {"proposal_index":index, "source_clause":source, "provider":"codex",
        "intent":intent, **({"subject":"work_item"} if references is not None else {}),
        "work_placement":"draft" if references is None else "not_applicable",
        "session_context":"unchanged", "workspace_effect":"none" if intent=="report" else "write",
        "payload_continuity":"current_turn", "reference_mode":"none" if references is None else "candidates",
        "references":None if references is None else list(references)}


async def resolve(rows, source, query, *, limit=64):
    raw = json.dumps({"decisions":rows}, ensure_ascii=False)
    plan = parse_whole_turn_reply(raw, source=source, candidates=CATALOG, provider_ids=("codex",))
    assert plan.status == "ok", plan.reason
    resolved = await resolve_whole_turn_references(plan,
        [{"role":"system", "content":"captured facts"}, {"role":"user", "content":source}],
        ({"task":source},), CATALOG, complete=True, query=query,
        provider_ids=("codex",), candidate_limit=limit)
    return plan, resolved


async def test_wrong_joint_target_becomes_empty_without_erasing_work_or_raw_evidence():
    source = "继续海战那个任务，加个暂停按钮。"

    async def query(messages):
        assert "[Independent candidate verdict - FINAL]" in messages[0]["content"]
        return '{"evidence":"none"}'

    original, grounded = await resolve([row(source)], source, query)
    assert grounded.status == "ok" and len(grounded.operations) == 1
    op = grounded.operations[0]
    assert op.source_clause == source and op.action["intent"] == "amend"
    assert op.action[REFS] == ()
    assert "workspace_ref" not in op.action
    assert grounded.raw_reply == original.raw_reply
    assert grounded.candidate_verdict_queries == 2


async def test_multi_operation_sources_do_not_lend_each_other_literal_identity():
    first, second = "告诉我 Canvas 的进度。", "给 Portal 加个搜索框。"
    source = first + second
    queries = []

    async def query(messages):
        queries.append(messages[-1]["content"])
        return '{"evidence":"none"}'

    _, grounded = await resolve([row(first, index=2, intent="report"),
        row(second, index=0, references=("work_item:canvas",))], source, query)
    assert grounded.status == "ok" and len(grounded.operations) == 2
    assert [op.source_clause for op in grounded.operations] == [first, second]
    assert [op.action["intent"] for op in grounded.operations] == ["report", "amend"]
    assert [op.action[REFS] for op in grounded.operations] == [(CATALOG[0],), (CATALOG[1],)]
    assert all(text.startswith(first + "\n\n") or text.startswith(second + "\n\n") for text in queries)
    assert all("same_turn_reference_data=" in text for text in queries)


async def test_later_reference_protocol_failure_invalidates_the_complete_plan():
    first, second = "告诉我 Canvas 的进度。", "给 Portal 加个搜索框。"

    async def query(messages):
        return '{"evidence":"none"}' if messages[-1]["content"].startswith(first) else 'not JSON'

    _, grounded = await resolve([row(first, intent="report"), row(second, index=1)], first+second, query)
    assert grounded.status == "invalid" and not grounded.operations
    assert grounded.candidate_protocol_retries > 0


async def test_sibling_name_cannot_replace_a_missing_target_without_positive_evidence():
    first, second = "告诉我 Canvas 的进度。", "给海战加个暂停按钮。"

    async def query(_messages):
        return '{"evidence":"none"}'

    _, grounded = await resolve([row(first, intent="report"), row(second, index=1)],
        first+second, query)
    assert grounded.status == "ok" and len(grounded.operations) == 2
    assert grounded.operations[0].action[REFS] == (CATALOG[0],)
    assert grounded.operations[1].action[REFS] == ()


async def test_sibling_name_still_grounds_a_positively_linked_pronoun():
    first, second = "告诉我 Canvas 的进度。", "再给它加个暂停按钮。"

    async def query(messages):
        return json.dumps({"evidence":"contextual"
            if "work_item:canvas" in messages[-1]["content"] else "none"})

    _, grounded = await resolve([row(first, intent="report"), row(second, index=1)],
        first+second, query)
    assert grounded.status == "ok" and len(grounded.operations) == 2
    assert all(op.action[REFS] == (CATALOG[0],) for op in grounded.operations)


async def test_reference_budget_is_shared_across_operations_and_repairs():
    first, second = "告诉我 Canvas 的进度。", "给 Portal 加个搜索框。"
    calls = []

    async def query(messages):
        calls.append(messages)
        assert messages[-1]["content"].startswith(first) or "[Host protocol repair]" in messages[-1]["content"]
        return 'bad' if len(calls) == 1 else '{"evidence":"none"}'

    _, grounded = await resolve([row(first, intent="report"), row(second, index=1)], first+second, query, limit=4)
    assert grounded.status == "incomplete" and not grounded.operations
    assert len(calls) == grounded.candidate_verdict_queries == 3


async def test_new_work_without_references_does_not_add_an_identity_query():
    source = "做个计时器吧。"

    async def query(_messages):
        raise AssertionError("new Work does not need target evidence")

    original, grounded = await resolve([row(source, intent="execute", references=None)], source, query)
    assert grounded == original and grounded.candidate_verdict_queries == 0


async def test_current_source_payload_policy_survives_reference_recompile():
    source = "继续 Canvas 那个任务。"
    item = row(source)
    item["payload_continuity"] = "confirmed_prior_request"
    raw = json.dumps({"decisions":[item]}, ensure_ascii=False)
    proposals = ({"task":"An older accepted Canvas task."},)
    controls = ({"provider":"codex", "task":"An older accepted Canvas task."},)
    plan = parse_whole_turn_reply(
        raw,
        source=source,
        candidates=CATALOG,
        provider_ids=("codex",),
        proposals=proposals,
        proposal_controls=controls,
        payload_policy="current_source",
    )
    assert plan.status == "ok"

    async def query(messages):
        return json.dumps({"evidence":
            "exact" if CATALOG[0].token in messages[-1]["content"] else "none"})

    resolved = await resolve_whole_turn_references(
        plan,
        [{"role":"system", "content":"captured facts"},
            {"role":"user", "content":source}],
        proposals,
        CATALOG,
        complete=True,
        query=query,
        provider_ids=("codex",),
        proposal_controls=controls,
        payload_policy="current_source",
    )
    assert resolved.status == "ok" and resolved.raw_reply == raw
    action = resolved.operations[0].action
    assert action["task"] == source
    assert action["_host_payload_source"] == "exact_current_user_clause"
    assert "_host_control_payload_grounding" not in action
    assert action[REFS] == (CATALOG[0],)
