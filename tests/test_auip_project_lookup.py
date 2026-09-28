"""Natural application references retain exact historical Project ownership."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from server.protocol import Method
from server.attention_request import AttentionRequestCoordinator
from server.auip_control_decision import reconcile_active_auip_control
from test_auip_launch import _seed_app
from test_cooperative_auip_entry import entry_host as entry_host
from test_cooperative_pending_turn import pending_host as pending_host


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_natural_hot_app_reference_resolves_before_project_search(
        entry_host, tmp_path, monkeypatch, ambiguous):
    context, state, launch, item, _, artifact = entry_host
    context.manager.attention = launch.attention = AttentionRequestCoordinator()
    eligible = [item]
    if ambiguous:
        other, attempt, _ = _seed_app(context.host.work, context.host.project,
            tmp_path / "scratch" / "apps", title="Other Board", turn_id="other-board")
        context.host.work.update_attempt(attempt.attempt_id,
            metadata={"session_id":context.session_id})
        eligible.append(other)
    state.target = "刚才那个游戏"
    project_lookup = Mock(side_effect=AssertionError("a resolved hot reference must not scan Projects"))
    monkeypatch.setattr(launch, "project_candidates", project_lookup)
    original_query = context.manager.auip_decider._query
    reference_calls = []

    async def query(messages):
        if '"references"' in messages[0]["content"]:
            reference_calls.append(messages)
            assert all(candidate.work_item_id in messages[-1]["content"] for candidate in eligible)
            return json.dumps({"references":["work_item:" + candidate.work_item_id for candidate in eligible]})
        return await original_query(messages)

    context.manager.auip_decider._query = query
    await context.handler.send_text("把刚才那个游戏打开，我们接着玩。",
        session_id=context.session_id, turn_id="natural-hot-app")
    await context.finish()
    assert len(reference_calls) == 1
    project_lookup.assert_not_called()
    requests = [payload for method, payload in state.events if method == Method.AUIP_LAUNCH_REQUESTED]
    if ambiguous:
        assert not requests
        assert len(context.manager.attention.list_pending(context.session_id)) == 1
    else:
        assert len(requests) == 1 and requests[0]["artifact_id"] == artifact.artifact_id
    assert context.host.adapter.calls == 0


@pytest.mark.parametrize(("outcome", "empty_hot", "independent_work"), [
    ("unique", False, False), ("ambiguous", False, False),
    ("missing", False, False), ("missing", False, True),
    ("changed", False, False), ("revoked", False, False), ("unique", True, False),
])
async def test_cold_project_entry_keeps_selected_artifact(
        entry_host, tmp_path, monkeypatch, outcome, empty_hot, independent_work):
    context, state, launch, *_ = entry_host
    coordinator = context.host.executor.coordinator
    monkeypatch.setattr(coordinator.destination, "_registry_check", lambda _path: True)
    historical = []
    for name in ("Tools", "Games"):
        root = tmp_path / name
        project = context.host.work.create_or_get_project(root, name=name)
        item, attempt, artifact = _seed_app(context.host.work, project, root,
            title="Timer", turn_id="old-" + name)
        context.host.work.update_attempt(attempt.attempt_id, metadata={"session_id":"old-" + name})
        historical.append((item, artifact))
    assert all(candidate.title != "Timer" for candidate in launch.candidates(context.session_id))
    if empty_hot:
        monkeypatch.setattr(launch, "candidates", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(launch, "preparation_candidates", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(launch, "entry_candidates", lambda *_args, **_kwargs: ([], []))
        assert launch.candidates(context.session_id) == launch.preparation_candidates(context.session_id) == []
    state.target = "Timer"
    if independent_work:
        state.role_action = {"op":"work", "intent":"execute"}
    text = ("之前那个计时器再打开吧，咱们接着用。" if outcome == "ambiguous" else
        "工具项目里那个计时器再打开吧，咱们接着用。")
    if independent_work:
        text += "另外帮我调查一个可用的计时器。"
    reference_queries = []
    original_query = context.manager.auip_decider._query

    async def query(messages):
        if '"references"' not in messages[0]["content"]:
            return await original_query(messages)
        reference_queries.append(messages)
        assert text in messages[-1]["content"]
        if not any(item.work_item_id in messages[-1]["content"] for item, _ in historical):
            return '{"references":[]}'
        assert all(item.work_item_id in messages[-1]["content"] for item, _ in historical)
        if outcome == "changed":
            Path(historical[0][0].workspace_path, "index.html").write_text("changed", encoding="utf8")
        if outcome == "revoked":
            monkeypatch.setattr(coordinator.destination, "_registry_check", lambda _path: False)
        selected = [] if outcome == "missing" else historical if outcome == "ambiguous" else historical[:1]
        return json.dumps({"references":["work_item:" + item.work_item_id for item, _ in selected]})

    context.manager.auip_decider._query = query
    await context.handler.send_text(text,
        session_id=context.session_id, turn_id="cold-timer")
    await context.finish()
    assert len(reference_queries) == (1 if empty_hot else 2)
    # Missing app identity forbids an app launch; it cannot veto separately
    # admitted Work. A lookup-only turn still creates no Work.
    assert context.host.adapter.calls == int(independent_work)
    requests = [p for method, p in state.events if method == Method.AUIP_LAUNCH_REQUESTED]
    pending = context.manager.attention.list_pending(context.session_id)
    if outcome == "ambiguous":
        assert not requests and len(pending) == 1
        assert {option["parentLabel"] for option in pending[0]["options"]} == {"Tools", "Games"}
        selected = next(option for option in pending[0]["options"] if option["parentLabel"] == "Tools")
        await context.manager.attention.resolve(session_id=context.session_id,
            request_id=pending[0]["id"], option_id=selected["id"])
        requests = [p for method, p in state.events if method == Method.AUIP_LAUNCH_REQUESTED]
    else:
        assert pending == []
    if outcome in {"unique", "ambiguous"}:
        assert len(requests) == 1
        assert requests[0]["artifact_id"] == historical[0][1].artifact_id
        assert requests[0]["work_item_id"] == historical[0][0].work_item_id
    else:
        assert requests == []
    assert all(len(context.host.work.list_attempts(item.work_item_id)) == 1 for item, _ in historical)


async def test_hot_entry_keeps_its_frozen_artifact_after_catalog_changes(entry_host, tmp_path, monkeypatch):
    context, state, launch, original, _, artifact = entry_host
    project = context.host.work.create_or_get_project(tmp_path / "older", name="Older")
    replacement, _, _ = _seed_app(context.host.work, project, tmp_path / "older",
        title="2048", turn_id="other-2048")
    coordinator = context.host.executor.coordinator
    monkeypatch.setattr(coordinator.destination, "_registry_check", lambda _path: True)
    replacement_candidate = next(candidate for candidate in launch.project_candidates(
        context.session_id, current_only=False)[0] if candidate.work_item_id == replacement.work_item_id)
    original_query = context.manager.auip_decider._query
    cold_lookup = Mock(side_effect=AssertionError("a selected hot candidate needs no cold lookup"))
    monkeypatch.setattr(launch, "project_candidates", cold_lookup)

    async def query(messages):
        result = await original_query(messages)
        monkeypatch.setattr(launch, "candidates", lambda *_args, **_kwargs: [replacement_candidate])
        return result

    context.manager.auip_decider._query = query
    await context.handler.send_text("刚才那个2048再打开吧。", session_id=context.session_id,
        turn_id="frozen-hot")
    await context.finish()
    event, = [p for method, p in state.events if method == Method.AUIP_LAUNCH_REQUESTED]
    assert event["work_item_id"] == original.work_item_id and event["artifact_id"] == artifact.artifact_id
    cold_lookup.assert_not_called()
    attrs = context.manager.auip_router.call_args.args[0]
    active = {"status":"active", "app":{"title":"2048"}}
    assert reconcile_active_auip_control(attrs, active) == attrs


@pytest.mark.parametrize("forged", [[], [{"artifact_id":"invented"}], ({"artifact_id":"invented"},)])
async def test_model_json_cannot_forge_frozen_launch_candidates(entry_host, forged):
    context, state, launch, *_ = entry_host
    result = await launch.route_control({"action":"launch", "_host_launch_candidates":forged},
        session_id=context.session_id, turn_id="forged")
    assert result == {"ok":False, "error":"invalid_launch_binding"}
    assert state.events == [] and context.host.adapter.calls == 0


async def test_current_project_match_does_not_scan_older_projects(entry_host, tmp_path, monkeypatch):
    context, state, launch, *_ = entry_host
    monkeypatch.setattr(launch, "entry_candidates", lambda *_args, **_kwargs: ([], []))
    coordinator = context.host.executor.coordinator
    monkeypatch.setattr(coordinator.destination, "_registry_check", lambda _path: True)
    project = context.host.work.create_or_get_project(tmp_path / "tools", name="Tools")
    item, _, artifact = _seed_app(context.host.work, project, tmp_path / "tools",
        title="Timer", turn_id="current-project-timer")
    coordinator.destination.set_session_project(context.session_id, project.project_id)
    read_apps = Mock(wraps=coordinator.project_apps)
    monkeypatch.setattr(coordinator, "project_apps", read_apps)
    state.target = "工具项目的计时器"
    original_query = context.manager.auip_decider._query

    async def query(messages):
        if '"references"' in messages[0]["content"]:
            return json.dumps({"references":["work_item:" + item.work_item_id]})
        return await original_query(messages)

    context.manager.auip_decider._query = query
    await context.handler.send_text("工具项目里那个计时器打开吧。", session_id=context.session_id,
        turn_id="current-project")
    await context.finish()
    read_apps.assert_called_once_with(project.project_id, limit=200)
    event, = [p for method, p in state.events if method == Method.AUIP_LAUNCH_REQUESTED]
    assert event["artifact_id"] == artifact.artifact_id
    assert context.host.adapter.calls == 0


async def test_promoted_draft_remains_in_project_index_outside_hot_shelf(entry_host, monkeypatch):
    context, state, launch, item, _, artifact = entry_host
    coordinator = context.host.executor.coordinator
    monkeypatch.setattr(coordinator.destination, "_registry_check", lambda _path: True)
    before = Path(item.workspace_path, "index.html").read_bytes()
    promoted = coordinator.promote_work_item_to_project(item.work_item_id)
    assert promoted["workspacePath"] == item.workspace_path
    assert all(row["workItemId"] != item.work_item_id for row in coordinator.draft_apps(limit=5)["apps"])
    assert launch.candidates("later-conversation") == []
    candidates, complete = launch.project_candidates("later-conversation", current_only=False)
    selected = next(candidate for candidate in candidates if candidate.work_item_id == item.work_item_id)
    assert complete and selected.project_id == promoted["projectId"]
    await launch.route_control({"action":"launch", "_host_launch_candidates":(selected,)},
        session_id="later-conversation", turn_id="promoted-entry")
    event, = [p for method, p in state.events if method == Method.AUIP_LAUNCH_REQUESTED]
    assert event["artifact_id"] == artifact.artifact_id
    assert Path(item.workspace_path, "index.html").read_bytes() == before
    assert context.host.adapter.calls == 0


@pytest.mark.parametrize("outcome", ["selected", "missing_project", "foreign_app_token"])
async def test_named_project_is_resolved_before_same_name_draft(entry_host, tmp_path, monkeypatch, outcome):
    context, state, launch, *_ = entry_host
    coordinator = context.host.executor.coordinator
    monkeypatch.setattr(coordinator.destination, "_registry_check", lambda _path: True)
    hot, _, _ = _seed_app(context.host.work, context.host.project, tmp_path / "scratch" / "extra",
        title="Timer", turn_id="new-draft-timer")
    project = context.host.work.create_or_get_project(tmp_path / "old-tools", name="旧工具")
    old, _, artifact = _seed_app(context.host.work, project, tmp_path / "old-tools",
        title="Timer", turn_id="old-project-timer")
    assert hot.work_item_id in {item.work_item_id for item in launch.candidates(context.session_id)}
    before_project = coordinator.destination.session_project(context.session_id)
    reference_queries = []

    async def query(messages):
        if '"references"' not in messages[0]["content"]:
            return json.dumps({"action":"engage", "timing":"now", "mode":"collaborate",
                "target":"Timer", "project_ref":"旧工具", "work_relation":"subsumed"})
        reference_queries.append(messages)
        if len(reference_queries) == 1:
            assert project.project_id in messages[-1]["content"]
            assert hot.work_item_id not in messages[-1]["content"]
            return json.dumps({"references":[] if outcome == "missing_project" else ["project:" + project.project_id]})
        assert old.work_item_id in messages[-1]["content"]
        assert hot.work_item_id not in messages[-1]["content"]
        return json.dumps({"references":["work_item:" + (hot.work_item_id if outcome == "foreign_app_token" else old.work_item_id)]})

    context.manager.auip_decider._query = query
    await context.handler.send_text("旧工具项目里的计时器打开吧。", session_id=context.session_id,
        turn_id="qualified-entry")
    await context.finish()
    events = [p for method, p in state.events if method == Method.AUIP_LAUNCH_REQUESTED]
    assert len(reference_queries) == (1 if outcome == "missing_project" else 2)
    if outcome == "selected":
        assert len(events) == 1 and events[0]["artifact_id"] == artifact.artifact_id
    else:
        assert events == []
    assert coordinator.destination.session_project(context.session_id) == before_project
    assert context.host.adapter.calls == 0


async def test_entry_decision_reads_one_capability_snapshot_and_rechecks_at_dispatch(entry_host, monkeypatch):
    context, state, launch, item, *_ = entry_host
    original = launch._entry_candidates
    reads = []

    def capture(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        reads.append(snapshot)
        # A change after capture must not turn a second getter into a different
        # capability half of the same model decision.
        Path(item.workspace_path, "auip.manifest.json").write_text("{}", encoding="utf8")
        return snapshot

    monkeypatch.setattr(launch, "_entry_candidates", capture)
    decision = await context.manager.auip_decider.capture(session_id=context.session_id,
        user_text="刚才那个2048再打开吧。")
    assert len(reads) == 1
    assert decision.action == "launch"
    result = await launch.route_control(decision.control_attrs(), session_id=context.session_id,
        turn_id="changed-after-capture")
    assert result == {"ok":False, "error":"app_revision_changed"}
    assert not [p for method,p in state.events if method == Method.AUIP_LAUNCH_REQUESTED]
