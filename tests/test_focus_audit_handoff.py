"""One input-bound modifier audit precedes canonical history and Host dispatch."""
import asyncio
from copy import deepcopy
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from server.focus_policy import audit_focus_modifier, finalize_work_focus_modifiers
from core.chat_runtime import ChatRuntime, _TurnState
import config.settings as settings
from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST
from agent_host.provider_runtime import ProviderRuntime
from agent_host.work_ledger_store import WorkLedgerStore
from server.app import _handle_delegate
from server.interaction_branch import InteractionBranchCoordinator
from server.reference_catalog import TypedReferenceCandidate
from server.work_ledger_coordinator import WorkLedgerCoordinator


SOURCE = "把 Atlas 项目当前源码里的导航栏改成深色。"


async def observe(directory, *, source=SOURCE, audit_reply="NONE"):
    atlas_path, quartz_path = directory / "Atlas", directory / "Quartz"
    atlas_path.mkdir()
    quartz_path.mkdir()
    (atlas_path / "navigation.html").write_text("<nav>navigation</nav>", encoding="utf-8")
    provider_runtime = ProviderRuntime()
    adapter = SimpleNamespace(
        provider_id=CODEX_APP_SERVER_MANIFEST.provider_id,
        manifest=CODEX_APP_SERVER_MANIFEST,
        reconcile_submission=AsyncMock(side_effect=AssertionError("no Provider query")),
        run=AsyncMock(side_effect=AssertionError("no Provider execution")),
    )
    provider_runtime.register(adapter)
    with WorkLedgerStore(directory / "ledger.sqlite3") as store:
        coordinator = WorkLedgerCoordinator(store)
        atlas = store.create_or_get_project(atlas_path, name="Atlas", project_id="project_atlas")
        quartz = store.create_or_get_project(quartz_path, name="Quartz", project_id="project_quartz")
        candidate = TypedReferenceCandidate("project", atlas.project_id, "Atlas", "persistent")
        control = {"provider":"codex", "intent":"amend", "subject":"project", "focus":"set",
                   "project_id":atlas.project_id, "task":source, "_host_workspace_access":"write",
                   "_host_control_reference_candidates":(candidate,)}
        class Observer:
            def capture(self, _batch):
                return SimpleNamespace(decision_status="ok", outcome="agree", canonical_actions=(control,), notes=(), reason="")
        async def no_branch(*_args, **_kwargs):
            raise AssertionError("no Browser execution")
        branches = InteractionBranchCoordinator(provider_run=no_branch, root=directory / "branches")
        dispatch_tasks, prepared_requests = [], []
        async def start(request):
            prepared_requests.append(coordinator.prepare_request(request))
            return SimpleNamespace(task_handle=None, result="intake_only", error="", metadata={"result_type":"ok"})
        def record(actions):
            async def execute():
                return [await _handle_delegate(a["attrs"].get("task", ""), a["attrs"]) for a in actions]
            task = asyncio.create_task(execute())
            dispatch_tasks.append(task)
            return task
        with (
            patch.object(settings, "WORK_PROJECT_ALLOWLIST", f"{atlas_path};{quartz_path}"),
            patch.object(settings, "WORK_WORKTREE_ISOLATION", False),
            patch("server.work_ledger_coordinator.get_work_ledger_coordinator", return_value=coordinator),
            patch("server.interaction_branch.get_interaction_branch_coordinator", return_value=branches),
            patch("core.session_manager.get_current_session_id", return_value="focus_history"),
            patch("server.app._latest_user_message", return_value=source),
            patch("server.app._user_message_before_current", return_value="我们正在 Quartz 项目。"),
            patch("server.app._immediately_preceding_assistant_was_interrupted", return_value=False),
            patch("llm.client.remote_llm_query", return_value=audit_reply) as audit,
            patch("core.chat_runtime.record_actions", side_effect=record),
            patch("agent_host.provider_runtime.runtime", provider_runtime),
            patch.object(provider_runtime, "start", new=AsyncMock(side_effect=start)),
        ):
            coordinator.set_session_project("focus_history", quartz.project_id)
            runtime = ChatRuntime()
            runtime.configure(control_proposal_observer=Observer(), control_proposal_authority=True)
            state = _TurnState(gui_callback=None, turn_id="focus_history_turn", question=source, session_id="focus_history")
            runtime._consume_stream_chunk(state, runtime._control_history_tag({"attrs":control}))
            await runtime._wait_for_control_authority(state)
            results = await asyncio.gather(*dispatch_tasks)
            assert len(prepared_requests) == 1 and audit.call_count == 1
            request = prepared_requests[0]
            report = {"native_model_calls":0, "provider_executions":0, "scripted_focus_audits":audit.call_count,
                "default_project":coordinator.session_project("focus_history"),
                "work_project":request.metadata["work"]["project_id"],
                "history":state.history_response, "history_claims_focus_set":'focus="set"' in state.history_response,
                "dispatch_results":results}
            expected_default = atlas.project_id if audit_reply == "SET" else quartz.project_id
            assert report["default_project"] == expected_default and report["work_project"] == atlas.project_id
        coordinator.close()
        return report


