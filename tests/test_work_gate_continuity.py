"""Work rejection/selection/active-run gates do not disable subsequent Chat."""
import asyncio

from server.attention_request import AttentionRequestCoordinator
from server.compound_control import CompoundControlPlan
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_targets import add_project, catalog_candidate, target_plan
from test_cooperative_planned_work import install_plans, planned, send


async def test_same_words_create_independent_work_while_prior_work_is_active(pending_host):
    context = pending_host
    text = "再创建一个独立的清单页。"
    plan = planned(context.manager.provider, text, text, "execute", one_off=True)
    await install_plans(context, {"one": plan, "two": plan, "chat": CompoundControlPlan(status="ok")})
    context.host.adapter.release.clear()
    try:
        first = await send(context, text, "one")
        await asyncio.wait_for(context.host.adapter.started.wait(), 3)
        assert (await context.handler.send_text(text, session_id=context.session_id,
            turn_id="one"))["status"] == "replayed"
        second = await send(context, text, "two")
        assert first["state"] == second["state"] == "work_started"
        assert first["work_item_id"] != second["work_item_id"]
        assert len(context.host.work.list_work_items()) == 2
        assert context.host.runtime.get_run(first["run_id"]).status == "running"
        assert (await send(context, "谢谢，聊点别的。", "chat"))["state"] == "no_action"
    finally:
        context.host.adapter.release.set()
        await context.finish()


async def test_invalid_provider_refusal_does_not_block_a_new_valid_request(pending_host):
    context = pending_host
    bad, good = "交给一个不存在的执行者。", "创建一个清单页。"
    await install_plans(context, {
        "bad": planned("unregistered", bad, bad, "execute"),
        "chat": CompoundControlPlan(status="ok"),
        "good": planned(context.manager.provider, good, good, "execute", one_off=True)})
    rejected = await send(context, bad, "bad")
    assert rejected["state"] == "rejected"
    assert not context.host.work.list_work_items() and context.host.adapter.calls == 0
    assert (await send(context, "谢谢，聊点别的。", "chat"))["state"] == "no_action"
    assert (await send(context, good, "good"))["state"] == "work_started"
    await context.finish()
    assert len(context.host.work.list_work_items()) == 1 and context.host.adapter.calls == 1


async def test_unresolved_target_selection_does_not_block_unrelated_chat_or_new_work(pending_host, tmp_path):
    context = pending_host
    projects = [add_project(context, tmp_path / f"project-{i}", f"Project {i}") for i in (1, 2)]
    candidates = [catalog_candidate(context, "project", p.project_id) for p in projects]
    context.manager.attention = AttentionRequestCoordinator()
    ambiguous, independent = "给选中的项目新增一个概览。", "另外独立创建一个计时器。"
    await install_plans(context, {
        "choice": target_plan(context.manager.provider, ambiguous, "amend", candidates),
        "chat": CompoundControlPlan(status="ok"),
        "independent": planned(context.manager.provider, independent, independent, "execute", one_off=True)})
    selection = await send(context, ambiguous, "choice")
    assert selection["state"] == "planned_work_selection_required"
    assert len(context.manager.attention.list_pending(context.session_id)) == 1
    assert (await send(context, "谢谢，聊点别的。", "chat"))["state"] == "no_action"
    assert (await send(context, independent, "independent"))["state"] == "work_started"
    await context.finish()
    assert len(context.host.work.list_work_items()) == 1
    assert all(item.project_id not in {p.project_id for p in projects}
        for item in context.host.work.list_work_items())
