"""Work-result entry stays distinct from a preselected AppSession replacement."""
import asyncio
import json

import pytest

from agent_host.work_ledger_store import WorkLedgerStore
from server.attention_request import AttentionRequestCoordinator
from server.auip_app_connection import AuipAppRequestHandler
from server.auip_control_decision import (
    _ACTIVE_RESULT_ENTRY_SYSTEM_PROMPT,
    _ACTIVE_SESSION_SYSTEM_PROMPT,
    _INACTIVE_ENTRY_SYSTEM_PROMPT,
    AuipControlDecisionResolver,
    parse_auip_control_decision,
    render_auip_role_grounding,
)
from server.auip_launch import AuipLaunchCoordinator
from server.auip_runtime import AuipRuntime
from server.event_bus import bus
from server.handlers.auip_handler import AuipHandler
from server.protocol import Method
from server.work_ledger_coordinator import WorkLedgerCoordinator
from server.work_preview import WorkPreviewManager
from test_auip_control_decision import _Catalog, _Runtime
from test_auip_launch import SESSION, _manifest, _register_file, _seed_app


def test_result_entry_prompt_profile_is_opt_in_and_active_only():
    async def run():
        active_calls = []

        async def active_query(messages):
            active_calls.append(messages)
            return json.dumps(
                {"action": "none", "work_relation": "subsumed", "read": []}
            )

        active_resolver = AuipControlDecisionResolver(
            query=active_query,
            app_runtime=_Runtime(
                {
                    "app_session_id": "app-notes",
                    "status": "active",
                    "app": {"title": "Pocket Notes"},
                    "available_modes": ["observe", "collaborate"],
                    "state": {},
                }
            ),
            launch_catalog=_Catalog(),
        )
        await active_resolver.capture(session_id="chat", user_text="聊聊这个应用。")
        await active_resolver.capture(
            session_id="chat",
            user_text="改好以后打开。",
            result_entry=True,
        )
        marker = "\n\n[Host AUIP capability facts]\n"
        assert active_calls[0][0]["content"].split(marker, 1)[0] == (
            _ACTIVE_SESSION_SYSTEM_PROMPT
        )
        assert active_calls[1][0]["content"].split(marker, 1)[0] == (
            _ACTIVE_RESULT_ENTRY_SYSTEM_PROMPT
        )

        inactive_calls = []

        async def inactive_query(messages):
            inactive_calls.append(messages)
            return json.dumps({"action": "none"})

        inactive_resolver = AuipControlDecisionResolver(
            query=inactive_query,
            app_runtime=_Runtime(),
            launch_catalog=_Catalog("Pocket Notes"),
        )
        await inactive_resolver.capture(session_id="chat", user_text="先聊聊。")
        await inactive_resolver.capture(
            session_id="chat",
            user_text="先聊聊。",
            result_entry=True,
        )
        assert len(inactive_calls) == 2
        assert {
            call[0]["content"].split(marker, 1)[0] for call in inactive_calls
        } == {_INACTIVE_ENTRY_SYSTEM_PROMPT}

    asyncio.run(run())


@pytest.mark.parametrize("target", ["", "Pocket Notes", "菜谱网页"])
def test_active_result_entry_preserves_reference_without_choosing_replacement(target):
    async def run():
        calls = []
        raw = json.dumps({"action":"engage", "timing":"after_work",
            "mode":"collaborate", "target":target, "work_relation":"independent"})

        async def query(messages):
            calls.append(messages)
            return raw

        resolver = AuipControlDecisionResolver(query=query,
            app_runtime=_Runtime({"app_session_id":"app-notes", "status":"active",
                "app":{"title":"Pocket Notes"}, "available_modes":["observe", "collaborate"],
                "state":{}}), launch_catalog=_Catalog(),
            has_active_work=lambda _session: ("attempt-recipe",))
        decision = await resolver.capture(session_id="chat", user_text="改好后再打开看看。")
        assert len(calls) == 1
        assert decision.status == "ok"
        assert decision.action == "engage"
        assert decision.target == target
        assert decision.raw_reply == raw
        assert decision.control_attrs() == {"action":"engage", "target":"delivery",
            "mode":"collaborate", "after":"work", "_host_app_session_id":"app-notes",
            "_host_active_work_attempt_ids":("attempt-recipe",)}
        grounding = render_auip_role_grounding(decision)
        assert "open_work_result_after_completion" in grounding
        assert "replace_after_work_completion" not in grounding

    asyncio.run(run())


def test_legacy_explicit_replacement_does_not_capture_a_different_target():
    raw = json.dumps({"action":"launch", "timing":"after_work",
        "mode":"collaborate", "target":"菜谱网页", "work_relation":"independent"})
    decision = parse_auip_control_decision(raw, has_active=True,
        active_title="Pocket Notes", candidate_titles=set(), allow_after_work=True)
    assert decision.status == "invalid"
    assert decision.control_attrs() is None


def test_result_entry_still_requires_deferred_capability():
    raw = json.dumps({"action":"engage", "timing":"after_work",
        "mode":"collaborate", "target":"", "work_relation":"independent"})
    decision = parse_auip_control_decision(raw, has_active=True,
        active_title="Pocket Notes", candidate_titles=set(), allow_after_work=False)
    assert decision.status == "invalid"
    assert decision.control_attrs() is None


