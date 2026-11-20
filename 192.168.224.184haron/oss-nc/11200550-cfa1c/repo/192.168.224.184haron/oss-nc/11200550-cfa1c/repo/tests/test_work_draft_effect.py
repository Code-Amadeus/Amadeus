"""An accepted new Draft reuses the existing Host allocation and Work pipeline."""
from pathlib import Path

import pytest

from server.control_ledger import ControlLedgerConflict
from test_chat_control_ingress import context as context
from test_chat_work_acceptance import host as host, send, decision
from test_work_context_effect import enable_context


async def test_chat_draft_create_amend_and_new_goal_keep_separate_workspaces(host, monkeypatch, tmp_path):
    scratch = tmp_path / "drafts"
    monkeypatch.setattr("config.settings.WORK_SCRATCH_ROOT", str(scratch))
    enable_context(host, monkeypatch)
    first_text = "帮我建个购物清单文件。"
    host.rows = [decision(first_text, work_placement="draft")]
    first = await send(host, first_text)
    assert "error" not in first, first
    first_binding = first["execution"]["binding"]
    first_item = host.context.store.get_work_item(first_binding["work_item_id"])
    first_path = Path(first_item.workspace_path)
    assert first_path.parent == scratch.resolve() and first_path != scratch.resolve()
    assert (first_path / ".git").exists()
    assert first_item.work_item_id == host.control.initial_work_item_id(first["accepted"]["effect_id"])
    assert host.coordinator.destination.is_unkept_draft(str(first_path))
    assert host.coordinator.session_project("A") == host.context.project.project_id
    replay = await host.runner.executor.execute(first["accepted"]["effect_id"])
    assert replay["replayed"] and host.adapter.calls == 1
    assert len(list(scratch.iterdir())) == 1

    addition = "再加上鸡蛋。"
    host.rows = [decision(addition, intent="amend", subject="work_item", work_placement="not_applicable",
        references=["work_item:" + first_item.work_item_id], reference_mode="candidates")]
    amended = await send(host, addition, "add-eggs")
    assert "error" not in amended, amended
    assert amended["execution"]["binding"]["work_item_id"] == first_item.work_item_id
    assert host.adapter.requests[1]["request"].cwd == str(first_path)
    assert host.adapter.requests[1]["request"].session is not None
    assert host.context.store.get_work_item(first_item.work_item_id).goal == first_text

    new_text = "对了，帮我保存一首诗。"
    host.rows = [decision(new_text, work_placement="draft")]
    independent = await send(host, new_text, "save-poem")
    assert "error" not in independent, independent
    new_item = host.context.store.get_work_item(independent["execution"]["binding"]["work_item_id"])
    assert new_item.work_item_id != first_item.work_item_id
    assert new_item.workspace_path != first_item.workspace_path
    assert Path(new_item.workspace_path).parent == scratch.resolve()
    assert len(list(scratch.iterdir())) == 2
    assert host.coordinator.session_project("A") == host.context.project.project_id
    assert all(row["execution"]["receipt"]["outcome"] == "succeeded" for row in (first, amended, independent))


async def test_failed_draft_claim_cannot_revive_retired_chat_input(host, monkeypatch, tmp_path):
    scratch = tmp_path / "drafts"
    monkeypatch.setattr("config.settings.WORK_SCRATCH_ROOT", str(scratch))
    text = "建个清单文件。"
    host.rows = [decision(text, work_placement="draft")]
    with host.context.store._transaction() as cursor:
        cursor.execute("CREATE TRIGGER reject_draft BEFORE INSERT ON run_attempts BEGIN SELECT RAISE(ABORT,'allocation test'); END")
    failed = await send(host, text)
    assert "error" in failed and "accepted" in failed
    effect = failed["accepted"]["effect_id"]
    assert host.control.binding(effect) is None and host.adapter.calls == 0
    assert host.context.store.list_work_items() == []
    allocated = list(scratch.iterdir())
    assert len(allocated) == 1
    with host.context.store._transaction() as cursor:
        cursor.execute("DROP TRIGGER reject_draft")
    # Chat retired the failed input. An empty prepared directory cannot revive it.
    assert host.context.ledger.get_effect(effect)["state"] == "cancelled"
    with pytest.raises(ControlLedgerConflict, match="not executable"):
        await host.runner.executor.execute(effect)
    assert host.adapter.calls == 0
    assert list(scratch.iterdir()) == allocated
    assert host.context.store.list_work_items() == []


async def test_draft_loss_after_binding_never_allocates_or_submits_a_sibling(host, monkeypatch, tmp_path):
    scratch = tmp_path / "drafts"
    monkeypatch.setattr("config.settings.WORK_SCRATCH_ROOT", str(scratch))
    original = host.coordinator.prepare_request
    def lose_after_binding(request, run_id, authority):
        original(request, run_id, authority)
        raise RuntimeError("lost after Draft binding")
    host.runtime.set_request_preparer(lose_after_binding)
    text = "建个清单文件。"
    host.rows = [decision(text, work_placement="draft")]
    failed = await send(host, text)
    assert "error" in failed and "accepted" in failed
    effect = failed["accepted"]["effect_id"]
    binding = host.control.binding(effect)
    assert binding is not None and host.adapter.calls == 0
    allocated = list(scratch.iterdir())
    result = await host.runner.executor.execute(effect)
    # A durable binding does not prove that the Runtime submitted this run.
    # Recovery must retain uncertainty and the same identity without resending.
    assert result["status"] == "unknown" and result["binding"] == binding
    assert len(allocated) == 1 and list(scratch.iterdir()) == allocated
    assert len(host.context.store.list_work_items()) == 1 and host.adapter.calls == 0
