from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("acp")

from agent_host.acp_configuration import AcpAgentSpec, load_acp_agents
from agent_host.adapters.acp import AcpProviderAdapter
from agent_host.mcp_connections import McpConnectionSpec
from agent_host.provider_types import (
    ProviderPermissionResponse,
    ProviderRunRequest,
    ProviderSessionHandle,
)

PEER = Path(__file__).parent / "fixtures" / "acp_agent.py"


def adapter(tmp_path, mode="normal", **kwargs):
    spec = AcpAgentSpec.from_dict(
        {
            "id": "deepseek",
            "name": "DeepSeek",
            "command": sys.executable,
            "args": [str(PEER), mode, str(tmp_path / "wire.jsonl")],
            "enabled": True,
            "resume": True,
            **kwargs.pop("config", {}),
        }
    )
    return AcpProviderAdapter(spec, setup_timeout=5, cancel_timeout=0.3, **kwargs)


def records(tmp_path):
    file = tmp_path / "wire.jsonl"
    return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []


async def wait_record(tmp_path, kind):
    async with asyncio.timeout(8):
        while not any(item["kind"] == kind for item in records(tmp_path)):
            await asyncio.sleep(0.02)


async def run_provider(provider, tmp_path, events, **kwargs):
    async def emit(event):
        events.append(event)

    return await provider.run(
        ProviderRunRequest(provider.provider_id, "Read a file", cwd=str(tmp_path), **kwargs),
        "run-1",
        emit,
    )


def test_registry_rejects_ambiguous_identity_and_inline_secrets():
    valid = {"id": "deepseek", "command": sys.executable}
    assert load_acp_agents(json.dumps([valid]))[0].provider_id == "deepseek"
    for values in (
        [valid, valid],
        [{**valid, "id": "codex"}],
        [{**valid, "environment": {"KEY": "sk-secret-value"}}],
        [{**valid, "enabled": "false"}],
        [{**valid, "api_key": "secret"}],
        [None],
    ):
        with pytest.raises(ValueError):
            load_acp_agents(json.dumps(values))


def test_acp_credential_descriptors_never_expose_values(monkeypatch):
    from server.handlers.system_handler import _acp_credentials

    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-credential")
    fields = _acp_credentials()
    assert all(field["type"] == "secret" and "value" not in field for field in fields)
    assert "private-credential" not in str(fields)


async def test_configured_agents_use_existing_registration_and_availability(tmp_path, monkeypatch):
    from agent_host.provider_runtime import ProviderRuntime
    from server.handlers import provider_handler

    specs = [adapter(tmp_path).spec.public_dict(), {
        "id": "missing_agent", "command": "amadeus-acp-missing-test-executable", "enabled": True,
    }]
    monkeypatch.setenv("AMADEUS_ACP_PROVIDERS", json.dumps(specs))
    monkeypatch.setattr(provider_handler, "runtime", ProviderRuntime())
    monkeypatch.setattr(provider_handler, "builtin_provider_specs", lambda: ())
    handler = provider_handler.ProviderHandler()
    listing = await handler._list({})
    assert listing["providers"] == ["deepseek"]
    missing = next(row for row in listing["provider_availability"] if row["provider_id"] == "missing_agent")
    assert not missing["registered"] and missing["reason"] == "acp_executable_unavailable"
    monkeypatch.setenv("AMADEUS_ACP_PROVIDERS", "invalid")
    handler = provider_handler.ProviderHandler()
    assert any(row["reason"] == "invalid_acp_configuration" for row in handler.provider_availability())


async def test_sdk_stdio_maps_events_options_and_native_attachment(tmp_path, monkeypatch):
    monkeypatch.setenv("ACP_SECRET_SOURCE", "test-value")
    monkeypatch.setenv("ACP_UNRELATED_SECRET", "must-not-leak")
    provider = adapter(
        tmp_path,
        config={
            "environment": {"ACP_TEST_SECRET": "ACP_SECRET_SOURCE"},
            "config_options": {"model": "large"},
        },
    )
    events = []
    result = await run_provider(provider, tmp_path, events)
    assert result.status == "done", result.error
    assert result.result == "Verified result."
    assert result.session.session_id == "native-session"
    assert result.activity_evidence.execution_items == 1
    assert [event.type for event in events] == [
        "session.opened",
        "tool.call",
        "tool.result",
        "assistant.delta",
    ]
    trace = records(tmp_path)
    assert next(item for item in trace if item["kind"] == "prompt")["secret"] == "test-value"
    assert next(item for item in trace if item["kind"] == "prompt")["unrelated"] is None
    assert any(item["kind"] == "config" and item["value"] == "large" for item in trace)
    assert provider.configuration()["config_options"][0]["currentValue"] == "large"
    result2 = await run_provider(provider, tmp_path, [], session=result.session)
    assert result2.status == "done", result2.error
    assert any(item["kind"] == "resume" for item in records(tmp_path))


