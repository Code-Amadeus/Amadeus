from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.session_manager import ConversationHistory  # noqa: E402
from server.provider_branch import (  # noqa: E402
    ProviderBranchStore,
    apply_branch_merge_to_history,
)


def main() -> None:
    raw_dom = """
    <html>
      <body>
        <input id="q" name="q" value="">
        <a class="video-card" href="/video">FGO mainline video</a>
        <script>window.privateTrace = "RAW_DOM_SHOULD_STAY_HIDDEN";</script>
      </body>
    </html>
    """
    raw_tool_trace = "Locator trial: input#q -> fill('FGO'); a.video-card -> click_ref br_2"

    with tempfile.TemporaryDirectory() as tmp:
        store = ProviderBranchStore(Path(tmp) / "branches")
        branch = store.create_branch(
            parent_session_id="main_session_1",
            provider="browser",
            goal="Continue visible browser interaction from the current canvas snapshot.",
            branch_id="browser_branch_contract",
        )

        # Main-visible branch interaction: this must be preserved in chatbox.
        branch.add_message(
            role="user",
            content="点开当前页面里那个 FGO 视频。",
            visibility="visible",
            source="main_chat_intervention",
        )
        branch.add_message(
            role="assistant",
            content="我会在当前页面里找对应的视频卡片，不重新发起搜索。",
            visibility="visible",
            source="browser_branch_llm",
        )

        # High-detail provider context: this must be persisted, but not merged
        # into main chat messages.
        branch.add_message(
            role="system",
            content=raw_dom,
            visibility="hidden",
            source="browser_dom_snapshot",
            metadata={"content_type": "text/html"},
        )
        branch.add_message(
            role="tool",
            content=raw_tool_trace,
            visibility="hidden",
            source="playwright_trace",
        )
        branch.add_action(
            {
                "kind": "browser",
                "action": "click_ref",
                "ref": "br_2",
                "label": "FGO mainline video",
                "result": "opened",
            }
        )
        branch.add_artifact(
            {
                "kind": "browser.snapshot",
                "title": "Video Page",
                "uri": "https://example.test/video",
            }
        )
        branch.add_risk(
            {
                "level": "low",
                "note": "The page may contain dynamic cards; stale refs require a fresh observe.",
            }
        )

        merge = branch.close(
            final_report="我已经在当前浏览器分支里点开了 FGO 视频卡片，canvas 上现在是视频页预览。",
            compact_digest="Browser branch used DOM/action refs to open the requested FGO video without starting a new search.",
            next_state={
                "browser_session_id": "browser_123",
                "current_url": "https://example.test/video",
                "requires_observe_for_next_action": True,
            },
        )

        assert merge["branch_id"] == "browser_branch_contract"
        assert merge["parent_session_id"] == "main_session_1"
        assert merge["provider"] == "browser"
        assert merge["hidden_message_count"] == 2, merge
        assert len(merge["visible_messages"]) == 2, merge
        assert merge["artifacts"][0]["kind"] == "browser.snapshot"
        assert merge["actions"][0]["action"] == "click_ref"
        assert merge["next_state"]["browser_session_id"] == "browser_123"

        history = ConversationHistory(max_rounds=12)
        history.add_user("打开 bilibili。")
        history.add_assistant("页面已经显示在 CRT canvas。")
        apply_branch_merge_to_history(merge, history)

        transcript = "\n".join(item["content"] for item in history.dialog)
        assert "点开当前页面里那个 FGO 视频" in transcript
        assert "我会在当前页面里找对应的视频卡片" in transcript
        assert "我已经在当前浏览器分支里点开了 FGO 视频卡片" in transcript
        assert "RAW_DOM_SHOULD_STAY_HIDDEN" not in transcript
        assert "Locator trial" not in transcript

        branch_file = Path(merge["branch_store_path"])
        assert branch_file.exists(), branch_file
        persisted = json.loads(branch_file.read_text(encoding="utf-8"))
        persisted_text = json.dumps(persisted, ensure_ascii=False)
        assert "RAW_DOM_SHOULD_STAY_HIDDEN" in persisted_text
        assert "Locator trial" in persisted_text
        assert persisted["messages"][0]["visibility"] == "visible"
        assert any(item["visibility"] == "hidden" for item in persisted["messages"])

    print("provider branch merge contract smoke ok")


if __name__ == "__main__":
    main()
