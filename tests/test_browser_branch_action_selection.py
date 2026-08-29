"""Browser branch action selection must obey the latest user instruction."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.adapters.browser_branch import (
    _extract_search_query,
    _with_deterministic_action_if_needed,
)
from server.browser_branch_planner import _planner_payload


def _context(latest: str) -> dict:
    return {
        "user_message": (
            "Continue the active browser interaction branch.\n"
            f"Latest user instruction: {latest}\n"
            "Branch goal: Open https://www.bilibili.com/."
        ),
        "latest_user_instruction": latest,
        "branch_transcript": [
            {"role": "user", "content": "请在当前页面搜索 Amadeus。"},
            {"role": "user", "content": latest},
        ],
        "request_metadata": {
            "branch_user_message": latest,
            "branch_pending_goal": "请在当前页面搜索 Amadeus。",
        },
        "interaction_refs": [
            {
                "ref": "br_10",
                "kind": "input",
                "role": "textbox",
                "label": "输入关键字搜索",
                "fillable": True,
            }
        ],
        "page": {"url": "https://search.bilibili.com/all?keyword=Amadeus"},
    }


def test_search_result_noun_is_not_a_search_command() -> None:
    latest = "观察搜索结果，如果有明确的视频结果就打开第一个合理结果；如果没有就只汇报当前页面状态。"
    assert _extract_search_query(_context(latest)) == ""


def test_passive_continue_does_not_replay_an_older_query() -> None:
    assert _extract_search_query(_context("好，继续。")) == ""


def test_explicit_latest_search_still_has_a_deterministic_fallback() -> None:
    context = _context("请在当前页面搜索 Amadeus。")
    decision = _with_deterministic_action_if_needed(
        {
            "actions": [],
            "final_report": "操作対象を特定できなかったわ。",
            "planner": "browser_branch_llm",
        },
        context,
    )
    assert decision["planner"] == "browser_branch_deterministic_fallback"
    assert decision["actions"] == [
        {
            "action": "fill_ref",
            "ref": "br_10",
            "value": "Amadeus",
            "submit": True,
            "task": "Search current page for Amadeus",
        }
    ]


def test_no_action_report_is_preserved_for_conditional_observation() -> None:
    latest = "观察搜索结果，如果有明确的视频结果就打开第一个合理结果；如果没有就只汇报当前页面状态。"
    decision = {
        "actions": [],
        "final_report": "現在のページ状態だけ報告するわ。",
        "reason": "No matching result is present.",
        "planner": "browser_branch_llm",
    }
    assert _with_deterministic_action_if_needed(decision, _context(latest)) is decision


def test_planner_payload_separates_latest_instruction_from_generated_context() -> None:
    latest = "打开第一个合理的视频结果。"
    payload = _planner_payload(_context(latest))
    assert payload["latest_user_instruction"] == latest
    assert "Branch goal:" in payload["user_message"]


def test_direct_branch_task_is_the_latest_instruction() -> None:
    payload = _planner_payload({"user_message": "Open the first matching result."})
    assert payload["latest_user_instruction"] == "Open the first matching result."


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all browser branch action-selection tests passed")


if __name__ == "__main__":
    _main()
