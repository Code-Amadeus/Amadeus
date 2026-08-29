from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.interaction_branch import InteractionBranchCoordinator  # noqa: E402
from server.work_context import render_active_provider_context  # noqa: E402


async def main() -> None:
    calls: list[dict[str, Any]] = []

    async def provider_run(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params)
        assert params["provider"] == "browser", params
        metadata = params["metadata"]
        assert metadata["source"] == "llm_delegate", metadata
        assert metadata["provider_branch"] is True, metadata
        assert metadata["browser_action"] == "observe", metadata
        assert metadata["browser_session_id"] == "browser_test", metadata
        return {
            "run": {
                "run_id": "browser_branch_run",
                "provider": "browser",
                "task": params["task"],
                "status": "done",
                "result": "Searched the current page for Amadeus.",
                "metadata": {
                    "session_id": "chat_test",
                    "browser": {
                        "browser_session_id": "browser_test",
                        "chat_session_id": "chat_test",
                        "current_url": "https://example.test/search?q=Amadeus",
                        "page_title": "Search Results",
                    },
                    "provider_branch": {
                        "branch_id": "branch_test",
                        "status": "done",
                        "final_report": "Searched the current page for Amadeus.",
                        "compact_digest": "Browser branch searched current page for Amadeus.",
                        "actions": [{"action": "fill_ref", "ref": "br_1"}],
                        "next_state": {
                            "browser_session_id": "browser_test",
                            "current_url": "https://example.test/search?q=Amadeus",
                            "page_title": "Search Results",
                        },
                    },
                },
            }
        }

    coordinator = InteractionBranchCoordinator(provider_run=provider_run, root=ROOT / "runtime" / "test_interaction_branches")
    coordinator._update_from_run(
        {
            "run_id": "browser_open",
            "provider": "browser",
            "task": "Open the test page.",
            "status": "done",
            "result": "Opened test page.",
            "metadata": {
                "session_id": "chat_test",
                "browser": {
                    "browser_session_id": "browser_test",
                    "chat_session_id": "chat_test",
                    "current_url": "https://example.test/",
                    "page_title": "Test Page",
                },
            },
        }
    )
    initial_branch = coordinator._active_by_session["chat_test"]
    assert initial_branch.checkpoint["parent_session_id"] == "chat_test", initial_branch.checkpoint
    assert initial_branch.hidden_summary, initial_branch
    assert "browser_session_id=browser_test" in initial_branch.hidden_summary, initial_branch.hidden_summary

    coordinator._update_from_run(
        {
            "run_id": "browser_interrupted",
            "provider": "browser",
            "task": "Search the current page for Amadeus.",
            "status": "cancelled",
            "result": "",
            "metadata": {
                "session_id": "chat_test",
                "browser": {
                    "browser_session_id": "browser_test",
                    "chat_session_id": "chat_test",
                    "current_url": "https://example.test/",
                    "page_title": "Test Page",
                },
            },
        }
    )
    interrupted_branch = coordinator._active_by_session["chat_test"]
    assert interrupted_branch.status == "idle", interrupted_branch
    assert "Search the current page" in interrupted_branch.pending_goal, interrupted_branch.pending_goal
    assert "interrupted before completion" in interrupted_branch.hidden_summary, interrupted_branch.hidden_summary

    handled_after_interrupt = await coordinator.try_route_user_message(
        text="Open the first result on this page.",
        session_id="chat_test",
        turn_id="turn_after_interrupt",
    )
    assert handled_after_interrupt and handled_after_interrupt["handled"] is True, handled_after_interrupt
    assert "Latest user instruction: Open the first result" in calls[-1]["task"], calls[-1]

    unrelated = await coordinator.try_route_user_message(
        text="Let's talk about quantum mechanics instead.",
        session_id="chat_test",
        turn_id="turn_unrelated",
    )
    assert unrelated is None, unrelated
    noise = await coordinator.try_route_user_message(
        text="Hello.",
        session_id="chat_test",
        turn_id="turn_noise",
    )
    assert noise is None, noise
    chinese_noise = await coordinator.try_route_user_message(
        text="听到。",
        session_id="chat_test",
        turn_id="turn_chinese_noise",
    )
    assert chinese_noise is None, chinese_noise

    handled = await coordinator.try_route_user_message(
        text="Search Amadeus on this page.",
        session_id="chat_test",
        turn_id="turn_search",
    )
    assert handled and handled["handled"] is True, handled
    assert calls and "Latest user instruction: Search Amadeus on this page." in calls[-1]["task"], calls[-1]
    assert handled["display_text"] == "Searched the current page for Amadeus.", handled
    branch = coordinator._active_by_session["chat_test"]
    assert any(item["role"] == "user" and "Search Amadeus" in item["content"] for item in branch.visible_messages), branch.visible_messages
    assert any(item["role"] == "assistant" and "Searched" in item["content"] for item in branch.visible_messages), branch.visible_messages
    assert branch.actions and branch.actions[-1]["action"] == "fill_ref", branch.actions
    active_context = render_active_provider_context(session_id="chat_test")
    assert "conversation branch" in active_context or "Browser Conversation Branch" in active_context, active_context
    assert "browser_test" in active_context, active_context

    coordinator._update_from_run(
        {
            "run_id": "browser_wait",
            "provider": "browser",
            "task": "Search the current page, but the keyword is missing.",
            "status": "done",
            "result": "Need a search keyword.",
            "metadata": {
                "session_id": "chat_test",
                "browser": {
                    "browser_session_id": "browser_test",
                    "chat_session_id": "chat_test",
                    "current_url": "https://example.test/",
                    "page_title": "Test Page",
                },
                "provider_branch": {
                    "branch_id": "branch_wait",
                    "status": "done",
                    "final_report": "Need a search keyword.",
                    "compact_digest": "Browser branch needs a search keyword.",
                    "actions": [],
                    "next_state": {
                        "browser_session_id": "browser_test",
                        "current_url": "https://example.test/",
                        "page_title": "Test Page",
                    },
                },
            },
        }
    )
    noise_while_waiting = await coordinator.try_route_user_message(
        text="听到。",
        session_id="chat_test",
        turn_id="turn_noise_waiting",
    )
    assert noise_while_waiting is None, noise_while_waiting
    chinese_noise_while_waiting = await coordinator.try_route_user_message(
        text="好的。",
        session_id="chat_test",
        turn_id="turn_chinese_noise_waiting",
    )
    assert chinese_noise_while_waiting is None, chinese_noise_while_waiting
    handled_short = await coordinator.try_route_user_message(
        text="Amadeus.",
        session_id="chat_test",
        turn_id="turn_keyword",
    )
    assert handled_short and handled_short["handled"] is True, handled_short
    assert "Pending branch goal: Search the current page" in calls[-1]["task"], calls[-1]["task"]
    assert "Latest user instruction: Amadeus." in calls[-1]["task"], calls[-1]["task"]
    final_branch = coordinator._active_by_session["chat_test"]
    assert final_branch.merge_count >= 4, final_branch.merge_count
    assert final_branch.hidden_messages, final_branch.hidden_messages

    print("interaction branch lifecycle smoke ok")


if __name__ == "__main__":
    asyncio.run(main())
