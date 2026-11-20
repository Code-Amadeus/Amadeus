"""Canonical reference selection resumes shared focus and amendment owners."""

import asyncio
from unittest.mock import AsyncMock, patch

from agent_host.work_ledger_store import WorkLedgerStore
from server.app import _handle_declared_focus
from server.attention_request import AttentionRequestCoordinator
from server.reference_catalog import candidate_catalog_from_coordinator
from server.reference_clarification import adjudicate_focus_reference, create_reference_selection
from server.work_ledger_coordinator import WorkLedgerCoordinator


def test_ambiguous_focus_waits_for_selection_before_shared_owner_mutation(tmp_path):
    async def run():
        project_path = tmp_path / "chess"
        project_path.mkdir()
        with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            coordinator.configure()
            project = store.create_or_get_project(project_path, name="Chess")
            item = store.create_work_item(project.project_id, title="Chess two-player", workspace_path=project_path)
            store.create_attempt(item.work_item_id, provider="codex", task="Implement two-player",
                metadata={"session_id": "reference-origin"})
            attention = AttentionRequestCoordinator()
            resumes = []
            async def resume(plan):
                resumes.append(plan)
                return await _handle_declared_focus(dict(plan.attrs), session_id=plan.session_id, announce_result=False)
            query = AsyncMock(return_value=f'{{"references":["work_item:{item.work_item_id}","project:{project.project_id}"]}}')
            try:
                with (
                    patch("core.session_manager.get_current_session_id", return_value="reference-origin"),
                    patch("server.work_ledger_coordinator.cwd_in_project_registry", return_value=True),
                ):
                    result = await adjudicate_focus_reference(coordinator=coordinator, session_id="reference-origin",
                        utterance="Switch back to that Chess project", task_text="",
                        attrs={"intent": "focus", "project_id": project.project_id},
                        query=query, resume=resume, attention=attention)
                    assert result.status == "deferred"
                    assert coordinator.session_project("reference-origin") == ""
                    request = attention.list_pending("reference-origin")[0]
                    choice = next(option for option in request["options"] if option["entityKind"] == "project")
                    selected = await attention.resolve(session_id="reference-origin", request_id=request["id"], option_id=choice["id"])
                    assert selected["ok"] is True
                    assert coordinator.session_project("reference-origin") == project.project_id
                    assert len(resumes) == 1
                    repeated = await attention.resolve(session_id="reference-origin", request_id=request["id"], option_id=choice["id"])
                    assert repeated["ok"] is False
                    assert len(resumes) == 1
                    assert len(store.list_work_items()) == 1
            finally:
                coordinator.close()
    asyncio.run(run())


def test_ambiguous_amendment_selection_carries_the_canonical_work_item(tmp_path):
    async def run():
        project_path = tmp_path / "project"
        project_path.mkdir()
        with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            project = store.create_or_get_project(project_path, name="Project")
            items = [store.create_work_item(project.project_id, title=title, workspace_path=project_path)
                for title in ("first", "second")]
            for item in items:
                store.create_attempt(item.work_item_id, provider="codex", task=item.title,
                    metadata={"session_id": "amend-origin"})
            with patch("server.work_ledger_coordinator.cwd_in_project_registry", return_value=True):
                catalog, complete, reason = candidate_catalog_from_coordinator(coordinator, "amend-origin")
            assert complete, reason
            candidates = [candidate for candidate in catalog if candidate.kind == "work_item"]
            attention = AttentionRequestCoordinator()
            resume = AsyncMock(return_value={"ok": True})
            request = await create_reference_selection(session_id="amend-origin", task_text="Change the title",
                attrs={"intent": "amend"}, candidates=candidates, resume=resume, coordinator=attention)
            selected = next(option for option in request["options"] if option["label"] == items[1].title)
            await attention.resolve(session_id="amend-origin", request_id=request["id"], option_id=selected["id"])
            plan = resume.await_args.args[0]
            assert plan.session_id == "amend-origin"
            assert plan.task_text == "Change the title"
            assert plan.attrs["intent"] == "amend"
            assert plan.attrs["workspace_ref"] == items[1].work_item_id
            assert len(store.list_work_items()) == 2
            coordinator.close()
    asyncio.run(run())


def test_fresh_execution_bypasses_reference_lookup_and_preserves_session_draft(tmp_path):
    async def run():
        with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            query, resume = AsyncMock(), AsyncMock()
            result = await adjudicate_focus_reference(coordinator=coordinator, session_id="draft-origin",
                utterance="Create a fresh game", task_text="Create the game",
                attrs={"intent": "execute", "target": "desktop"}, query=query, resume=resume)
            assert result.status == "bypass"
            query.assert_not_awaited()
            resume.assert_not_awaited()
            with patch("config.settings.WORK_SCRATCH_ROOT", str(tmp_path / "drafts")):
                route = coordinator.resolve_workspace_route({"session_id": "draft-origin"})
            assert route["status"] == "resolved"
            assert route["source"] == "scratch_default"
            assert coordinator.session_project("draft-origin") == ""
            assert store.list_work_items() == []
            coordinator.close()
    asyncio.run(run())
