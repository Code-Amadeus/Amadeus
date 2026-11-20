from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.session_manager import ConversationHistory  # noqa: E402
from server.event_bus import bus  # noqa: E402
from server.handlers.work_activity_handler import WorkActivityCoordinator  # noqa: E402
from server.protocol import Method  # noqa: E402
from server.work_context import (  # noqa: E402
    augment_system_prompt_with_active_provider_context,
    clear_work_run,
    recent_work_notes,
    render_active_provider_context,
)


async def main() -> None:
    session_id = "smoke_browser_followup_session"
    run_id = "smoke_browser_followup_run"
    browser_session_id = "browser_smoke_123"
    raw_page_sentinel = "RAW_PAGE_TEXT_SHOULD_NOT_ENTER_MAIN_PROMPT"

    clear_work_run(run_id)

    coordinator = WorkActivityCoordinator()
    coordinator.configure()

    await bus.emit(
        Method.PROVIDER_EVENT,
        {
            "provider": "browser",
            "run_id": run_id,
            "task": "Open the docs page.",
            "type": "artifact.created",
            "payload": {
                "artifact_type": "browser.snapshot",
                "browser_session_id": browser_session_id,
                "url": "https://example.com/docs",
                "title": "Example Docs",
                "excerpt": "The browser is showing the documentation page with several navigable sections.",
                "links": [{"title": "Second section", "url": "https://example.com/docs#second"}],
                "screenshot": "data:image/png;base64,abc",
                "engine": "playwright",
                "status_code": 200,
                "raw_page_text": raw_page_sentinel,
            },
            "metadata": {"session_id": session_id},
        },
    )

    notes = recent_work_notes(session_id=session_id, limit=4)
    browser_notes = [item for item in notes if item.get("provider") == "browser"]
    assert browser_notes, notes
    latest = browser_notes[-1]
    metadata = latest.get("metadata") if isinstance(latest.get("metadata"), dict) else {}
    assert metadata.get("continuable") is True, latest
    assert metadata.get("browser_session_id") == browser_session_id, latest
    assert metadata.get("url") == "https://example.com/docs", latest
    assert metadata.get("page_title") == "Example Docs", latest

    context = render_active_provider_context(session_id=session_id)
    assert "Transient active provider context" in context, context
    assert browser_session_id in context, context
    assert "Example Docs" in context, context
    assert "https://example.com/docs" in context, context
    assert raw_page_sentinel not in context, context

    system_prompt = augment_system_prompt_with_active_provider_context(
        "You are Kurisu.",
        session_id=session_id,
    )
    assert "[Runtime side-channel]" in system_prompt, system_prompt
    assert "prefer the browser provider/session" in system_prompt, system_prompt
    assert browser_session_id in system_prompt, system_prompt
    assert raw_page_sentinel not in system_prompt, system_prompt

    history = ConversationHistory(max_rounds=4)
    history.add_user("打开这个网页。")
    history.add_assistant("我打开了页面，画面已经放在 CRT canvas 上。")
    messages = history.build_deepseek_messages(system_prompt, "继续点第二个链接看看")

    assert messages[0]["role"] == "system", messages
    assert browser_session_id in messages[0]["content"], messages[0]
    assert messages[-1] == {"role": "user", "content": "继续点第二个链接看看"}, messages[-1]
    assert all(raw_page_sentinel not in item.get("content", "") for item in messages), messages

    clear_work_run(run_id)
    print("active provider context prompt smoke ok")


if __name__ == "__main__":
    asyncio.run(main())