def action():
    return {"type": "DELEGATE", "attrs": {"intent": "amend", "focus": "set", "project_id": "project_a",
        "task": "Update navigation", "_host_source_user_text": "切到 A 项目，并修改导航栏。"}}


def test_same_modifier_input_reuses_the_actual_typed_audit():
    async def run():
        item = action()
        with patch("llm.client.remote_llm_query", return_value="SET") as query:
            await finalize_work_focus_modifiers([item])
            audit = await audit_focus_modifier(deepcopy(item["attrs"]))
        assert audit.allowed and query.call_count == 1
        assert audit.request_fingerprint
    asyncio.run(run())


@pytest.mark.parametrize("change", [
    {"_host_source_user_text": "仅修改 A 项目的导航栏。"}, {"intent": "report"},
    {"focus": "clear"}, {"project_id": ""},
])
def test_changed_audit_input_cannot_reuse_an_earlier_confirmation(change):
    async def run():
        item = action()
        with patch("llm.client.remote_llm_query", return_value="SET"):
            await finalize_work_focus_modifiers([item])
        item["attrs"].update(change)
        with patch("llm.client.remote_llm_query", return_value="NONE") as query:
            audit = await audit_focus_modifier(item["attrs"])
        assert not audit.allowed and query.call_count == 1
    asyncio.run(run())


def test_model_shaped_audit_data_is_not_a_host_receipt():
    async def run():
        item = action()
        item["attrs"]["_host_focus_modifier_audit"] = {"allowed": True, "outcome": "confirmed"}
        with patch("llm.client.remote_llm_query", return_value="NONE") as query:
            await finalize_work_focus_modifiers([item])
        assert "focus" not in item["attrs"] and query.call_count == 1
    asyncio.run(run())


def test_ordinary_work_and_pure_focus_keep_their_existing_owners():
    async def run():
        ordinary = action()
        ordinary["attrs"].pop("focus")
        pure = action()
        pure["attrs"]["intent"] = "focus"
        with patch("llm.client.remote_llm_query", side_effect=AssertionError("unexpected audit")):
            await finalize_work_focus_modifiers([ordinary, pure])
        assert pure["attrs"]["focus"] == "set"
    asyncio.run(run())


def test_real_intake_and_history_agree_after_modifier_is_removed(tmp_path):
    result = asyncio.run(observe(tmp_path))
    assert result["work_project"] == "project_atlas"
    assert result["default_project"] == "project_quartz"
    assert not result["history_claims_focus_set"]
    assert result["scripted_focus_audits"] == 1


def test_confirmed_switch_reaches_real_intake_without_a_second_audit(tmp_path):
    result = asyncio.run(observe(tmp_path, source="切到 Atlas 项目，并把导航栏改成深色。", audit_reply="SET"))
    assert result["default_project"] == result["work_project"] == "project_atlas"
    assert result["history_claims_focus_set"]
    assert result["scripted_focus_audits"] == 1


def test_cancel_during_focus_audit_does_not_record_or_dispatch():
    async def run():
        entered, release = threading.Event(), threading.Event()
        def slow_query(*_args, **_kwargs):
            entered.set()
            assert release.wait(5)
            return "SET"
        control = action()["attrs"]
        class Observer:
            def capture(self, _batch):
                return SimpleNamespace(decision_status="ok", outcome="agree", canonical_actions=(control,), notes=(), reason="")
        runtime = ChatRuntime()
        runtime.configure(control_proposal_observer=Observer(), control_proposal_authority=True)
        state = _TurnState(gui_callback=None, turn_id="cancel_focus_audit", session_id="focus_audit",
                           question=control["_host_source_user_text"])
        with patch("llm.client.remote_llm_query", side_effect=slow_query), patch("core.chat_runtime.record_actions") as record:
            try:
                runtime._consume_stream_chunk(state, runtime._control_history_tag({"attrs":control}))
                assert await asyncio.to_thread(entered.wait, 3)
                state.control_authority_tasks[0].cancel()
                await runtime._wait_for_control_authority(state)
                record.assert_not_called()
                assert "[DELEGATE" not in state.history_response and "\x00CONTROL_AUTHORITY" not in state.history_response
            finally:
                release.set()
    asyncio.run(run())
