"""Assembly coverage for conversational project focus.

These tests deliberately enter through the raw DELEGATE tag.  The contract is
not satisfied when the model probe recognizes ``intent=focus`` but the host
dispatcher never invokes the focus handler.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.work_ledger_store import WorkLedgerStore
from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST
from agent_host.provider_runtime import runtime as provider_runtime
from llm.stream_parser import StreamTagParser
from server.scratch_workspace import is_scratch_path
from server import app as server_app
from server.canvas_action_router import CanvasActionRouter
from server.event_bus import bus
from server.handlers.provider_handler import ProviderHandler
from server.handlers.work_ledger_handler import WorkLedgerHandler
from server.protocol import Method
from server.work_ledger_coordinator import WorkLedgerCoordinator
from tools.text_utils import parse_tags_and_clean
from vts import action as action_dispatcher


async def _taskless_focus_from_raw_tag() -> None:
    with tempfile.TemporaryDirectory(prefix="focus_dispatch_assembly_") as temp:
        root = Path(temp)
        project_path = root / "amadeus"
        project_path.mkdir()
        store = WorkLedgerStore(root / "ledger.sqlite3")
        coordinator = WorkLedgerCoordinator(store)
        coordinator.configure()
        project = store.create_or_get_project(project_path, name="amadeus")
        updated = asyncio.Event()
        projections: list[dict] = []

        async def capture(_method: str, params: dict) -> None:
            projections.append(dict(params))
            updated.set()

        bus.on(Method.WORK_UPDATED, capture)
        try:
            parser = StreamTagParser()
            _cleaned, actions = parser.process_chunk(
                f'[DELEGATE provider="locus" intent="focus" '
                f'project_id="{project.project_id}"]'
            )
            assert len(actions) == 1

            with (
                patch.object(action_dispatcher, "_delegate_fn", server_app._handle_delegate),
                patch.object(
                    server_app,
                    "_speak_task_lookup_answer",
                    new=AsyncMock(return_value=True),
                ) as focus_confirmation,
                patch("core.session_manager.get_current_session_id", return_value="session-focus"),
                patch("config.settings.DELEGATE_INTENT_ATTRIBUTE", True),
                patch("config.settings.DELEGATE_FOCUS_INTENT", True),
                patch("config.settings.WORK_PROJECT_ALLOWLIST", str(project_path)),
                patch.object(provider_runtime, "start", new=AsyncMock()) as provider_start,
            ):
                batch = action_dispatcher.record_actions(actions)
                assert batch is not None
                await batch
                assert updated.is_set()
                await asyncio.gather(*tuple(server_app._focus_confirmation_tasks))

                assert coordinator.session_project("session-focus") == project.project_id
                assert projections[-1]["work"]["destinationLabel"] == "amadeus"
                assert store.list_work_items() == []
                provider_start.assert_not_awaited()
                focus_confirmation.assert_awaited_once()
                assert "amadeus" in focus_confirmation.await_args.args[0]
        finally:
            bus.off(Method.WORK_UPDATED, capture)
            coordinator.close()


def test_taskless_focus_runs_through_the_real_tag_dispatch_chain() -> None:
    asyncio.run(_taskless_focus_from_raw_tag())
    print("ok: raw taskless focus reaches the host and refreshes the projection")


async def _resolved_focus_confirmation_has_one_owner() -> None:
    with tempfile.TemporaryDirectory(prefix="resolved_focus_confirmation_") as temp:
        root = Path(temp)
        project_path = root / "ETERNAL_LOOP"
        project_path.mkdir()
        store = WorkLedgerStore(root / "ledger.sqlite3")
        coordinator = WorkLedgerCoordinator(store)
        coordinator.configure()
        project = store.create_or_get_project(project_path, name="ETERNAL_LOOP")
        spoken = AsyncMock(return_value=True)
        try:
            with (
                patch.object(server_app, "_speak_task_lookup_answer", new=spoken),
                patch(
                    "core.session_manager.get_current_session_id",
                    return_value="session-resolved-focus",
                ),
                patch("config.settings.DELEGATE_INTENT_ATTRIBUTE", True),
                patch("config.settings.DELEGATE_FOCUS_INTENT", True),
                patch("config.settings.WORK_PROJECT_ALLOWLIST", str(project_path)),
            ):
                result = await server_app._handle_delegate(
                    "",
                    {
                        "provider": "locus",
                        "intent": "focus",
                        "project_id": project.project_id,
                        "_host_reference_resolved": True,
                    },
                )
                await asyncio.gather(*tuple(server_app._focus_confirmation_tasks))
                assert "now working" in str(result)
                spoken.assert_awaited_once()
                assert "ETERNAL_LOOP" in spoken.await_args.args[0]

                spoken.reset_mock()
                await server_app._handle_delegate(
                    "",
                    {
                        "provider": "locus",
                        "intent": "focus",
                        "project_id": project.project_id,
                        "_host_reference_resolved": True,
                        "_host_reference_selection_resumed": True,
                    },
                )
                await asyncio.gather(*tuple(server_app._focus_confirmation_tasks))
                spoken.assert_not_awaited()
        finally:
            coordinator.close()


def test_immediate_reference_resolution_confirms_focus_without_card_duplication() -> None:
    asyncio.run(_resolved_focus_confirmation_has_one_owner())
    print("ok: immediate focus resolution confirms once; Slice resume owns its acknowledgement")


async def _taskless_project_report_from_raw_tag() -> None:
    with tempfile.TemporaryDirectory(prefix="report_dispatch_assembly_") as temp:
        root = Path(temp)
        project_path = root / "endless-game"
        project_path.mkdir()
        store = WorkLedgerStore(root / "ledger.sqlite3")
        coordinator = WorkLedgerCoordinator(store)
        coordinator.configure()
        project = store.create_or_get_project(project_path, name="ETERNAL_LOOP")
        item = store.create_work_item(
            project.project_id,
            title="Improve monster rendering",
            goal="Make progression visible",
        )
        store.set_work_item_state(item.work_item_id, "accepted")
        spoken = AsyncMock(return_value=True)
        before = tuple(row.work_item_id for row in store.list_work_items(limit=200))
        try:
            _cleaned, actions = parse_tags_and_clean(
                f'[DELEGATE provider="locus" intent="report" subject="project" '
                f'project_id="{project.project_id}"]'
            )
            with (
                patch.object(action_dispatcher, "_delegate_fn", server_app._handle_delegate),
                patch.object(server_app, "_speak_task_lookup_answer", new=spoken),
                patch.object(provider_runtime, "start", new=AsyncMock()) as provider_start,
                patch("config.settings.DELEGATE_INTENT_ATTRIBUTE", True),
                patch("config.settings.TASK_LOOKUP_ENABLED", True),
                patch("server.app._observer_display_language", return_value="simplified_chinese"),
            ):
                batch = action_dispatcher.record_actions(actions)
                assert batch is not None
                await batch

            provider_start.assert_not_awaited()
            spoken.assert_awaited_once()
            assert "ETERNAL_LOOP" in spoken.await_args.args[0]
            assert spoken.await_args.kwargs["history_marker"] == "PROJECT_STATUS"
            assert tuple(
                row.work_item_id for row in store.list_work_items(limit=200)
            ) == before
        finally:
            coordinator.close()


def test_taskless_project_report_runs_through_real_tag_and_ledger() -> None:
    asyncio.run(_taskless_project_report_from_raw_tag())
    print("ok: raw taskless project report reaches deterministic ledger truth")


def _provider_record(*, result: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        task_handle=None,
        result=result,
        error="",
        metadata={"result_type": "ok"},
    )


async def _compound_focus_and_task_from_one_raw_tag(*, declaration: str) -> None:
    with tempfile.TemporaryDirectory(prefix="focus_task_assembly_") as temp:
        root = Path(temp)
        project_path = root / "amadeus"
        project_path.mkdir()
        store = WorkLedgerStore(root / "ledger.sqlite3")
        coordinator = WorkLedgerCoordinator(store)
        coordinator.configure()
        project = store.create_or_get_project(project_path, name="amadeus")
        prepared_requests = []

        async def start(request):
            prepared = coordinator.prepare_request(request)
            prepared_requests.append(prepared)
            return _provider_record()

        try:
            parser = StreamTagParser()
            _cleaned, actions = parser.process_chunk(
                f'[DELEGATE provider="codex" {declaration} '
                f'project_id="{project.project_id}" task="Update README"]'
            )
            with (
                patch.object(action_dispatcher, "_delegate_fn", server_app._handle_delegate),
                patch("core.session_manager.get_current_session_id", return_value="session-compound"),
                patch("config.settings.DELEGATE_INTENT_ATTRIBUTE", True),
                patch("config.settings.DELEGATE_FOCUS_INTENT", True),
                patch("config.settings.WORK_WORKTREE_ISOLATION", False),
                patch(
                    "server.work_ledger_coordinator.cwd_in_project_registry",
                    return_value=True,
                ),
                patch.object(provider_runtime, "start", new=AsyncMock(side_effect=start)),
                patch.object(
                    provider_runtime,
                    "provider_manifests",
                    return_value=(CODEX_APP_SERVER_MANIFEST,),
                ),
                patch.object(
                    provider_runtime,
                    "get_manifest",
                    return_value=CODEX_APP_SERVER_MANIFEST,
                ),
            ):
                batch = action_dispatcher.record_actions(actions)
                assert batch is not None
                await batch

            items = store.list_work_items()
            assert len(items) == 1
            item = items[0]
            attempts = store.list_attempts(item.work_item_id)
            assert len(attempts) == 1
            assert Path(item.workspace_path) == project_path
            assert prepared_requests[0].cwd == str(project_path)
            assert item.metadata["intent"] == "execute"
            assert item.metadata["focus_applied"] is True
            assert attempts[0].metadata["intent"] == "execute"
            assert attempts[0].metadata["focus_applied"] is True
            assert prepared_requests[0].metadata["delegate_attrs"]["intent"] == "execute"
            assert "focus" not in prepared_requests[0].metadata["delegate_attrs"]
        finally:
            coordinator.close()


def test_focus_is_a_modifier_when_the_same_tag_contains_work() -> None:
    asyncio.run(
        _compound_focus_and_task_from_one_raw_tag(
            declaration='intent="execute" focus="set"',
        )
    )
    print("ok: orthogonal focus modifier switches first and preserves execute")


def test_legacy_focus_plus_task_remains_compatible() -> None:
    asyncio.run(
        _compound_focus_and_task_from_one_raw_tag(
            declaration='intent="focus"',
        )
    )
    print("ok: legacy focus plus task still degrades to one execute")


async def _grounded_attrs_survive_legacy_raw_reparse() -> None:
    """Raw transport is evidence of what the model said, not final authority."""

    captured: list[tuple[str, dict]] = []

    async def capture(task: str, attrs: dict) -> str:
        captured.append((task, dict(attrs)))
        return "ok"

    action = {
        "type": "DELEGATE",
        "raw": (
            '[DELEGATE provider="locus" intent="focus" project_id="wrong" '
            'task="inspect route-note.txt"]'
        ),
        "attrs": {
            "provider": "locus",
            "intent": "amend",
            "project_id": "wrong",
            "task": "grounded task",
            "workspace_ref": "work_latest",
            "amend_inferred": True,
            "_host_source_user_text": "读取 route-note.txt",
        },
    }
    with patch.object(action_dispatcher, "_delegate_fn", capture):
        batch = action_dispatcher.record_actions([action])
        assert batch is not None
        await batch

    assert len(captured) == 1
    task, attrs = captured[0]
    assert task == "grounded task"
    assert attrs["intent"] == "amend"
    assert attrs["workspace_ref"] == "work_latest"
    assert attrs["amend_inferred"] is True
    assert attrs["_host_source_user_text"] == "读取 route-note.txt"


def test_host_grounding_cannot_be_rolled_back_by_raw_tag_transport() -> None:
    asyncio.run(_grounded_attrs_survive_legacy_raw_reparse())
    print("ok: raw tag transport preserves every host-grounded attribute")


async def _draft_exits_and_one_off() -> None:
    with tempfile.TemporaryDirectory(prefix="focus_draft_exits_") as temp:
        root = Path(temp)
        project_path = root / "amadeus"
        project_path.mkdir()
        store = WorkLedgerStore(root / "ledger.sqlite3")
        coordinator = WorkLedgerCoordinator(store)
        coordinator.configure()
        project = store.create_or_get_project(project_path, name="amadeus")
        requests = []

        async def start(request):
            prepared = coordinator.prepare_request(request)
            requests.append(prepared)
            return _provider_record()

        patches = (
            patch.object(action_dispatcher, "_delegate_fn", server_app._handle_delegate),
            patch("core.session_manager.get_current_session_id", return_value="session-exits"),
            patch("config.settings.DELEGATE_INTENT_ATTRIBUTE", True),
            patch("config.settings.DELEGATE_FOCUS_INTENT", True),
            patch("config.settings.WORK_WORKTREE_ISOLATION", False),
            patch("config.settings.WORK_SCRATCH_ROOT", str(root / "scratch")),
            patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ),
                patch.object(provider_runtime, "start", new=AsyncMock(side_effect=start)),
                patch.object(
                    provider_runtime,
                    "provider_manifests",
                    return_value=(CODEX_APP_SERVER_MANIFEST,),
                ),
                patch.object(
                    provider_runtime,
                    "get_manifest",
                    return_value=CODEX_APP_SERVER_MANIFEST,
                ),
        )
        try:
            with (
                patches[0],
                patches[1],
                patches[2],
                patches[3],
                patches[4],
                patches[5],
                patches[6],
                patches[7],
                patches[8],
                patches[9],
            ):
                coordinator.set_session_project("session-exits", project.project_id)

                # Exit A: taskless focus without project_id persistently clears.
                parser = StreamTagParser()
                _cleaned, actions = parser.process_chunk(
                    '[DELEGATE provider="locus" intent="focus"]'
                )
                batch = action_dispatcher.record_actions(actions)
                assert batch is not None
                await batch
                assert coordinator.session_project("session-exits") == ""
                assert store.list_work_items() == []

                await server_app._handle_delegate(
                    "Create a standalone chess note",
                    {"provider": "codex", "intent": "execute"},
                )
                first = store.list_work_items()[0]
                assert is_scratch_path(first.workspace_path)

                # Exit B: this instruction alone goes to Drafts; focus stays.
                coordinator.set_session_project("session-exits", project.project_id)
                await server_app._handle_delegate(
                    "Create a separate one-off board",
                    {"provider": "codex", "intent": "execute", "one_off": "true"},
                )
                second = store.list_work_items()[0]
                assert second.work_item_id != first.work_item_id
                assert is_scratch_path(second.workspace_path)
                assert coordinator.session_project("session-exits") == project.project_id

                # Exit C: clear is orthogonal when the same utterance also
                # starts work. The task uses Drafts and future turns stay there.
                await server_app._handle_delegate(
                    "Create a standalone rules note and leave the project",
                    {
                        "provider": "codex",
                        "intent": "execute",
                        "focus": "clear",
                    },
                )
                third = store.list_work_items()[0]
                assert third.work_item_id not in {first.work_item_id, second.work_item_id}
                assert is_scratch_path(third.workspace_path)
                assert third.metadata["intent"] == "execute"
                assert third.metadata["focus_applied"] is True
                assert coordinator.session_project("session-exits") == ""

                # A malformed set must fail closed instead of accidentally
                # clearing the current project and then running elsewhere.
                coordinator.set_session_project("session-exits", project.project_id)
                request_count = len(requests)
                refused = await server_app._handle_delegate(
                    "Must not run",
                    {
                        "provider": "codex",
                        "intent": "execute",
                        "focus": "set",
                    },
                )
                assert "project is required" in str(refused)
                assert len(requests) == request_count
                assert coordinator.session_project("session-exits") == project.project_id

                # The Slice recovery action clears the same projected state.
                current = coordinator.snapshot()
                result = await CanvasActionRouter(
                    work_action=WorkLedgerHandler(coordinator).route_action,
                ).route(
                    {
                        "target": "work_destination",
                        "action": "exit_project",
                        "revision": current["revision"],
                    }
                )
                assert result["ok"] is True
                assert result["work"]["destinationLabel"] == ""
                assert coordinator.session_project("session-exits") == ""
        finally:
            coordinator.close()


def test_both_draft_exits_preserve_their_distinct_scope() -> None:
    asyncio.run(_draft_exits_and_one_off())
    print("ok: persistent exit, one-off override, and Slice recovery stay distinct")


async def _rejected_and_retired_focus() -> None:
    with tempfile.TemporaryDirectory(prefix="focus_failure_projection_") as temp:
        root = Path(temp)
        project_path = root / "retired-project"
        project_path.mkdir()
        store = WorkLedgerStore(root / "ledger.sqlite3")
        coordinator = WorkLedgerCoordinator(store)
        coordinator.configure()
        project = store.create_or_get_project(project_path, name="retired-project")
        provider_start = AsyncMock()
        try:
            with (
                patch.object(action_dispatcher, "_delegate_fn", server_app._handle_delegate),
                patch("core.session_manager.get_current_session_id", return_value="session-failure"),
                patch("config.settings.DELEGATE_INTENT_ATTRIBUTE", True),
                patch("config.settings.DELEGATE_FOCUS_INTENT", True),
                patch("config.settings.WORK_PROJECT_ALLOWLIST", str(project_path)),
                patch.object(provider_runtime, "start", new=provider_start),
            ):
                parser = StreamTagParser()
                _cleaned, actions = parser.process_chunk(
                    '[DELEGATE provider="locus" intent="focus" '
                    'project_id="project-does-not-exist" task="Do not run"]'
                )
                batch = action_dispatcher.record_actions(actions)
                assert batch is not None
                await batch
                failure = coordinator.snapshot()
                assert failure["destinationFeedback"]["status"] == "rejected"
                assert store.list_work_items() == []
                provider_start.assert_not_awaited()

                project_path.rmdir()
                parser = StreamTagParser()
                _cleaned, actions = parser.process_chunk(
                    f'[DELEGATE provider="locus" intent="focus" '
                    f'project_id="{project.project_id}" task="Still do not run"]'
                )
                batch = action_dispatcher.record_actions(actions)
                assert batch is not None
                await batch
                missing = coordinator.snapshot()
                assert "folder" in missing["destinationFeedback"]["message"].lower()
                provider_start.assert_not_awaited()

                project_path.mkdir()
                store.set_project_state(project.project_id, "retired")
                parser = StreamTagParser()
                _cleaned, actions = parser.process_chunk(
                    f'[DELEGATE provider="locus" intent="focus" '
                    f'project_id="{project.project_id}"]'
                )
                batch = action_dispatcher.record_actions(actions)
                assert batch is not None
                await batch
                assert coordinator.session_project("session-failure") == project.project_id
                assert coordinator.snapshot()["destinationFeedback"] is None
        finally:
            coordinator.close()


def test_focus_failure_is_projected_and_retired_projects_remain_addressable() -> None:
    asyncio.run(_rejected_and_retired_focus())
    print("ok: rejected switches are visible and retired projects remain addressable")


async def _multiple_delegate_tags_are_serial() -> None:
    timeline = []
    finished = asyncio.Event()

    async def delegate(_task, attrs):
        intent = str(attrs.get("intent") or "")
        timeline.append(("start", intent))
        if intent == "focus":
            await asyncio.sleep(0.03)
        timeline.append(("done", intent))
        if intent == "execute":
            finished.set()

    _cleaned, actions = parse_tags_and_clean(
        '[DELEGATE provider="locus" intent="focus" project_id="project-a"]'
        '[DELEGATE provider="locus" intent="execute" task="Update README"]'
    )
    with patch.object(action_dispatcher, "_delegate_fn", delegate):
        batch = action_dispatcher.record_actions(actions)
        assert batch is not None
        await batch
        assert finished.is_set()
    assert timeline == [
        ("start", "focus"),
        ("done", "focus"),
        ("start", "execute"),
        ("done", "execute"),
    ]


def test_multiple_delegate_tags_are_awaited_in_source_order() -> None:
    asyncio.run(_multiple_delegate_tags_are_serial())
    print("ok: multiple DELEGATE tags execute serially without the focus race")


async def _taskless_gate_is_named() -> None:
    called = []

    async def delegate(task, attrs):
        called.append((task, attrs))

    _cleaned, actions = parse_tags_and_clean(
        '[DELEGATE provider="locus" intent="execute"]'
        '[DELEGATE provider="locus" intent="report"]'
    )
    with patch.object(action_dispatcher, "_delegate_fn", delegate):
        action_dispatcher.record_actions(actions)
        await asyncio.sleep(0)
    assert called == []

    _cleaned, actions = parse_tags_and_clean(
        '[DELEGATE provider="browser" intent="execute" action="open" '
        'url="https://example.test/page"]'
    )
    with patch.object(action_dispatcher, "_delegate_fn", delegate):
        batch = action_dispatcher.record_actions(actions)
        assert batch is not None
        await batch
    assert called == [
        (
            "",
            {
                "provider": "browser",
                "intent": "execute",
                "action": "open",
                "url": "https://example.test/page",
            },
        )
    ]
    called.clear()

    _cleaned, actions = parse_tags_and_clean(
        '[DELEGATE provider="locus" intent="report" subject="project" '
        'project_id="project-a"]'
    )
    with patch.object(action_dispatcher, "_delegate_fn", delegate):
        batch = action_dispatcher.record_actions(actions)
        assert batch is not None
        await batch
    assert called == [
        (
            "",
            {
                "provider": "locus",
                "intent": "report",
                "subject": "project",
                "project_id": "project-a",
            },
        )
    ]

    failed_batch = []

    async def fail_focus(_task, attrs):
        failed_batch.append(str(attrs.get("intent") or ""))
        if attrs.get("intent") == "focus":
            raise RuntimeError("simulated focus failure")

    _cleaned, actions = parse_tags_and_clean(
        '[DELEGATE provider="locus" intent="focus" project_id="project-a"]'
        '[DELEGATE provider="locus" intent="execute" task="Must not run"]'
    )
    with patch.object(action_dispatcher, "_delegate_fn", fail_focus):
        batch = action_dispatcher.record_actions(actions)
        assert batch is not None
        await batch
    assert failed_batch == ["focus"]


def test_taskless_dispatch_gate_is_a_named_allowlist() -> None:
    asyncio.run(_taskless_gate_is_named())
    print("ok: taskless gating admits controls and structured operations only")


if __name__ == "__main__":
    ProviderHandler()
    test_taskless_focus_runs_through_the_real_tag_dispatch_chain()
    test_immediate_reference_resolution_confirms_focus_without_card_duplication()
    test_taskless_project_report_runs_through_real_tag_and_ledger()
    test_focus_is_a_modifier_when_the_same_tag_contains_work()
    test_legacy_focus_plus_task_remains_compatible()
    test_host_grounding_cannot_be_rolled_back_by_raw_tag_transport()
    test_both_draft_exits_preserve_their_distinct_scope()
    test_focus_failure_is_projected_and_retired_projects_remain_addressable()
    test_multiple_delegate_tags_are_awaited_in_source_order()
    test_taskless_dispatch_gate_is_a_named_allowlist()
