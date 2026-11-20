"""Typed reference ambiguity becomes a hierarchical Slice selection."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.attention_request import AttentionRequestCoordinator
from server.reference_clarification import (
    TypedReferenceCandidate,
    adjudicate_focus_reference,
    candidate_catalog_from_coordinator,
    clarification_announcement,
    parse_reference_reply,
    plan_resume,
)


PROJECT = TypedReferenceCandidate(
    kind="project",
    entity_id="project-chess",
    label="象棋",
    scope="persistent",
    recency_rank=2,
)
PROJECT_ITEM = TypedReferenceCandidate(
    kind="work_item",
    entity_id="work-chess-fix",
    label="象棋双人模式",
    scope="project",
    parent_project_id="project-chess",
    parent_project_label="象棋",
    recency_rank=1,
)
DRAFT_ITEM = TypedReferenceCandidate(
    kind="work_item",
    entity_id="work-chess-draft",
    label="临时象棋原型",
    scope="session_draft",
    recency_rank=2,
)


class CatalogCoordinator:
    def workspace_routing_context(self, *, limit: int):
        assert limit == 200
        return {
            "candidates": [
                {"projectId": "project-chess", "projectName": "象棋"},
                {
                    "projectId": "project-amadeus",
                    "projectName": "Amadeus",
                    "projectAliases": ["assistant control plane", "amadeus repo"],
                },
            ],
            "candidateCount": 2,
            "candidatesComplete": True,
        }

    def conversation_work_items_for_resolution(self, session_id: str, *, limit: int):
        assert session_id == "session-current"
        assert limit == 200
        return {
            "complete": True,
            "items": [
                {
                    "work_item_id": "work-chess-fix",
                    "project_id": "project-chess",
                    "title": "象棋双人模式",
                },
                {
                    "work_item_id": "work-chess-draft",
                    "project_id": "scratch-container",
                    "title": "临时象棋原型",
                    "source_user_text": "另外做个一次性的象棋游戏",
                    "files": ["chess.html"],
                },
            ],
        }


def test_catalog_preserves_project_work_item_hierarchy() -> None:
    candidates, complete, reason = candidate_catalog_from_coordinator(
        CatalogCoordinator(), "session-current"
    )
    assert complete is True and reason == ""
    by_token = {candidate.token: candidate for candidate in candidates}
    assert by_token["work_item:work-chess-fix"].parent_project_id == "project-chess"
    assert by_token["work_item:work-chess-fix"].parent_project_label == "象棋"
    assert by_token["work_item:work-chess-draft"].scope == "session_draft"
    assert by_token["work_item:work-chess-draft"].parent_project_id == ""
    assert by_token["project:project-amadeus"].aliases == (
        "assistant control plane",
        "amadeus repo",
    )
    assert by_token["work_item:work-chess-draft"].aliases == (
        "另外做个一次性的象棋游戏",
        "chess.html",
    )


def test_catalog_uses_registry_membership_not_display_label_for_ownership() -> None:
    class BlankLabelCoordinator:
        @staticmethod
        def workspace_routing_context(*, limit: int):
            return {
                "candidates": [{"projectId": "project-blank", "projectName": ""}],
                "candidateCount": 1,
                "candidatesComplete": True,
            }

        @staticmethod
        def conversation_work_items_for_resolution(session_id: str, *, limit: int):
            return {
                "complete": True,
                "items": [
                    {
                        "work_item_id": "work-owned",
                        "project_id": "project-blank",
                        "title": "Owned delivery",
                    },
                    {
                        "work_item_id": "work-draft",
                        "project_id": "scratch-container",
                        "title": "Draft delivery",
                    },
                ],
            }

    candidates, complete, reason = candidate_catalog_from_coordinator(
        BlankLabelCoordinator(), "session-current"
    )
    assert complete is True and reason == ""
    by_token = {candidate.token: candidate for candidate in candidates}
    assert by_token["work_item:work-owned"].scope == "project"
    assert by_token["work_item:work-owned"].parent_project_id == "project-blank"
    assert by_token["work_item:work-draft"].scope == "session_draft"
    assert by_token["work_item:work-draft"].parent_project_id == ""


def test_parser_preserves_parent_child_ambiguity() -> None:
    result = parse_reference_reply(
        '{"references":["project:project-chess","work_item:work-chess-fix"]}',
        [PROJECT_ITEM, PROJECT],
    )
    assert result.status == "ambiguous"
    assert result.candidates == (PROJECT_ITEM, PROJECT)
    invalid = parse_reference_reply(
        '{"references":["project:unknown"]}',
        [PROJECT],
    )
    assert invalid.status == "invalid"


def test_resume_plans_keep_project_and_work_item_effects_distinct() -> None:
    attrs = {"intent": "execute", "focus": "set", "project_id": "old"}
    project = plan_resume(
        session_id="session-current", task_text="修复计时器", attrs=attrs, candidate=PROJECT
    )
    assert project.kind == "delegate"
    assert project.attrs["focus"] == "set"
    assert project.attrs["project_id"] == "project-chess"

    work = plan_resume(
        session_id="session-current",
        task_text="修复计时器",
        attrs=attrs,
        candidate=PROJECT_ITEM,
    )
    assert work.kind == "delegate"
    assert work.attrs["intent"] == "amend"
    assert work.attrs["workspace_ref"] == "work-chess-fix"
    assert "focus" not in work.attrs and "project_id" not in work.attrs

    owned_switch = plan_resume(
        session_id="session-current", task_text="", attrs=attrs, candidate=PROJECT_ITEM
    )
    assert owned_switch.kind == "bind_work_item"
    draft_switch = plan_resume(
        session_id="session-current", task_text="", attrs=attrs, candidate=DRAFT_ITEM
    )
    assert draft_switch.kind == "acknowledge"


def test_clarification_announcement_does_not_claim_ui_receipt() -> None:
    chinese, japanese = clarification_announcement()
    assert "需要你选一个" in chinese
    assert "一つ選んでください" in japanese
    for text in (chinese, japanese):
        assert "Slice" not in text
        assert "显示" not in text
        assert "表示しました" not in text


async def test_ordinary_or_clear_controls_bypass_without_query() -> None:
    query_calls = 0

    async def query(_messages):
        nonlocal query_calls
        query_calls += 1
        return '{"references":[]}'

    async def resume(_plan):
        raise AssertionError("bypass must not install a continuation")

    for attrs in ({}, {"focus": "clear"}, {"intent": "execute"}):
        result = await adjudicate_focus_reference(
            coordinator=CatalogCoordinator(),
            session_id="session-current",
            utterance="普通聊天",
            task_text="",
            attrs=attrs,
            query=query,
            resume=resume,
        )
        assert result.status == "bypass"
    assert query_calls == 0


async def test_unique_project_is_corrected_without_card() -> None:
    attention = AttentionRequestCoordinator()

    async def query(_messages):
        return '{"references":["project:project-chess"]}'

    async def resume(_plan):
        raise AssertionError("unique references resume through the existing handler")

    result = await adjudicate_focus_reference(
        coordinator=CatalogCoordinator(),
        session_id="session-current",
        utterance="切回象棋项目",
        task_text="",
        attrs={"intent": "focus", "project_id": "project-amadeus"},
        query=query,
        resume=resume,
        attention=attention,
    )
    assert result.status == "resolved"
    assert result.attrs["project_id"] == "project-chess"
    assert result.attrs["_host_reference_resolved"] is True
    assert attention.list_pending("session-current") == []


async def test_ambiguity_waits_for_click_then_resumes_exactly_once() -> None:
    attention = AttentionRequestCoordinator()
    plans = []

    async def query(_messages):
        return '{"references":["work_item:work-chess-fix","project:project-chess"]}'

    async def resume(plan):
        plans.append(plan)
        return {"resumed": True}

    result = await adjudicate_focus_reference(
        coordinator=CatalogCoordinator(),
        session_id="session-current",
        utterance="切回刚才那个象棋，然后修复计时器",
        task_text="修复计时器",
        attrs={"intent": "execute", "focus": "set", "project_id": "project-chess"},
        query=query,
        resume=resume,
        attention=attention,
    )
    assert result.status == "deferred"
    assert plans == []
    request = attention.list_pending("session-current")[0]
    assert [option["entityKind"] for option in request["options"]] == [
        "project",
        "work_item",
    ]
    assert {option["entityKind"] for option in request["options"]} == {
        "project",
        "work_item",
    }
    work_option = next(
        option for option in request["options"] if option["entityKind"] == "work_item"
    )
    assert work_option["parentLabel"] == "象棋"
    resolved = await attention.resolve(
        session_id="session-current",
        request_id=request["id"],
        option_id=work_option["id"],
    )
    assert resolved["ok"] is True
    assert len(plans) == 1
    assert plans[0].attrs["intent"] == "amend"
    duplicate = await attention.resolve(
        session_id="session-current",
        request_id=request["id"],
        option_id=work_option["id"],
    )
    assert duplicate["ok"] is False
    assert len(plans) == 1
    attention.reset_for_tests()


async def test_report_cannot_replace_an_ambiguous_context_switch() -> None:
    attention = AttentionRequestCoordinator()
    plans = []

    switch_replies = iter(
        (
            '{"context_switch":true}',
            '{"references":["work_item:work-chess-fix","project:project-chess"]}',
        )
    )

    async def switch_query(_messages):
        return next(switch_replies)

    async def resume(plan):
        plans.append(plan)
        return {}

    result = await adjudicate_focus_reference(
        coordinator=CatalogCoordinator(),
        session_id="session-current",
        utterance="切回刚才那个象棋",
        task_text="先查询这个任务属于哪里",
        attrs={"provider": "locus", "intent": "report", "subject": "work_item"},
        query=switch_query,
        resume=resume,
        attention=attention,
    )
    assert result.status == "deferred"
    request = attention.list_pending("session-current")[0]
    project_option = next(
        option for option in request["options"] if option["entityKind"] == "project"
    )
    await attention.resolve(
        session_id="session-current",
        request_id=request["id"],
        option_id=project_option["id"],
    )
    assert len(plans) == 1
    assert plans[0].task_text == ""
    assert plans[0].attrs["intent"] == "focus"
    assert plans[0].attrs["project_id"] == "project-chess"
    assert "subject" not in plans[0].attrs
    attention.reset_for_tests()

    query_calls = 0

    async def status_query(_messages):
        nonlocal query_calls
        query_calls += 1
        return '{"context_switch":false}'

    status = await adjudicate_focus_reference(
        coordinator=CatalogCoordinator(),
        session_id="session-current",
        utterance="刚才那个象棋任务做完了吗",
        task_text="查询状态",
        attrs={"provider": "locus", "intent": "report", "subject": "work_item"},
        query=status_query,
        resume=resume,
        attention=attention,
    )
    assert query_calls == 1
    assert status.status == "bypass"
    assert attention.list_pending("session-current") == []


async def main() -> None:
    test_catalog_preserves_project_work_item_hierarchy()
    test_parser_preserves_parent_child_ambiguity()
    test_resume_plans_keep_project_and_work_item_effects_distinct()
    test_clarification_announcement_does_not_claim_ui_receipt()
    await test_ordinary_or_clear_controls_bypass_without_query()
    await test_unique_project_is_corrected_without_card()
    await test_ambiguity_waits_for_click_then_resumes_exactly_once()
    await test_report_cannot_replace_an_ambiguous_context_switch()
    print("all reference clarification tests passed")


if __name__ == "__main__":
    asyncio.run(main())
