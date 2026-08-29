"""Reference selection gates the real focus handler before side effects."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.work_ledger_store import WorkLedgerStore
from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST
from server import app as server_app
from server.attention_request import attention_requests
from server.control_decision import CONTROL_REFERENCE_CANDIDATES_ATTR
from server.reference_catalog import TypedReferenceCandidate
from server.work_ledger_coordinator import WorkLedgerCoordinator


async def _ambiguous_focus_waits_for_slice_selection() -> None:
    attention_requests.reset_for_tests()
    with tempfile.TemporaryDirectory(prefix="reference_clarification_assembly_") as temp:
        root = Path(temp)
        chess_path = root / "chess"
        chess_path.mkdir()
        store = WorkLedgerStore(root / "ledger.sqlite3")
        project = store.create_or_get_project(chess_path, name="象棋")
        item = store.create_work_item(
            project.project_id,
            title="象棋双人模式",
            workspace_path=chess_path,
        )
        store.create_attempt(
            item.work_item_id,
            provider="codex",
            task="实现双人模式",
            metadata={"session_id": "session-reference"},
        )
        coordinator = WorkLedgerCoordinator(store)
        coordinator.configure()
        query = AsyncMock(
            return_value=(
                '{"references":['
                f'"work_item:{item.work_item_id}",'
                f'"project:{project.project_id}"'
                "]}"
            )
        )
        speak = AsyncMock(return_value=True)
        try:
            with (
                patch("core.session_manager.get_current_session_id", return_value="session-reference"),
                patch("config.settings.DELEGATE_INTENT_ATTRIBUTE", True),
                patch("config.settings.DELEGATE_FOCUS_INTENT", True),
                patch("config.settings.REFERENCE_CLARIFICATION_ENABLED", True),
                patch(
                    "server.work_ledger_coordinator.cwd_in_project_registry",
                    return_value=True,
                ),
                patch(
                    "server.reference_clarification.default_message_query",
                    new=query,
                ),
                patch.object(server_app, "_speak_task_lookup_answer", new=speak),
            ):
                result = await server_app._handle_delegate(
                    "",
                    {
                        "provider": "codex",
                        "intent": "focus",
                        "project_id": project.project_id,
                        "_host_source_user_text": "切回刚才那个象棋",
                    },
                )
                assert result == "[focus] awaiting reference selection"
                assert coordinator.session_project("session-reference") == ""
                assert store.list_work_items()[0].work_item_id == item.work_item_id
                pending = attention_requests.list_pending("session-reference")
                assert len(pending) == 1
                project_option = next(
                    option
                    for option in pending[0]["options"]
                    if option["entityKind"] == "project"
                )

                resolved = await attention_requests.resolve(
                    session_id="session-reference",
                    request_id=pending[0]["id"],
                    option_id=project_option["id"],
                )
                assert resolved["ok"] is True
                assert coordinator.session_project("session-reference") == project.project_id
                assert len(store.list_work_items()) == 1
                duplicate = await attention_requests.resolve(
                    session_id="session-reference",
                    request_id=pending[0]["id"],
                    option_id=project_option["id"],
                )
                assert duplicate["ok"] is False
                assert coordinator.session_project("session-reference") == project.project_id
        finally:
            attention_requests.reset_for_tests()
            coordinator.close()


async def _ambiguous_amend_uses_the_same_selection_primitive() -> None:
    attention_requests.reset_for_tests()
    with tempfile.TemporaryDirectory(prefix="amend_selection_assembly_") as temp:
        root = Path(temp)
        project_path = root / "notes"
        project_path.mkdir()
        store = WorkLedgerStore(root / "ledger.sqlite3")
        project = store.create_or_get_project(project_path, name="Notes")
        first = store.create_work_item(
            project.project_id, title="Personal notes", workspace_path=project_path
        )
        second = store.create_work_item(
            project.project_id, title="Research notes", workspace_path=project_path
        )
        coordinator = WorkLedgerCoordinator(store)
        coordinator.configure()
        resume = AsyncMock(return_value={"status": "captured"})
        speak = AsyncMock(return_value=True)
        try:
            with (
                patch("core.session_manager.get_current_session_id", return_value="session-amend"),
                patch("config.settings.REFERENCE_CLARIFICATION_ENABLED", True),
                patch(
                    "server.work_ledger_coordinator.cwd_in_project_registry",
                    return_value=True,
                ),
                patch.object(server_app, "_resume_reference_selection", new=resume),
                patch.object(server_app, "_speak_task_lookup_answer", new=speak),
            ):
                result = await server_app._handle_delegate(
                    "给 notes.txt 加一行",
                    {
                        "provider": "codex",
                        "intent": "amend",
                        "amend_ambiguous": "Personal notes, Research notes",
                        "_host_amend_candidates": [
                            {
                                "work_item_id": first.work_item_id,
                                "project_id": project.project_id,
                                "title": first.title,
                            },
                            {
                                "work_item_id": second.work_item_id,
                                "project_id": project.project_id,
                                "title": second.title,
                            },
                        ],
                        "_host_source_user_text": "给 notes.txt 加一行",
                    },
                )
                assert result == "[amend blocked] awaiting WorkItem selection"
                assert len(store.list_work_items()) == 2
                request = attention_requests.list_pending("session-amend")[0]
                chosen = next(
                    option
                    for option in request["options"]
                    if option["label"] == "Research notes"
                )
                resolved = await attention_requests.resolve(
                    session_id="session-amend",
                    request_id=request["id"],
                    option_id=chosen["id"],
                )
                assert resolved["ok"] is True
                resume.assert_awaited_once()
                plan = resume.await_args.args[0]
                assert plan.kind == "delegate"
                assert plan.session_id == "session-amend"
                assert plan.attrs["intent"] == "amend"
                assert plan.attrs["workspace_ref"] == second.work_item_id
                assert "amend_ambiguous" not in plan.attrs
                assert "_host_amend_candidates" not in plan.attrs
                assert len(store.list_work_items()) == 2
        finally:
            attention_requests.reset_for_tests()
            coordinator.close()


async def _control_decision_typed_set_reuses_attention_without_second_query() -> None:
    attention_requests.reset_for_tests()
    with tempfile.TemporaryDirectory(prefix="control_reference_assembly_") as temp:
        root = Path(temp)
        chess_path = root / "chess"
        chess_path.mkdir()
        store = WorkLedgerStore(root / "ledger.sqlite3")
        project = store.create_or_get_project(chess_path, name="象棋")
        item = store.create_work_item(
            project.project_id,
            title="象棋双人模式",
            workspace_path=chess_path,
        )
        store.create_attempt(
            item.work_item_id,
            provider="codex",
            task="实现双人模式",
            metadata={"session_id": "session-control-reference"},
        )
        coordinator = WorkLedgerCoordinator(store)
        coordinator.configure()
        project_candidate = TypedReferenceCandidate(
            "project", project.project_id, project.name, "persistent"
        )
        item_candidate = TypedReferenceCandidate(
            "work_item",
            item.work_item_id,
            item.title,
            "project",
            parent_project_id=project.project_id,
            parent_project_label=project.name,
        )
        speak = AsyncMock(return_value=True)
        query = AsyncMock(side_effect=AssertionError("typed decision must not re-query"))
        try:
            with (
                patch(
                    "core.session_manager.get_current_session_id",
                    return_value="session-control-reference",
                ),
                patch("config.settings.REFERENCE_CLARIFICATION_ENABLED", True),
                patch(
                    "server.work_ledger_coordinator.cwd_in_project_registry",
                    return_value=True,
                ),
                patch(
                    "server.reference_clarification.default_message_query",
                    new=query,
                ),
                patch.object(server_app, "_speak_task_lookup_answer", new=speak),
            ):
                result = await server_app._handle_delegate(
                    "",
                    {
                        "provider": "codex",
                        "intent": "focus",
                        CONTROL_REFERENCE_CANDIDATES_ATTR: (
                            item_candidate,
                            project_candidate,
                        ),
                        "_host_source_user_text": "切回刚才那个象棋",
                    },
                )
                assert result == "[focus] awaiting reference selection"
                query.assert_not_awaited()
                assert coordinator.session_project("session-control-reference") == ""
                request = attention_requests.list_pending(
                    "session-control-reference"
                )[0]
                work_option = next(
                    option
                    for option in request["options"]
                    if option["entityKind"] == "work_item"
                )
                resolved = await attention_requests.resolve(
                    session_id="session-control-reference",
                    request_id=request["id"],
                    option_id=work_option["id"],
                )
                assert resolved["ok"] is True
                assert (
                    coordinator.session_project("session-control-reference")
                    == project.project_id
                )
                context = store.get_session_work_context("session-control-reference")
                assert context is not None
                assert context.active_work_item_id == item.work_item_id

                status, task_text, attrs = (
                    await server_app._adjudicate_delegate_reference(
                        "查询这次交付",
                        {
                            "provider": "codex",
                            "intent": "report",
                            "subject": "project",
                            CONTROL_REFERENCE_CANDIDATES_ATTR: (item_candidate,),
                        },
                    )
                )
                assert status == "resolved"
                assert task_text == "查询这次交付"
                assert attrs["intent"] == "report"
                assert attrs["subject"] == "work_item"
                assert attrs["workspace_ref"] == item.work_item_id

                status, _task_text, _attrs = (
                    await server_app._adjudicate_delegate_reference(
                        "",
                        {
                            "provider": "codex",
                            "intent": "focus",
                            CONTROL_REFERENCE_CANDIDATES_ATTR: None,
                        },
                    )
                )
                assert status == "blocked"
                assert (
                    coordinator.session_project("session-control-reference")
                    == project.project_id
                )

                status, _task_text, _attrs = (
                    await server_app._adjudicate_delegate_reference(
                        "",
                        {
                            "provider": "codex",
                            "intent": "focus",
                            "focus": "clear",
                            CONTROL_REFERENCE_CANDIDATES_ATTR: None,
                        },
                    )
                )
                assert status == "bypass"
                query.assert_not_awaited()

                status, task_text, attrs = (
                    await server_app._adjudicate_delegate_reference(
                        "在象棋项目里完成这件事，然后回到草稿",
                        {
                            "provider": "codex",
                            "intent": "execute",
                            "focus": "clear",
                            CONTROL_REFERENCE_CANDIDATES_ATTR: (project_candidate,),
                        },
                    )
                )
                assert status == "resolved"
                assert task_text == "在象棋项目里完成这件事，然后回到草稿"
                assert attrs["project_id"] == project.project_id
                assert attrs["focus"] == "clear"
                query.assert_not_awaited()
        finally:
            attention_requests.reset_for_tests()
            coordinator.close()


def test_ambiguous_focus_waits_for_slice_selection() -> None:
    asyncio.run(_ambiguous_focus_waits_for_slice_selection())
    print("ok: ambiguous focus resumes exactly once from the Slice choice")


def test_fresh_desktop_delivery_bypasses_reference_lookup_and_routes_to_draft() -> None:
    async def run() -> None:
        attention_requests.reset_for_tests()
        with tempfile.TemporaryDirectory(prefix="desktop_draft_assembly_") as temp:
            root = Path(temp)
            store = WorkLedgerStore(root / "ledger.sqlite3")
            coordinator = WorkLedgerCoordinator(store)
            coordinator.configure()
            speak = AsyncMock(return_value=True)
            try:
                with (
                    patch(
                        "core.session_manager.get_current_session_id",
                        return_value="session-desktop-draft",
                    ),
                    patch("config.settings.REFERENCE_CLARIFICATION_ENABLED", True),
                    patch("config.settings.WORK_SCRATCH_ROOT", str(root / "drafts")),
                    patch.object(server_app, "_speak_task_lookup_answer", new=speak),
                ):
                    status, task_text, attrs = (
                        await server_app._adjudicate_delegate_reference(
                            "五子棋ゲームをHTMLとして新規作成する",
                            {
                                "provider": "codex",
                                "intent": "execute",
                                "target": "desktop",
                                "_host_source_user_text": (
                                    "你可以在桌面帮我写写一个五子棋的游戏吗？"
                                ),
                                CONTROL_REFERENCE_CANDIDATES_ATTR: None,
                            },
                        )
                    )
                    assert status == "bypass"
                    assert task_text == "五子棋ゲームをHTMLとして新規作成する"
                    route = server_app._delegate_workspace_route(
                        "codex",
                        attrs,
                        manifest=CODEX_APP_SERVER_MANIFEST,
                    )
                    assert route["status"] == "resolved"
                    assert route["source"] == "scratch_default"
                    assert Path(route["cwd"]) == (root / "drafts").resolve()
                    assert coordinator.session_project("session-desktop-draft") == ""
                    speak.assert_not_awaited()
            finally:
                attention_requests.reset_for_tests()
                coordinator.close()

    asyncio.run(run())
    print("ok: fresh Desktop delivery uses a Session Draft before export")


def test_ambiguous_amend_uses_the_same_selection_primitive() -> None:
    asyncio.run(_ambiguous_amend_uses_the_same_selection_primitive())
    print("ok: ambiguous amend reuses the generic WorkItem selection card")


def test_control_decision_typed_set_reuses_attention_without_second_query() -> None:
    asyncio.run(_control_decision_typed_set_reuses_attention_without_second_query())
    print("ok: ControlDecision typed ambiguity reuses Attention without re-query")


if __name__ == "__main__":
    test_ambiguous_focus_waits_for_slice_selection()
    test_fresh_desktop_delivery_bypasses_reference_lookup_and_routes_to_draft()
    test_ambiguous_amend_uses_the_same_selection_primitive()
    test_control_decision_typed_set_reuses_attention_without_second_query()
