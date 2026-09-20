from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.provider_runtime import runtime  # noqa: E402
from server.browser_branch_planner import has_browser_branch_llm_config  # noqa: E402
from server.event_bus import bus  # noqa: E402
from server.handlers.provider_handler import ProviderHandler  # noqa: E402
from server.handlers.work_activity_handler import WorkActivityCoordinator  # noqa: E402
from server.interaction_branch import InteractionBranchCoordinator  # noqa: E402
from server.protocol import Method  # noqa: E402
from server.work_context import recent_work_notes, render_active_provider_context  # noqa: E402


SESSION_ID = "browser_real_scene_session"
BILIBILI_URL = "https://www.bilibili.com/"


async def run_provider_and_wait(handler: ProviderHandler, params: dict[str, Any]) -> dict[str, Any]:
    response = await handler.run_provider(params)
    run = response["run"]
    record = runtime.get_run(str(run["run_id"]))
    if record is not None and record.task_handle is not None:
        await record.task_handle
        run = record.to_dict()
    return run


async def continue_branch_and_wait(
    coordinator: InteractionBranchCoordinator,
    *,
    text: str,
    turn_id: str,
) -> tuple[dict[str, Any], str]:
    started = await coordinator.continue_from_delegate(
        session_id=SESSION_ID,
        task=text,
        turn_id=turn_id,
    )
    assert started is not None, "active Browser branch did not accept branch=continue"
    record = runtime.get_run(str(started.get("run_id") or ""))
    assert record is not None, started
    if record.task_handle is not None:
        await record.task_handle
    run = record.to_dict()
    # ProviderResult normally updates the subscribed coordinator. Keep this
    # explicit call idempotent so the smoke also proves the final persisted
    # branch state before it inspects narration.
    branch = coordinator._update_from_run(run)
    assert branch is not None, run
    return run, branch.visible_summary


def compact_run(run: dict[str, Any]) -> dict[str, Any]:
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    browser = metadata.get("browser") if isinstance(metadata.get("browser"), dict) else {}
    branch = metadata.get("provider_branch") if isinstance(metadata.get("provider_branch"), dict) else {}
    return {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "error": run.get("error"),
        "title": browser.get("page_title") or browser.get("title"),
        "url": browser.get("current_url") or browser.get("url"),
        "browser_session_id": browser.get("browser_session_id"),
        "branch_status": branch.get("status"),
        "branch_actions": branch.get("actions") or [],
        "branch_report": branch.get("final_report"),
        "result_excerpt": str(run.get("result") or "")[:500],
    }


def assert_truthful_branch_narration(
    coordinator: InteractionBranchCoordinator,
    run: dict[str, Any],
    display_text: str,
) -> None:
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    provider_branch = metadata.get("provider_branch") if isinstance(metadata.get("provider_branch"), dict) else {}
    branch = coordinator.active_branch_for_session(SESSION_ID)
    assert branch is not None, run
    decision = (
        branch.metadata.get("outcome_verdict")
        if isinstance(branch.metadata.get("outcome_verdict"), dict)
        else {}
    )
    observed = (
        decision.get("observed")
        if isinstance(decision.get("observed"), dict)
        else {}
    )
    report = str(provider_branch.get("final_report") or run.get("result") or "").strip()
    assert observed.get("url") == branch.url, decision
    assert observed.get("title") == branch.title, decision
    if decision.get("provider_report_allowed") is True:
        assert display_text == report, (display_text, report, decision)
    else:
        assert not report or report not in display_text, (display_text, report, decision)
        assert branch.title in display_text, (display_text, branch.title, decision)