@pytest.mark.parametrize(
    "mode,config", [("v2", {}), ("normal", {"config_options": {"model": "invented"}})]
)
async def test_unsupported_version_or_model_cannot_submit(tmp_path, mode, config):
    result = await run_provider(adapter(tmp_path, mode, config=config), tmp_path, [])
    assert result.status == "error"
    assert not any(item["kind"] == "prompt" for item in records(tmp_path))


async def test_resume_unavailable_never_silently_starts_fresh(tmp_path):
    result = await run_provider(
        adapter(tmp_path, "no_resume"),
        tmp_path,
        [],
        session=ProviderSessionHandle("deepseek", "old"),
    )
    assert result.status == "error"
    assert not any(item["kind"] in {"new", "prompt"} for item in records(tmp_path))


@pytest.mark.parametrize("allowed,expected", [(True, "yes"), (False, "no")])
async def test_permission_is_bound_and_one_shot(tmp_path, allowed, expected):
    provider = adapter(tmp_path, "permission")
    events = []
    task = asyncio.create_task(run_provider(provider, tmp_path, events))
    async with asyncio.timeout(8):
        while not any(event.type == "permission.requested" for event in events):
            if task.done():
                pytest.fail(str(await task))
            await asyncio.sleep(0.02)
    request_id = next(
        e.payload["permissionRequest"]["request_id"]
        for e in events
        if e.type == "permission.requested"
    )
    response = ProviderPermissionResponse(request_id, allowed)
    assert not (await provider.resolve_permission("wrong-run", response))["accepted"]
    assert (await provider.resolve_permission("run-1", response))["accepted"]
    assert not (await provider.resolve_permission("run-1", response))["accepted"]
    result = await task
    assert result.status == "done", result.error
    assert (
        next(item for item in records(tmp_path) if item["kind"] == "permission")["outcome"][
            "outcome"
        ]["optionId"]
        == expected
    )


async def test_durable_allow_cannot_be_selected_by_host_once(tmp_path):
    result = await run_provider(adapter(tmp_path, "durable_only"), tmp_path, [])
    assert result.status == "cancelled", result.error


@pytest.mark.parametrize("mode", ["wait", "permission_wait", "slow_setup"])
async def test_cancel_confirms_exact_execution_and_expires_approvals(tmp_path, mode):
    provider = adapter(tmp_path, mode)
    events = []
    task = asyncio.create_task(run_provider(provider, tmp_path, events))
    await wait_record(tmp_path, "initialize" if mode == "slow_setup" else "prompt")
    first, second = await asyncio.gather(provider.cancel("run-1"), provider.cancel("run-1"))
    result = await task
    assert first == second
    assert first["confirmed"] and first["cancelled"], first
    assert result.status == "cancelled", result.error
    assert sum(item["kind"] == "cancel" for item in records(tmp_path)) <= 1
    if mode == "slow_setup":
        assert not any(item["kind"] == "prompt" for item in records(tmp_path))


async def test_lost_connection_is_unknown_not_retryable_failure(tmp_path):
    result = await run_provider(adapter(tmp_path, "disconnect"), tmp_path, [])
    assert result.status == "orphaned"
    assert result.metadata["runtime_resumable"] is False
    assert sum(item["kind"] == "prompt" for item in records(tmp_path)) == 1


async def test_token_limit_is_not_success(tmp_path):
    result = await run_provider(adapter(tmp_path, "limit"), tmp_path, [])
    assert result.status == "error"
    assert result.activity_evidence.terminal_observed


async def test_auth_rejection_is_a_safe_failure_without_secret_details(tmp_path):
    result = await run_provider(adapter(tmp_path, "auth_required"), tmp_path, [])
    assert result.status == "error"
    assert result.metadata["result_type"] == "authentication_required"
    assert "must-not-appear" not in str(result.to_dict())


async def test_malformed_sdk_notification_cannot_be_silently_accepted(tmp_path):
    result = await run_provider(adapter(tmp_path, "malformed_update"), tmp_path, [])
    assert result.status == "orphaned"
    assert "projection" in result.error


async def test_host_projection_failure_cannot_complete(tmp_path):
    provider = adapter(tmp_path)

    async def fail(event):
        if event.type == "tool.call":
            raise RuntimeError("Host projection unavailable")

    result = await provider.run(
        ProviderRunRequest("deepseek", "Read a file", cwd=str(tmp_path)), "run-1", fail
    )
    assert result.status == "orphaned"


async def test_unconfirmed_cancel_stays_unknown_and_sdk_releases_process(tmp_path):
    provider = adapter(tmp_path, "ignore_cancel", run_timeout=0.2)
    result = await run_provider(provider, tmp_path, [])
    assert result.status == "orphaned"
    assert result.metadata["runtime_resumable"] is False
    assert len([row for row in records(tmp_path) if row["kind"] == "prompt"]) == 1


