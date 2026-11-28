"""M1 integration exercises the editable Eidema package with synthetic stores."""
import asyncio
from dataclasses import replace
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import eidema

from config.memory import MemoryConfig
from config.environment import EnvironmentReader
from core import session_manager as sm
from core.chat_history_projection import project_role_history
from server.cooperative_chat_ingress import _shared_role_history
from server.handlers.memory_handler import MemoryHandler
from server.memory_service import HostMemoryService
from server.protocol import Method


@pytest.fixture
async def host(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "_model_history_projection", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    sm.create_session("A")
    project = {"id":"project-a"}
    config = MemoryConfig(semantic_enabled=False, db_path=tmp_path / "memory.sqlite3", user_id="synthetic-user")
    service = HostMemoryService(config, project_for_session=lambda sid:project["id"])
    service.start()
    await service._startup
    assert service._store is not None
    sm.configure_model_history_projection(service.project_history)
    handler = MemoryHandler(service)
    context = service.context("A")
    async def request(method, **params):
        return await handler.handle(method, {"session_id":"A", "context_token":context["context_token"], **params})
    state = SimpleNamespace(service=service, handler=handler, context=context, request=request,
        config=config, project=project)
    try:
        yield state
    finally:
        await state.service.close()


async def remember(host, text="synthetic memory", *, operation="save", item="item", scope="character", source=None):
    result = await host.request(Method.MEMORY_REMEMBER, operation_id=operation, item_id=item,
        text=text, scope=scope, **({"source":source} if source is not None else {}))
    assert result["ok"], result
    return result["operation"]


async def forget(host, memory_id, version=1, *, operation="forget", item="forget-item"):
    preview = await host.request(Method.MEMORY_FORGET_PREVIEW, memory_id=memory_id, expected_version=version)
    assert preview["ok"], preview
    result = await host.request(Method.MEMORY_FORGET, memory_id=memory_id, expected_version=version,
        operation_id=operation, item_id=item, confirmed_targets=preview["preview"]["targets"])
    assert result["ok"], result
    return result


def source_for(text, *, role="assistant", message_id="source", turn="source-turn"):
    assert sm.append_session_message("A", role=role, content=text, turn_id=turn, message_id=message_id)
    source = sm.read_session_source("A", message_id, revision=1)
    return {key:source[key] for key in ("session_id", "message_id", "revision", "projection_version")}, source["text"]


async def test_explicit_lifecycle_receipts_conflicts_and_restart(host):
    first = await remember(host)
    replay = await remember(host)
    assert replay["memory_id"] == first["memory_id"] and replay["replayed"]
    conflict = await host.request(Method.MEMORY_REMEMBER, operation_id="save", item_id="item",
        text="different input", scope="character")
    assert conflict["error"] == "conflict"
    corrected = await host.request(Method.MEMORY_CORRECT, memory_id=first["memory_id"], expected_version=1,
        operation_id="correct", item_id="new-item", text="changed preference", mode="change")
    assert corrected["ok"]
    successor = corrected["operation"]["memory_id"]
    detail = await host.request(Method.MEMORY_GET, memory_id=successor)
    assert detail["memory"]["text"] == "changed preference"
    assert any(item["state"] == "superseded" and item["status_reason"] == "preference_changed" for item in detail["history"])
    stale = await host.request(Method.MEMORY_CORRECT, memory_id=first["memory_id"], expected_version=1,
        operation_id="stale", item_id="stale-item", text="bad", mode="error")
    assert stale["error"] == "conflict"
    await forget(host, successor)
    assert (await host.request(Method.MEMORY_LIST))["items"] == []
    assert (await host.request(Method.MEMORY_GET, memory_id=first["memory_id"]))["error"] == "not_found"
    retired = await remember(host)
    assert retired["status"] == "retired"
    await host.service.close()
    restarted = HostMemoryService(host.config, project_for_session=lambda sid:host.project["id"])
    restarted.start()
    try:
        await restarted._startup
        assert restarted._store.list_memories(restarted._access(restarted.context("A"))) == ()
        assert restarted._store.remember(restarted._access(restarted.context("A")),
            restarted._scope(restarted.context("A"), "character"), operation_id="save", item_id="item",
            text="untrusted replay body", now_ms=999).status == "retired"
    finally:
        await restarted.close()


async def test_scope_authority_and_stale_context_never_use_ui_identity(host, monkeypatch):
    own = await remember(host, "own character")
    api, store = eidema, host.service._store
    foreign = api.AccessScope("other-user", "kurisu", "project-a")
    secret = store.remember(foreign, api.MemoryScope("shared", "other-user"),
        operation_id="foreign", item_id="foreign-item", text="OTHER_USER_SECRET", now_ms=1)
    other_char = api.AccessScope("synthetic-user", "other-character", "project-a")
    hidden = store.remember(other_char, api.MemoryScope("project", "synthetic-user", "other-character", "project-a"),
        operation_id="other-character", item_id="other-char-item", text="OTHER_CHARACTER_SECRET", now_ms=2)
    for memory_id in (secret.memory_id, hidden.memory_id):
        assert (await host.request(Method.MEMORY_GET, memory_id=memory_id, user_id="other-user"))["error"] == "not_found"
        assert (await host.request(Method.MEMORY_CORRECT, memory_id=memory_id, expected_version=1,
            operation_id="spoof-" + memory_id, item_id="spoof-item-" + memory_id,
            text="spoof", mode="error"))["error"] == "not_found"
        assert (await host.request(Method.MEMORY_FORGET, memory_id=memory_id, expected_version=1,
            operation_id="forget-" + memory_id, item_id="forget-item-" + memory_id))["error"] == "not_found"
    assert [item["memory_id"] for item in (await host.request(Method.MEMORY_LIST))["items"]] == [own["memory_id"]]
    host.project["id"] = "project-b"
    assert (await host.request(Method.MEMORY_LIST))["error"] == "context_changed"
    sm.create_session("B")
    assert (await host.request(Method.MEMORY_LIST))["error"] == "stale_session"
    assert sm.load_session("A")[0]
    assert (await host.request(Method.MEMORY_LIST))["error"] == "context_changed"


async def test_source_paraphrase_validation_raw_coordinates_and_omission_boundary(host):
    raw = '  前[EMO preset="smile"]好物は抹茶です。後  '
    origin, canonical = source_for(raw)
    start = canonical.index("好物")
    end = canonical.index("後")
    source = {**origin, "start_cp":start, "end_cp":end}
    saved = await remember(host, "用户喜欢抹茶", source=source)
    assert (await host.request(Method.MEMORY_GET, memory_id=saved["memory_id"]))["memory"]["text"] == "用户喜欢抹茶"
    invalid = await host.request(Method.MEMORY_REMEMBER, operation_id="invalid", item_id="invalid-item",
        text="summary", source={**source, "end_cp":999}, scope="character")
    assert invalid["error"] == "invalid_request"
    frozen = _shared_role_history("A", "next")
    await forget(host, saved["memory_id"])
    projected = project_role_history(frozen)
    assert projected[0]["text"] == '  前[memory source omitted]後  '
    assert sm.conversation_history.dialog[-1]["content"] == raw
    assert "好物" not in sm.conversation_history.build_deepseek_messages("", "next")[0]["content"]
    assert "抹茶" not in sm.conversation_history.build_gemini_full_prompt("", "next")


async def test_forget_accepts_the_complete_preview_above_the_list_page_limit(host):
    origin, text = source_for("The box is in the rehearsal hall.")
    source = {**origin, "start_cp": 0, "end_cp": len(text)}
    roots = [await remember(host, f"Note {index}", source=source, operation=f"save-{index}",
                            item=f"item-{index}") for index in range(101)]
    independent = await remember(host, "Unrelated note", operation="independent", item="independent")
    params = dict(memory_id=roots[0]["memory_id"], expected_version=roots[0]["row_version"])
    preview = await host.request(Method.MEMORY_FORGET_PREVIEW, **params)
    assert preview["ok"], preview
    targets = preview["preview"]["targets"]
    assert {item["memory_id"] for item in targets} == {item["memory_id"] for item in roots}
    assert len(targets) == 101
    incomplete = await host.request(Method.MEMORY_FORGET, **params,
        operation_id="incomplete", item_id="incomplete", confirmed_targets=targets[:-1])
    assert incomplete["error"] == "conflict"
    result = await host.request(Method.MEMORY_FORGET, **params,
        operation_id="complete", item_id="complete", confirmed_targets=targets)
    assert result["ok"] and result["operation"]["status"] == "forgotten", result
    remaining = await host.request(Method.MEMORY_LIST)
    assert [item["memory_id"] for item in remaining["items"]] == [independent["memory_id"]]


async def test_excluded_assistant_hidden_and_control_copies_are_not_model_history(host):
    raw = '<think>HIDDEN_COPY</think>Before HIDDEN_COPY after[DELEGATE task="HIDDEN_COPY"]'
    origin, canonical = source_for(raw)
    saved = await remember(host, "explicit summary", source={**origin,
        "start_cp":canonical.index("HIDDEN_COPY"), "end_cp":canonical.index("HIDDEN_COPY") + len("HIDDEN_COPY")})
    await forget(host, saved["memory_id"])
    projected = project_role_history(_shared_role_history("A", "next"))
    assert projected[0]["text"] == "Before [memory source omitted] after"
    assert sm.conversation_history.dialog[-1]["content"] == raw


async def test_exclusions_survive_scope_disable_restart_and_missing_package(host, monkeypatch):
    origin, text = source_for("unrelated FORGET_ME suffix", role="user")
    saved = await remember(host, "summary", source={**origin, "start_cp":10, "end_cp":19})
    await forget(host, saved["memory_id"])
    await host.service.close()
    disabled = HostMemoryService(replace(host.config, enabled=False), project_for_session=lambda sid:"other-project")
    disabled.start()
    try:
        await disabled._startup
        sm.configure_model_history_projection(disabled.project_history)
        assert "FORGET_ME" not in json.dumps(sm.conversation_history.build_deepseek_messages("", "new input"))
        assert not (await disabled.status(disabled.context("A")))["state"] == "ready"
    finally:
        await disabled.close()
    failed = HostMemoryService(host.config)
    monkeypatch.setattr("server.memory_service.importlib.import_module", Mock(side_effect=ImportError("PRIVATE_ERROR")))
    failed.start()
    try:
        await failed._startup
        sm.configure_model_history_projection(failed.project_history)
        assert sm.conversation_history.build_deepseek_messages("", "current") == [
            {"role":"user", "content":"unrelated [memory source omitted] suffix"}, {"role":"user", "content":"current"}]
        assert (await failed.status(failed.context("A")))["history_exclusions_ready"] is True
    finally:
        await failed.close()


async def test_unknown_revision_or_projection_blocks_mapping_and_source_relink(host):
    origin, text = source_for("prefix private suffix")
    saved = await remember(host, source={**origin, "start_cp":7, "end_cp":14})
    await forget(host, saved["memory_id"])
    sm.conversation_history.mark_last_assistant_interrupted("prefix", turn_id="source-turn")
    assert sm.save_session("A")
    assert sm.conversation_history.model_recent() == []
    relink = await host.request(Method.MEMORY_REMEMBER, operation_id="relink", item_id="relink-item", text="new statement",
        scope="character", source={**origin, "revision":2, "start_cp":0, "end_cp":6})
    assert relink["error"] == "conflict"
    host.service._exclusions[("A", "source")] = [{"revision":2, "projection_version":"old-projection-v0", "start_cp":0, "end_cp":1}]
    assert (await host.request(Method.MEMORY_REMEMBER, operation_id="relink2", item_id="relink-item2", text="manual statement",
        scope="character", source={**origin, "revision":2, "start_cp":0, "end_cp":6}))["error"] == "conflict"
    assert (await remember(host, "new explicit manual statement", operation="manual", item="manual-item"))["status"] == "saved"


@pytest.mark.parametrize("cancel", [True, False])
async def test_late_forget_commit_installs_exclusions_after_request_timeout_or_cancel(host, monkeypatch, cancel):
    origin, text = source_for("keep PRIVATE_LATE keep", role="user")
    saved = await remember(host, source={**origin, "start_cp":5, "end_cp":17})
    real_forget = host.service._store.forget
    started, release = threading.Event(), threading.Event()
    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return real_forget(*args, **kwargs)
    monkeypatch.setattr(host.service._store, "forget", delayed)
    original_run = host.service._db.run
    async def run(function, **kwargs):
        return await original_run(function, **({**kwargs, "timeout":None if cancel else .01}
            if kwargs.get("mutation") else kwargs))
    monkeypatch.setattr(host.service._db, "run", run)
    task = asyncio.create_task(host.request(Method.MEMORY_FORGET, memory_id=saved["memory_id"], expected_version=1,
        operation_id="late-forget", item_id="late-forget-item"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task)["error"] == "outcome_unknown"
        release.set()
        if host.service._db._future is not None:
            await asyncio.wrap_future(host.service._db._future)
        await asyncio.sleep(0)
        assert host.service._exclusions
        assert "PRIVATE_LATE" not in json.dumps(sm.conversation_history.build_deepseek_messages("", "new input"))
        monkeypatch.setattr(host.service._db, "run", original_run)
        replay = await host.request(Method.MEMORY_FORGET, memory_id=saved["memory_id"], expected_version=1,
            operation_id="late-forget", item_id="late-forget-item")
        assert replay["ok"] and replay["operation"]["status"] == "forgotten", replay
    finally:
        release.set()


async def test_commit_receipt_is_truthful_after_session_switch(host, monkeypatch):
    real = host.service._store.remember
    started, release = threading.Event(), threading.Event()
    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return real(*args, **kwargs)
    monkeypatch.setattr(host.service._store, "remember", delayed)
    task = asyncio.create_task(host.request(Method.MEMORY_REMEMBER, operation_id="switch-save", item_id="switch-item",
        text="committed to original context", scope="project"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        sm.create_session("B")
        release.set()
        result = await task
        assert result["ok"] and result["context"]["session_id"] == "A" and result["operation"]["status"] == "saved"
    finally:
        release.set()


async def test_forget_privacy_barrier_covers_logical_commit_before_cleanup(host, monkeypatch):
    origin, text = source_for("before PRIVATE_CLEANUP after", role="user")
    saved = await remember(host, source={**origin, "start_cp":7, "end_cp":22})
    logical_commit, release = threading.Event(), threading.Event()
    real_cleanup = host.service._store.cleanup
    def paused_cleanup():
        logical_commit.set()
        assert release.wait(3)
        return real_cleanup()
    monkeypatch.setattr(host.service._store, "cleanup", paused_cleanup)
    task = asyncio.create_task(host.request(Method.MEMORY_FORGET, memory_id=saved["memory_id"], expected_version=1,
        operation_id="cleanup-forget", item_id="cleanup-forget-item"))
    try:
        assert await asyncio.to_thread(logical_commit.wait, 1)
        assert host.service._exclusions_ready
        assert sm.conversation_history.build_deepseek_messages("", "new user") == [
            {"role":"user", "content":"before [memory source omitted] after"}, {"role":"user", "content":"new user"}]
        release.set()
        assert (await task)["ok"]
        assert host.service._exclusions_ready
        assert "PRIVATE_CLEANUP" not in json.dumps(sm.conversation_history.model_recent())
    finally:
        release.set()


async def test_rejected_forget_restores_unaffected_model_history(host):
    assert sm.append_session_message("A", role="user", content="KEEP_UNRELATED", turn_id="old", message_id="old")
    saved = await remember(host)
    rejected = await host.request(Method.MEMORY_FORGET, memory_id=saved["memory_id"], expected_version=99,
        operation_id="rejected-forget", item_id="rejected-forget-item")
    assert rejected["error"] == "conflict" and host.service._exclusions_ready
    assert sm.conversation_history.model_recent()[0]["content"] == "KEEP_UNRELATED"


async def test_pagination_is_scoped_before_limit_and_validates_cursor(host):
    for index in range(5):
        await remember(host, f"row {index}", operation=f"page-{index}", item=f"page-item-{index}")
    first = await host.request(Method.MEMORY_LIST, limit=2)
    second = await host.request(Method.MEMORY_LIST, limit=2, cursor=first["next_cursor"])
    third = await host.request(Method.MEMORY_LIST, limit=2, cursor=second["next_cursor"])
    assert len({item["memory_id"] for page in (first, second, third) for item in page["items"]}) == 5
    assert third["next_cursor"] is None
    assert (await host.request(Method.MEMORY_LIST, cursor=[True, "bad"]))["error"] == "invalid_request"


async def test_forget_preview_never_silently_cascades_independent_items(host):
    origin, text = source_for("overlapping fact")
    source = {**origin, "start_cp":0, "end_cp":len(text)}
    one = await remember(host, "summary one", source=source)
    two = await remember(host, "summary two", operation="two", item="two-item", source=source)
    omitted = await host.request(Method.MEMORY_FORGET, memory_id=one["memory_id"], expected_version=1,
        operation_id="delete", item_id="delete-item")
    assert omitted["error"] == "conflict" and omitted["reason"] == "forget_confirmation_required"
    preview = await host.request(Method.MEMORY_FORGET_PREVIEW, memory_id=one["memory_id"], expected_version=1)
    assert {target["memory_id"] for target in preview["preview"]["targets"]} == {one["memory_id"], two["memory_id"]}
    await remember(host, "summary three", operation="three", item="three-item", source=source)
    stale = await host.request(Method.MEMORY_FORGET, memory_id=one["memory_id"], expected_version=1,
        operation_id="delete", item_id="delete-item", confirmed_targets=preview["preview"]["targets"])
    assert stale["error"] == "conflict" and stale["reason"] == "forget_preview_stale"
    assert len((await host.request(Method.MEMORY_LIST))["items"]) == 3
    await forget(host, one["memory_id"])
    assert (await host.request(Method.MEMORY_LIST))["items"] == []


def test_memory_configuration_defaults_semantic_on_and_no_uncalibrated_gate():
    config = MemoryConfig.from_environment(EnvironmentReader({}))
    assert config.enabled and config.semantic_enabled and config.semantic_threshold is None
    assert MemoryConfig.from_environment(EnvironmentReader({"AMADEUS_MEMORY_SEMANTIC_THRESHOLD":".82"})).semantic_threshold == .82


async def test_warmup_keeps_writes_status_fts_and_chat_available(host):
    await host.service.close()
    started, release = threading.Event(), threading.Event()
    def loader():
        started.set()
        assert release.wait(3)
        raise ImportError("PRIVATE_LOADER_ERROR")
    service = HostMemoryService(replace(host.config, semantic_enabled=True), embedder_factory=loader)
    service.start()
    try:
        await service._startup
        assert await asyncio.to_thread(started.wait, 1)
        handler = MemoryHandler(service)
        context = service.context("A")
        saved = await handler.handle(Method.MEMORY_REMEMBER, {**context, "text":"lexical preference",
            "scope":"character", "operation_id":"warmup", "item_id":"warmup-item"})
        assert saved["ok"]
        status = await service.status(context)
        assert status["state"] == "degraded" and status["semantic_state"] == "warming"
        assert await service.recall_for_session("A", "lexical") is None
        # Explicit core search remains available while automatic admission is off.
        result = await service._query.run(lambda:service._store.recall(service._access(context), "lexical", now_ms=1))
        assert result.candidates
        release.set()
        await service._index_task
        assert (await service.status(context))["semantic_reason"] == "dependencies_missing"
    finally:
        release.set()
        await service.close()