async def main() -> None:
    if not has_browser_branch_llm_config():
        print("browser real scene set skipped: no browser branch LLM config")
        return

    captured: dict[str, list[dict[str, Any]]] = {
        "canvas": [],
        "activity": [],
        "provider_result": [],
        "work_note": [],
    }

    async def capture(method: str, params: dict[str, Any]) -> None:
        key_by_method = {
            Method.WALLPAPER_CANVAS: "canvas",
            Method.WALLPAPER_ACTIVITY: "activity",
            Method.PROVIDER_RESULT: "provider_result",
            Method.CHAT_WORK_NOTE: "work_note",
        }
        method_key = Method(method) if not isinstance(method, Method) else method
        key = key_by_method.get(method_key)
        if key:
            captured[key].append(dict(params or {}))

    for method in (
        Method.WALLPAPER_CANVAS,
        Method.WALLPAPER_ACTIVITY,
        Method.PROVIDER_RESULT,
        Method.CHAT_WORK_NOTE,
    ):
        bus.on(method, capture)

    WorkActivityCoordinator().configure()
    provider = ProviderHandler()
    branch_coordinator = InteractionBranchCoordinator(
        provider_run=provider.run_provider,
        display_language=lambda: "japanese",
    )
    branch_coordinator.configure()

    print("[real] opening Bilibili homepage")
    opened = await run_provider_and_wait(
        provider,
        {
            "provider": "browser",
            "task": f"Open Bilibili homepage: {BILIBILI_URL}",
            "mode": "open",
            "requirements": {
                "task_kind": "browser",
                "preferred_provider": "browser",
                "preference_policy": "require",
            },
            "metadata": {
                "source": "llm_delegate",
                "session_id": SESSION_ID,
                "browser_action": "open",
                "browser_mode": "open",
                "url": BILIBILI_URL,
                "max_branch_actions": 0,
                "timeout_ms": 30000,
            },
        },
    )
    print(json.dumps({"opened": compact_run(opened)}, ensure_ascii=False, indent=2))
    assert opened["status"] == "done", opened
    browser = opened["metadata"]["browser"]
    browser_session_id = browser["browser_session_id"]
    assert browser_session_id, opened
    assert captured["canvas"], "opening a real browser page should update the canvas"
    assert captured["canvas"][-1].get("browserSessionId") == browser_session_id, captured["canvas"][-1]

    active_context = render_active_provider_context(session_id=SESSION_ID)
    assert browser_session_id in active_context, active_context
    assert "raw" not in active_context.lower(), active_context

    print("[real] routing follow-up through interaction branch: search Amadeus on current page")
    search_run, search_display = await continue_branch_and_wait(
        branch_coordinator,
        text="请在当前哔哩哔哩页面搜索 Amadeus。",
        turn_id="real_search_amadeus",
    )
    print(json.dumps({"searched": compact_run(search_run), "display_text": search_display}, ensure_ascii=False, indent=2))
    assert_truthful_branch_narration(branch_coordinator, search_run, search_display)
    assert search_run.get("status") == "done", search_run
    search_branch = search_run.get("metadata", {}).get("provider_branch", {})
    search_actions = search_branch.get("actions") or []
    assert search_actions, search_branch
    assert any(item.get("action") in {"fill_ref", "click_ref", "observe", "open"} for item in search_actions), search_actions
    assert search_run["metadata"]["browser"]["browser_session_id"] == browser_session_id, search_run
    assert captured["canvas"][-1].get("browserSessionId") == browser_session_id, captured["canvas"][-1]

    print("[real] routing another page-continuation turn: observe or open a likely first result")
    followup_run, followup_display = await continue_branch_and_wait(
        branch_coordinator,
        text="观察搜索结果，如果有明确的视频结果就打开第一个合理结果；如果没有就只汇报当前页面状态。",
        turn_id="real_open_first_result",
    )
    print(json.dumps({"followup": compact_run(followup_run), "display_text": followup_display}, ensure_ascii=False, indent=2))
    assert_truthful_branch_narration(branch_coordinator, followup_run, followup_display)
    assert followup_run.get("status") == "done", followup_run
    assert followup_run["metadata"]["browser"]["browser_session_id"] == browser_session_id, followup_run
    assert captured["canvas"][-1].get("browserSessionId") == browser_session_id, captured["canvas"][-1]
    followup_actions = followup_run.get("metadata", {}).get("provider_branch", {}).get("actions") or []
    assert [item.get("action") for item in followup_actions] == ["click_ref"], followup_run

    print("[real] returning to the search results through structured browser back")
    back_run, back_display = await continue_branch_and_wait(
        branch_coordinator,
        text="返回上一页的 Amadeus 搜索结果，不要重新搜索。",
        turn_id="real_back_to_results",
    )
    print(json.dumps({"back": compact_run(back_run), "display_text": back_display}, ensure_ascii=False, indent=2))
    assert_truthful_branch_narration(branch_coordinator, back_run, back_display)
    assert back_run.get("status") == "done", back_run
    assert back_run["metadata"]["browser"]["browser_session_id"] == browser_session_id, back_run
    back_actions = back_run.get("metadata", {}).get("provider_branch", {}).get("actions") or []
    assert [item.get("action") for item in back_actions] == ["back"], back_run
    assert "search.bilibili.com" in str(back_run["metadata"]["browser"].get("current_url") or ""), back_run
    assert any("\u3040" <= char <= "\u30ff" for char in back_display), back_display

    print("[real] verifying new-topic turn is not eaten by browser branch")
    unrelated = await branch_coordinator.try_route_user_message(
        text="换个话题，我们聊一下量子退相干。",
        session_id=SESSION_ID,
        turn_id="real_new_topic",
    )
    assert unrelated is None, unrelated

    branch_files = sorted((ROOT / "runtime" / "provider_branches").glob("browser_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    latest_branch = branch_files[0] if branch_files else None
    if latest_branch:
        payload = latest_branch.read_text(encoding="utf-8")
        assert "DOCTYPE" in payload.upper() or "<html" in payload.lower(), latest_branch
        main_context = render_active_provider_context(session_id=SESSION_ID)
        assert "<html" not in main_context.lower(), main_context[:1000]
        print("[real] latest provider branch store:", latest_branch)

    assert any(item.get("activity") == "work" for item in captured["activity"]), captured["activity"]
    assert captured["activity"][-1].get("activity") == "", captured["activity"][-1]
    assert len(captured["provider_result"]) >= 4, captured["provider_result"]
    notes = recent_work_notes(session_id=SESSION_ID, limit=12)
    assert any(str(item.get("source") or "") == "interaction_branch" for item in notes), notes

    adapter = runtime.get_adapter("browser")
    shutdown = getattr(adapter, "shutdown", None)
    if callable(shutdown):
        await shutdown()

    print("browser real scene set smoke ok")
    print("canvas updates:", len(captured["canvas"]))
    print("provider results:", len(captured["provider_result"]))
    print("work notes:", len(captured["work_note"]))


if __name__ == "__main__":
    asyncio.run(main())
