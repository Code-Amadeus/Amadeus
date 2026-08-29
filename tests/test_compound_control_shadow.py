"""Contracts for the non-authoritative compound ControlDecision candidate."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.compound_control_shadow import (
    build_decomposition_messages,
    parse_decomposition_reply,
    resolve_compound_control_plan,
)
from server.reference_catalog import TypedReferenceCandidate


ALPHA = TypedReferenceCandidate(
    "work_item",
    "work_alpha",
    "alpha.txt task",
    "session_draft",
    aliases=("alpha.txt",),
)
BETA = TypedReferenceCandidate(
    "work_item",
    "work_beta",
    "beta.txt task",
    "session_draft",
    aliases=("beta.txt",),
)


def _messages(current: str) -> tuple[dict[str, str], ...]:
    return (
        {"role": "system", "content": "production structured-control semantics"},
        {"role": "user", "content": "创建 alpha.txt。"},
        {"role": "assistant", "content": "已委托 alpha。"},
        {"role": "user", "content": "创建 beta.txt。"},
        {"role": "assistant", "content": "已委托 beta。"},
        {"role": "user", "content": current},
    )


def test_parser_requires_exact_ordered_non_overlapping_source_clauses() -> None:
    source = "把 alpha.txt 改成 one-updated；顺便告诉我 beta.txt 现在什么状态。"
    clauses = parse_decomposition_reply(
        '{"clauses":["顺便告诉我 beta.txt 现在什么状态","把 alpha.txt 改成 one-updated"]}',
        source_user_text=source,
    )
    assert [clause.text for clause in clauses] == [
        "把 alpha.txt 改成 one-updated",
        "顺便告诉我 beta.txt 现在什么状态",
    ]
    for reply in (
        '{"clauses":["修改 alpha.txt"]}',
        '{"clauses":["alpha.txt","alpha.txt"]}',
        '{"clauses":["alpha.txt","alpha.txt 改成 one-updated"]}',
        '{"clauses":["a","b","c","d"]}',
        '{"clauses":[" alpha.txt"]}',
        '{"clauses":["alpha.txt"],"extra":true}',
    ):
        try:
            parse_decomposition_reply(reply, source_user_text=source)
        except ValueError:
            pass
        else:
            raise AssertionError(reply)


def test_decomposition_enumerator_keeps_only_bounded_recent_history() -> None:
    source = [
        {"role": "system", "content": "production semantics"},
        *(
            item
            for index in range(6)
            for item in (
                {"role": "user", "content": f"old user {index}"},
                {"role": "assistant", "content": f"old assistant {index}"},
            )
        ),
        {"role": "user", "content": "今天先聊点别的。"},
    ]
    messages = build_decomposition_messages(source)
    system = messages[0]["content"]
    assert "current user's own affirmative control requests" in system
    assert "History may resolve pronouns but may not supply an action" in system
    assert "exact source substring" in system
    assert "independently actionable report" in system
    assert len(messages) == 10
    assert "old user 0" not in str(messages)
    assert "old assistant 1" not in str(messages)
    assert "old user 2" in str(messages)
    assert "old assistant 5" in str(messages)
    assert messages[-1]["content"] == "今天先聊点别的。"


def test_compound_plan_aligns_amend_and_report_to_different_work_items() -> None:
    current = "把 alpha.txt 改成 one-updated；顺便告诉我 beta.txt 现在什么状态。"
    calls: list[str] = []

    async def query(messages: list[dict[str, str]]) -> str:
        joined = "\n".join(message["content"] for message in messages)
        calls.append(joined)
        if "[Compound control decomposition - FINAL]" in joined:
            return (
                '{"clauses":["把 alpha.txt 改成 one-updated",'
                '"顺便告诉我 beta.txt 现在什么状态"]}'
            )
        current_user = next(
            message["content"]
            for message in reversed(messages)
            if message["role"] == "user"
        )
        if "[Independent candidate verdict - FINAL]" in joined:
            return '{"evidence":"none"}'
        if current_user.startswith("把 alpha.txt"):
            return (
                '{"decisions":[{"proposal_index":0,"provider":"codex",'
                '"intent":"amend","subject":"work_item",'
                '"work_placement":"not_applicable",'
                '"session_context":"unchanged","reference_mode":"candidates"}]}'
            )
        if current_user.startswith("顺便告诉我 beta.txt"):
            return (
                '{"decisions":[{"proposal_index":0,"provider":"codex",'
                '"intent":"report","subject":"work_item",'
                '"work_placement":"not_applicable",'
                '"session_context":"unchanged","reference_mode":"candidates"}]}'
            )
        raise AssertionError(current_user)

    plan = asyncio.run(
        resolve_compound_control_plan(
            _messages(current),
            ({"task": current},),
            (ALPHA, BETA),
            complete=True,
            query=query,
            provider_ids={"codex"},
        )
    )
    assert plan.status == "ok", plan.reason
    assert [operation.source_clause for operation in plan.operations] == [
        "把 alpha.txt 改成 one-updated",
        "顺便告诉我 beta.txt 现在什么状态",
    ]
    assert [operation.action["intent"] for operation in plan.operations] == [
        "amend",
        "report",
    ]
    assert [operation.action["workspace_ref"] for operation in plan.operations] == [
        "work_alpha",
        "work_beta",
    ]
    assert [operation.action["task"] for operation in plan.operations] == [
        "把 alpha.txt 改成 one-updated",
        "顺便告诉我 beta.txt 现在什么状态",
    ]
    assert plan.decision_queries == 2
    assert plan.candidate_verdict_queries == 4
    assert len(calls) == 7


def test_single_action_and_no_action_paths_do_not_gain_operations() -> None:
    single = "把 alpha.txt 改成 green。"

    async def single_query(messages: list[dict[str, str]]) -> str:
        joined = "\n".join(message["content"] for message in messages)
        if "[Compound control decomposition - FINAL]" in joined:
            return '{"clauses":["把 alpha.txt 改成 green。"]}'
        if "[Independent candidate verdict - FINAL]" in joined:
            return '{"evidence":"none"}'
        return (
            '{"decisions":[{"proposal_index":0,"provider":"codex",'
            '"intent":"amend","subject":"work_item",'
            '"work_placement":"not_applicable",'
            '"session_context":"unchanged","reference_mode":"candidates"}]}'
        )

    plan = asyncio.run(
        resolve_compound_control_plan(
            _messages(single),
            ({"task": single},),
            (ALPHA, BETA),
            complete=True,
            query=single_query,
            provider_ids={"codex"},
        )
    )
    assert plan.status == "ok"
    assert len(plan.operations) == 1
    assert plan.operations[0].action["workspace_ref"] == "work_alpha"
    assert plan.operations[0].action["task"] == single

    query_count = 0

    async def no_action_query(messages: list[dict[str, str]]) -> str:
        nonlocal query_count
        query_count += 1
        joined = "\n".join(message["content"] for message in messages)
        return (
            '{"clauses":[]}'
            if "[Compound control decomposition - FINAL]" in joined
            else '{"decisions":[]}'
        )

    no_action = asyncio.run(
        resolve_compound_control_plan(
            _messages("只是说说，今天先别改。"),
            ({"task": "stale proposal"},),
            (ALPHA, BETA),
            complete=True,
            query=no_action_query,
            provider_ids={"codex"},
        )
    )
    assert no_action.status == "ok"
    assert no_action.operations == ()
    assert query_count == 2

    never_called = False

    async def should_not_run(_messages: list[dict[str, str]]) -> str:
        nonlocal never_called
        never_called = True
        return '{"clauses":[]}'

    no_gate = asyncio.run(
        resolve_compound_control_plan(
            _messages("把 alpha.txt 改一下。"),
            (),
            (ALPHA,),
            complete=True,
            query=should_not_run,
            provider_ids={"codex"},
        )
    )
    assert no_gate.status == "ok"
    assert no_gate.operations == ()
    assert never_called is False


def test_duplicate_context_constraints_collapse_without_provider_payload() -> None:
    current = "先回到草稿，后面的临时工作不要放进项目。"

    async def query(messages: list[dict[str, str]]) -> str:
        joined = "\n".join(message["content"] for message in messages)
        if "[Compound control decomposition - FINAL]" in joined:
            return (
                '{"clauses":["先回到草稿",'
                '"后面的临时工作不要放进项目"]}'
            )
        return (
            '{"decisions":[{"proposal_index":0,"provider":"codex",'
            '"intent":"focus","work_placement":"not_applicable",'
            '"session_context":"clear","reference_mode":"none"}]}'
        )

    plan = asyncio.run(
        resolve_compound_control_plan(
            _messages(current),
            ({"task": current},),
            (ALPHA, BETA),
            complete=True,
            query=query,
            provider_ids={"codex"},
        )
    )
    assert plan.status == "ok"
    assert len(plan.operations) == 1
    assert plan.operations[0].action["intent"] == "focus"
    assert plan.operations[0].action["focus"] == "clear"
    assert "task" not in plan.operations[0].action


def test_malformed_decomposition_gets_one_bounded_retry() -> None:
    calls = 0

    async def query(messages: list[dict[str, str]]) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return "not json"
        if calls == 2:
            assert "[Compound decomposition protocol repair]" in messages[-1]["content"]
            return '{"clauses":[]}'
        return '{"decisions":[]}'

    plan = asyncio.run(
        resolve_compound_control_plan(
            _messages("只是聊天。"),
            ({"task": "stale"},),
            (),
            complete=True,
            query=query,
            provider_ids={"codex"},
        )
    )
    assert plan.status == "ok"
    assert plan.decomposition_protocol_retries == 1
    assert calls == 3


if __name__ == "__main__":
    test_parser_requires_exact_ordered_non_overlapping_source_clauses()
    test_decomposition_enumerator_keeps_only_bounded_recent_history()
    test_compound_plan_aligns_amend_and_report_to_different_work_items()
    test_single_action_and_no_action_paths_do_not_gain_operations()
    test_duplicate_context_constraints_collapse_without_provider_payload()
    test_malformed_decomposition_gets_one_bounded_retry()
    print("all compound control shadow tests passed")