@pytest.mark.parametrize("result_entry, action", [(False, "launch"), (True, "engage")])
def test_inactive_result_entry_retains_profile_without_inventing_a_source(result_entry, action):
    async def run():
        async def query(_messages):
            return '{"action":"engage","timing":"after_work","mode":"observe","target":""}'

        resolver = AuipControlDecisionResolver(query=query, app_runtime=_Runtime(), launch_catalog=_Catalog())
        decision = await resolver.capture(session_id="chat", user_text="做好后打开看看。",
            include_work_followup=True, result_entry=result_entry)
        assert decision.action == action
        assert decision.app_session_id == ""
        assert decision.control_attrs() == {"action":action, "target":"delivery",
            "mode":"observe", "after":"work"}

    asyncio.run(run())


@pytest.mark.parametrize("same_work", [True, False])
def test_work_result_entry_uses_existing_launch_close_receipt_and_preview(tmp_path, same_work):
    async def run():
        events = []

        async def emit(method, payload):
            events.append((method, dict(payload)))

        with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
            project = store.create_or_get_project(tmp_path, name="Applications")
            notes, _old_attempt, old_artifact = _seed_app(store, project, tmp_path,
                title="Pocket Notes", turn_id="old-notes")
            target = notes
            if not same_work:
                target, _recipe_attempt, _recipe_artifact = _seed_app(store, project,
                    tmp_path, title="Recipes", turn_id="old-recipes")
            runtime = AuipRuntime()
            preview = WorkPreviewManager(store, publisher=emit)
            launch = AuipLaunchCoordinator(artifacts=store,
                work_roster=WorkLedgerCoordinator(store),
                attention=AttentionRequestCoordinator(), emit=emit)
            host = AuipHandler(runtime, artifacts=store, current_session_id=lambda: SESSION,
                launch=launch, preview_handoff=preview.begin_auip_handoff)
            launch.before_result_entry = host.prepare_result_entry
            app = AuipAppRequestHandler(runtime)
            subscriptions = [(Method.AUIP_UPDATED, preview.on_auip_updated),
                (Method.AUIP_UPDATED, launch.on_app_updated),
                (Method.AUIP_SURFACE_CLOSE_REQUESTED, emit)]
            for method, callback in subscriptions:
                bus.on(method, callback)
            try:
                prepared = await host.handle(Method.AUIP_ATTACH_PREPARE,
                    {"artifact_id":old_artifact.artifact_id, "mode":"collaborate"})
                assert prepared["ok"] is True
                registered = await app.handle(Method.AUIP_REGISTER,
                    {"manifest":_manifest("Pocket Notes"), "attach_ticket":prepared["attach_ticket"]})
                old_app_id = registered["app_session_id"]
                await preview.on_auip_updated(registered)
                assert (await preview.get(notes.work_item_id))["appSessionId"] == old_app_id

                attempt = store.create_attempt(target.work_item_id, provider="locus",
                    task="Add a search button", metadata={"session_id":SESSION, "turn_id":"result-entry"})
                reserved = await host.route_control({"action":"engage", "after":"work",
                    "target":"delivery", "mode":"observe", "_host_work_binding":"turn",
                    "_host_work_item_id":target.work_item_id, "_host_app_session_id":old_app_id},
                    session_id=SESSION, user_text="加个搜索，改好后打开看看。", turn_id="result-entry")
                assert reserved["deferred"] is True
                assert runtime.get(old_app_id)["status"] == "active"

                from pathlib import Path

                workspace = Path(target.workspace_path)
                entry = workspace / "index.html"
                entry.write_text("<!doctype html><title>updated</title>", encoding="utf-8")
                _register_file(store, target, attempt, entry)
                _register_file(store, target, attempt, workspace / "auip.manifest.json")
                store.update_attempt(attempt.attempt_id, execution_status="succeeded")
                await launch.on_work_updated(Method.WORK_UPDATED, {})
                opened = [payload for method, payload in events if method == Method.AUIP_LAUNCH_REQUESTED]
                closes = [payload for method, payload in events if method == Method.AUIP_SURFACE_CLOSE_REQUESTED]
                if same_work:
                    assert opened == []
                    assert len(closes) == 1
                    assert closes[0]["app_session_id"] == old_app_id
                    assert (await preview.get(notes.work_item_id))["appSessionId"] == old_app_id
                    receipt = await host.handle(Method.AUIP_SURFACE_CLOSE_RESULT,
                        {"app_session_id":old_app_id, "host_surface_id":prepared["host_surface_id"],
                            "status":"closed"})
                    assert receipt["ok"] is True
                    opened = [payload for method, payload in events if method == Method.AUIP_LAUNCH_REQUESTED]
                else:
                    assert closes == []
                    assert runtime.get(old_app_id)["status"] == "active"
                assert len(opened) == 1
                assert opened[0]["work_item_id"] == target.work_item_id

                ready = await host.handle(Method.AUIP_ATTACH_PREPARE,
                    {"artifact_id":opened[0]["artifact_id"], "request_id":opened[0]["request_id"]})
                assert ready["ok"] is True
                # Each application has its own restricted connection owner.
                attached = await AuipAppRequestHandler(runtime).handle(Method.AUIP_REGISTER,
                    {"manifest":_manifest(target.title), "attach_ticket":ready["attach_ticket"]})
                assert attached["ok"] is True
                await preview.on_auip_updated(attached)
                surface = await preview.get(target.work_item_id)
                assert surface["lifecycle"] == "attached"
                assert surface["attemptId"] == attempt.attempt_id
                assert surface["appSessionId"] == attached["app_session_id"] != old_app_id
                await launch.on_work_updated(Method.WORK_UPDATED, {})
                assert len([1 for method, _payload in events if method == Method.AUIP_LAUNCH_REQUESTED]) == 1
                assert len(store.list_attempts(target.work_item_id)) == 2
            finally:
                for method, callback in subscriptions:
                    bus.off(method, callback)
                await preview.close_all()

    asyncio.run(run())