async def test_shutdown_does_not_invent_a_cancelled_outcome(tmp_path):
    provider = adapter(tmp_path, "wait")
    task = asyncio.create_task(run_provider(provider, tmp_path, []))
    await wait_record(tmp_path, "prompt")
    await provider.close()
    result = await task
    assert result.status == "orphaned"


async def test_slow_host_delivery_is_drained_before_terminal_result(tmp_path):
    provider = adapter(tmp_path)
    observed = []

    async def slow_emit(event):
        await asyncio.sleep(0.04)
        observed.append(event.type)

    result = await provider.run(
        ProviderRunRequest("deepseek", "Read a file", cwd=str(tmp_path)), "run-1", slow_emit
    )
    assert result.status == "done"
    assert observed[-1] == "assistant.delta"
    assert result.activity_evidence.execution_items == 1


async def test_progress_before_tool_does_not_consume_final_answer(tmp_path):
    events = []
    result = await run_provider(adapter(tmp_path, "progress"), tmp_path, events)
    assert result.status == "done"
    assert result.result == "Verified result."
    assert result.activity_evidence.progress_milestones == 1
    assert any(event.type == "semantic.progress" for event in events)


async def test_two_provider_instances_use_distinct_native_identity(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    first = adapter(left)
    second = adapter(right, config={"id": "claude", "name": "Claude"})
    a, b = await asyncio.gather(run_provider(first, left, []), run_provider(second, right, []))
    assert a.status == b.status == "done"
    assert a.session.provider != b.session.provider
    assert a.session.session_id == b.session.session_id  # Native ids need not be globally unique.
    rejected = await run_provider(second, right, [], session=a.session)
    assert rejected.status == "error"


async def test_new_session_cannot_be_attached_while_its_first_run_is_active(tmp_path):
    provider = adapter(tmp_path, "wait")
    events = []
    first = asyncio.create_task(run_provider(provider, tmp_path, events))
    await wait_record(tmp_path, "prompt")
    session = next(event.session for event in events if event.type == "session.opened")

    async def emit(event):
        events.append(event)

    second = await provider.run(
        ProviderRunRequest("deepseek", "Conflicting turn", str(tmp_path), session=session),
        "run-2",
        emit,
    )
    assert second.status == "error"
    assert sum(row["kind"] == "prompt" for row in records(tmp_path)) == 1
    await provider.cancel("run-1")
    assert (await first).status == "cancelled"


async def test_existing_runtime_and_ledger_keep_work_identity_and_acceptance(tmp_path, monkeypatch):
    from agent_host.provider_contract import ProviderRequirements
    from agent_host.provider_runtime import ProviderRuntime
    from agent_host.work_ledger_store import WorkLedgerStore
    from server.work_ledger_coordinator import WorkLedgerCoordinator

    monkeypatch.setattr("config.settings.WORK_WORKTREE_ISOLATION", False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("config.settings.WORK_PROJECT_ALLOWLIST", str(workspace))
    with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
        project = store.create_or_get_project(workspace)
        coordinator = WorkLedgerCoordinator(store)
        runtime = ProviderRuntime()
        provider = adapter(tmp_path)
        runtime.register(provider)
        runtime.set_request_preparer(coordinator.prepare_request)
        coordinator.configure()
        try:
            record = await runtime.start(
                ProviderRunRequest(
                    "deepseek",
                    "Read the requested file",
                    str(workspace),
                    requirements=ProviderRequirements(task_kind="general", workspace_access="read"),
                    metadata={
                        "source": "acp-contract-test",
                        "session_id": "host-chat",
                        "project_id": project.project_id,
                    },
                )
            )
            await record.task_handle
            await coordinator.drain_provider_facts()
            assert record.status == "done", record.error
            work = record.metadata["work"]
            attempt = store.get_attempt(work["attempt_id"])
            assert attempt.provider_run_id == record.run_id
            assert attempt.work_item_id == work["work_item_id"]
            assert attempt.execution_status == "succeeded"
            assert record.run_id != "native-session"
            assert store.get_work_item(work["work_item_id"]).state != "accepted"
            assert not store.list_artifacts(work["work_item_id"])
        finally:
            await runtime.close()
            await coordinator.drain_provider_facts()
            coordinator.close()


async def test_mcp_projection_is_provider_scoped_and_rejects_unrepresentable_cwd(tmp_path):
    connection = McpConnectionSpec("files", "Files", "stdio", True, ("claude",), command="unused")
    result = await run_provider(adapter(tmp_path, mcp_connections=(connection,)), tmp_path, [])
    assert result.status == "done", result.error
    assert next(item for item in records(tmp_path) if item["kind"] == "new")["mcp"] == []
    with pytest.raises(ValueError):
        adapter(
            tmp_path,
            mcp_connections=(
                McpConnectionSpec(
                    "files",
                    "Files",
                    "stdio",
                    True,
                    ("deepseek",),
                    command="unused",
                    cwd=str(tmp_path / "other"),
                ),
            ),
        )._mcp_servers(tmp_path, {})
