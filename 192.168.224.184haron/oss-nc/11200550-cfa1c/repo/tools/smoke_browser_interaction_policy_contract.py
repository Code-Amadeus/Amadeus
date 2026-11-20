from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.browser_interaction_policy import BrowserInteractionPolicy  # noqa: E402
from agent_host.provider_types import ProviderRunRequest  # noqa: E402


def request(action: str, *, task: str, source: str = "llm_delegate", **metadata):
    return ProviderRunRequest(
        provider="browser",
        task=task,
        mode=action,
        metadata={"source": source, "browser_action": action, "browser_mode": action, **metadata},
    )


def main() -> None:
    policy = BrowserInteractionPolicy()

    simple_open = policy.decide(
        request(
            "open",
            task="Open https://example.com and show it on the canvas.",
            url="https://example.com",
        )
    )
    assert not simple_open.use_branch, simple_open
    assert simple_open.entry_kind == "direct", simple_open

    explicit_no_action_open = policy.decide(
        request(
            "open",
            task="Open the page, no further page action is needed.",
            url="https://example.com",
            max_branch_actions=0,
        )
    )
    assert not explicit_no_action_open.use_branch, explicit_no_action_open
    assert explicit_no_action_open.reason == "no_branch_actions_requested", explicit_no_action_open

    click_text = policy.decide(
        request(
            "click_text",
            task="Click the Search button on the current page.",
            browser_session_id="browser_123",
            text="Search",
        )
    )
    assert click_text.use_branch, click_text
    assert click_text.entry_kind == "dom_branch_observe_first", click_text
    assert click_text.initial_request.mode == "observe", click_text
    assert click_text.initial_request.metadata["branch_original_action"] == "click_text", click_text

    fill_ref = policy.decide(
        request(
            "fill_ref",
            task="Fill the current search box.",
            browser_session_id="browser_123",
            ref="br_1",
            value="Amadeus",
        )
    )
    assert fill_ref.use_branch, fill_ref
    assert fill_ref.entry_kind == "dom_branch_observe_first", fill_ref

    followup_observe = policy.decide(
        request(
            "observe",
            task="Continue on the current page and decide the next action.",
            browser_session_id="browser_123",
        )
    )
    assert followup_observe.use_branch, followup_observe
    assert followup_observe.entry_kind == "dom_branch", followup_observe

    open_then_interact = policy.decide(
        request(
            "open",
            task="Open this page, then click the FGO video card.",
            url="https://example.com",
        )
    )
    assert open_then_interact.use_branch, open_then_interact
    assert open_then_interact.entry_kind == "dom_branch", open_then_interact

    chinese_open_then_interact = policy.decide(
        request(
            "open",
            task="打开这个页面，然后在搜索框里输入 Amadeus。",
            url="https://example.com",
        )
    )
    assert chinese_open_then_interact.use_branch, chinese_open_then_interact
    assert chinese_open_then_interact.entry_kind == "dom_branch", chinese_open_then_interact

    canvas_click = policy.decide(
        request(
            "click_text",
            task="Canvas chip asked to click this exact label.",
            source="canvas_action",
            browser_session_id="browser_123",
            text="Search",
        )
    )
    assert not canvas_click.use_branch, canvas_click
    assert canvas_click.reason == "non_llm_delegate", canvas_click

    print("browser interaction policy contract smoke ok")


if __name__ == "__main__":
    main()
